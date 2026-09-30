"""Stations within a corridor around a route: resample, project to xyz, nearest-point KD-tree query.

Everything except `get_station_index()` is a pure function over numpy arrays.
"""
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_MILES = 3958.7613
RESAMPLE_MILES = 0.5
MILES_PER_DEG_LAT = 69.0


@dataclass(frozen=True)
class Candidate:
    station_id: int      # OPIS Truckstop ID
    mile_marker: float   # distance along the route to the nearest route point
    offset_miles: float  # distance from the station to that route point
    price: object        # Decimal, full precision


class StationIndex:
    """Geocoded stations as parallel arrays; `xyz` is precomputed once per stations_version."""

    def __init__(self, version, ids, lat, lng, prices):
        self.version = version
        self.ids = np.asarray(ids, dtype=np.int64)
        self.lat = np.asarray(lat, dtype=np.float64)
        self.lng = np.asarray(lng, dtype=np.float64)
        self.prices = np.asarray(prices, dtype=object)
        self.xyz = to_xyz(self.lat, self.lng)

    def __len__(self):
        return len(self.ids)


_index = None
index_builds = 0  # observable in tests


def get_station_index():
    """Module-level StationIndex, rebuilt only when stations_version changes (1 aggregate query)."""
    global _index, index_builds
    from routing.models import Station, stations_version  # lazy: keep the rest Django-free

    version = stations_version()
    if _index is None or _index.version != version:
        rows = list(Station.objects.filter(lat__isnull=False, lng__isnull=False)
                    .order_by("external_id").values_list("external_id", "lat", "lng", "price"))
        ids, lat, lng, prices = zip(*rows) if rows else ((), (), (), ())
        _index = StationIndex(version, ids, lat, lng, prices)
        index_builds += 1
    return _index


def haversine_miles(lat1, lng1, lat2, lng2):
    lat1, lng1, lat2, lng2 = map(np.radians, (lat1, lng1, lat2, lng2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lng2 - lng1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def route_miles(coords):
    """Cumulative miles at each vertex of a [[lon, lat], ...] polyline (first value 0)."""
    c = np.asarray(coords, dtype=np.float64)[:, :2]
    seg = haversine_miles(c[:-1, 1], c[:-1, 0], c[1:, 1], c[1:, 0])
    return np.concatenate(([0.0], np.cumsum(seg)))


def resample(coords, step_miles=RESAMPLE_MILES):
    """Points every ~step_miles along the route -> (lat, lng, cum_miles), endpoints included."""
    c = np.asarray(coords, dtype=np.float64)[:, :2]
    cum = route_miles(c)
    total = cum[-1]
    n = max(int(np.ceil(total / step_miles)), 1)
    targets = np.linspace(0.0, total, n + 1)
    # Linear interpolation in lat/lng between vertices: segments are short relative to Earth's
    # curvature, so the error is far below the corridor width.
    lat = np.interp(targets, cum, c[:, 1])
    lng = np.interp(targets, cum, c[:, 0])
    return lat, lng, targets


def to_xyz(lat, lng):
    # Unit-sphere xyz: Euclidean (chord) distance maps exactly to great-circle distance via
    # 2R·asin(d/2), works anywhere without a per-route reference latitude, and suits cKDTree.
    lat, lng = np.radians(lat), np.radians(lng)
    return np.column_stack((np.cos(lat) * np.cos(lng), np.cos(lat) * np.sin(lng), np.sin(lat)))


def chord_to_miles(d):
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.clip(d / 2, 0.0, 1.0))


def miles_to_chord(miles):
    return 2 * np.sin(miles / (2 * EARTH_RADIUS_MILES))


def stations_along_route(coords, index, corridor_miles, step_miles=RESAMPLE_MILES):
    """Stations within corridor_miles of the route, sorted by mile_marker (then price, id).

    The offset is measured to the nearest resampled route point, so it can exceed the true
    perpendicular distance by at most ~step/2 (0.25 mi): negligible next to city-centroid error.
    Stations near but beyond either end snap to mile 0 / the route length.
    """
    if len(index) == 0:
        return []
    lat, lng, cum = resample(coords, step_miles)

    # Cheap bounding-box prefilter (route bbox + corridor margin) before the KD-tree query.
    lat_margin = corridor_miles / MILES_PER_DEG_LAT
    max_abs_lat = min(max(abs(lat.min()), abs(lat.max())) + lat_margin, 89.0)
    lng_margin = corridor_miles / (MILES_PER_DEG_LAT * np.cos(np.radians(max_abs_lat)))
    near = ((index.lat >= lat.min() - lat_margin) & (index.lat <= lat.max() + lat_margin)
            & (index.lng >= lng.min() - lng_margin) & (index.lng <= lng.max() + lng_margin))
    rows = np.flatnonzero(near)
    if rows.size == 0:
        return []

    tree = cKDTree(to_xyz(lat, lng))
    dist, idx = tree.query(index.xyz[rows], distance_upper_bound=miles_to_chord(corridor_miles))
    hit = np.isfinite(dist)
    offsets = chord_to_miles(dist[hit])
    keep = offsets <= corridor_miles
    rows, markers, offsets = rows[hit][keep], cum[idx[hit][keep]], offsets[keep]

    out = [Candidate(int(index.ids[r]), float(m), float(o), index.prices[r])
           for r, m, o in zip(rows, markers, offsets)]
    out.sort(key=lambda c: (c.mile_marker, c.price, c.station_id))
    return out
