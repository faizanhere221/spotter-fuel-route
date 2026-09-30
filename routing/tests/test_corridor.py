import json
from decimal import Decimal
from pathlib import Path

import numpy as np
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from routing.models import Station
from routing.services import corridor
from routing.services.corridor import (
    StationIndex, get_station_index, haversine_miles, resample, route_miles, stations_along_route,
)

FIXTURES = Path(__file__).parent / "fixtures"
MI_PER_DEG_LAT = 69.09  # at ~36°N, close enough for placing synthetic stations

# Straight north-south route along lng -100 from 35°N to 37°N (~138 mi).
ROUTE = [[-100.0, 35.0], [-100.0, 36.0], [-100.0, 37.0]]
TOTAL = haversine_miles(35.0, -100.0, 37.0, -100.0)


def east_of(lat, lng, miles):
    return lat, lng + miles / (MI_PER_DEG_LAT * np.cos(np.radians(lat)))


def index_of(*stations):
    """stations: (id, lat, lng, price)"""
    ids, lat, lng, prices = zip(*stations)
    return StationIndex("test", ids, lat, lng, [Decimal(p) for p in prices])


def load_fixture_route():
    return json.loads((FIXTURES / "ny_la_route.json").read_text())["features"][0]


class RouteGeometryTests(SimpleTestCase):
    def test_route_miles_cumulative(self):
        cum = route_miles(ROUTE)
        self.assertEqual(cum[0], 0.0)
        self.assertAlmostEqual(cum[-1], TOTAL, places=6)
        self.assertAlmostEqual(cum[1], haversine_miles(35.0, -100.0, 36.0, -100.0), places=6)

    def test_resample_spacing_and_endpoints(self):
        lat, lng, cum = resample(ROUTE)
        self.assertLessEqual(np.diff(cum).max(), 0.5 + 1e-9)
        self.assertEqual((lat[0], lng[0], cum[0]), (35.0, -100.0, 0.0))
        self.assertAlmostEqual(lat[-1], 37.0)
        self.assertAlmostEqual(cum[-1], TOTAL)

    def test_fixture_geometry_length_matches_ors_distance(self):
        f = load_fixture_route()
        ors_miles = f["properties"]["summary"]["distance"] / 1609.344
        self.assertAlmostEqual(route_miles(f["geometry"]["coordinates"])[-1], ors_miles, delta=ors_miles * 0.001)


class StationsAlongRouteTests(SimpleTestCase):
    def test_offset_filter_and_mile_marker(self):
        mid_mile = haversine_miles(35.0, -100.0, 36.0, -100.0)
        idx = index_of((1, *east_of(36.0, -100.0, 3.0), "3.1"), (2, *east_of(36.0, -100.0, 7.0), "2.9"))
        found = stations_along_route(ROUTE, idx, corridor_miles=5)
        self.assertEqual([c.station_id for c in found], [1])
        self.assertAlmostEqual(found[0].offset_miles, 3.0, delta=0.1)
        self.assertAlmostEqual(found[0].mile_marker, mid_mile, delta=0.5)
        self.assertEqual(found[0].price, Decimal("3.1"))

    def test_behind_start_and_past_finish(self):
        idx = index_of(
            (1, 35.0 - 3 / MI_PER_DEG_LAT, -100.0, "3"),  # 3 mi behind the start -> kept, mile 0
            (2, 35.0 - 7 / MI_PER_DEG_LAT, -100.0, "3"),  # 7 mi behind -> dropped
            (3, 37.0 + 3 / MI_PER_DEG_LAT, -100.0, "3"),  # 3 mi past the finish -> kept, mile = total
            (4, 37.0 + 7 / MI_PER_DEG_LAT, -100.0, "3"),  # 7 mi past -> dropped
        )
        found = stations_along_route(ROUTE, idx, corridor_miles=5)
        self.assertEqual([c.station_id for c in found], [1, 3])
        self.assertEqual(found[0].mile_marker, 0.0)
        self.assertAlmostEqual(found[1].mile_marker, TOTAL)
        self.assertAlmostEqual(found[0].offset_miles, 3.0, delta=0.1)

    def test_identical_coordinates_all_kept_sorted_by_price(self):
        lat, lng = east_of(36.5, -100.0, 1.0)
        idx = index_of((10, lat, lng, "3.5"), (11, lat, lng, "3.2"), (12, lat, lng, "3.9"))
        found = stations_along_route(ROUTE, idx, corridor_miles=5)
        self.assertEqual([c.station_id for c in found], [11, 10, 12])
        self.assertEqual(len({c.mile_marker for c in found}), 1)

    def test_sorted_by_mile_marker(self):
        idx = index_of((1, 36.8, -100.0, "3"), (2, 35.2, -100.0, "3"), (3, 36.0, -100.0, "3"))
        self.assertEqual([c.station_id for c in stations_along_route(ROUTE, idx, 5)], [2, 3, 1])

    def test_prefilter_keeps_edge_station_at_high_latitude(self):
        # East-west route at 48°N; station 4.9 mi north of it must survive the bbox prefilter.
        route = [[-110.0, 48.0], [-100.0, 48.0]]
        idx = index_of((1, 48.0 + 4.9 / 69.0, -105.0, "3"), (2, 30.0, -105.0, "3"))
        found = stations_along_route(route, idx, corridor_miles=5)
        self.assertEqual([c.station_id for c in found], [1])

    def test_empty_index(self):
        self.assertEqual(stations_along_route(ROUTE, StationIndex("v", [], [], [], []), 5), [])

    def test_fixture_route_finds_stations_placed_on_it(self):
        f = load_fixture_route()
        coords = f["geometry"]["coordinates"]
        cum = route_miles(coords)
        picks = [len(coords) // 5, len(coords) // 2, 4 * len(coords) // 5]
        idx = index_of(*[(i, coords[v][1], coords[v][0], "3.5") for i, v in enumerate(picks)])
        found = stations_along_route(coords, idx, corridor_miles=5)
        self.assertEqual([c.station_id for c in found], [0, 1, 2])
        for c, v in zip(found, picks):
            self.assertAlmostEqual(c.mile_marker, cum[v], delta=0.5)
            self.assertLess(c.offset_miles, 0.3)


class StationIndexCacheTests(TestCase):
    def make(self, external_id, lat, lng, price="3.5", when=None):
        return Station.objects.create(
            external_id=external_id, name=f"S{external_id}", address="I-1", city="X", state="OK",
            rack_id=1, price=Decimal(price), lat=lat, lng=lng, imported_at=when or timezone.now())

    def setUp(self):
        corridor._index = None

    def test_reload_only_when_version_changes_and_skips_null_coords(self):
        t = timezone.now()
        self.make(1, 36.0, -100.0, "3.00733333", t)
        self.make(2, None, None, when=t)
        before = corridor.index_builds
        idx = get_station_index()
        self.assertEqual(list(idx.ids), [1])
        self.assertEqual(idx.prices[0], Decimal("3.00733333"))
        self.assertIs(get_station_index(), idx)
        self.assertEqual(corridor.index_builds, before + 1)

        self.make(3, 36.5, -100.0, when=t + timezone.timedelta(seconds=1))  # new import batch
        idx2 = get_station_index()
        self.assertIsNot(idx2, idx)
        self.assertEqual(list(idx2.ids), [1, 3])
        self.assertEqual(corridor.index_builds, before + 2)
