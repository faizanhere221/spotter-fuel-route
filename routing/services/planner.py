"""plan_route(): resolve → route → corridor → optimize, with caching. Used by both endpoints.

Caches (Django cache, LocMem by default):
  geo:v1:{sha1(normalized query)}                                   30 days  ORS geocode fallback
  route:v1:{route_hash}                                              7 days   ORS directions
  plan:v1:{route_hash}:{stations_version}:{tank}:{mpg}:{fuel}:{corr} 1 day    computed plan
"""
import hashlib
import logging
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.cache import cache

from routing.models import Station, stations_version
from routing.services.corridor import get_station_index, stations_along_route
from routing.services.optimizer import plan_fuel
from routing.services.ors import ORSClient
from routing.services.places import LocationError, normalize, resolve_point

logger = logging.getLogger(__name__)

GEO_TTL = 30 * 24 * 3600
GEO_MISS_TTL = 24 * 3600  # "not found" is cached briefly so a fixed typo or new place isn't stuck
ROUTE_TTL = 7 * 24 * 3600
PLAN_TTL = 24 * 3600


@dataclass
class PlanResult:
    plan: dict                 # summary, fuel_stops, assumptions, warnings, route (cacheable part)
    start: dict
    finish: dict
    external_api_calls: int
    cached: bool
    timings_ms: dict = field(default_factory=dict)

    def as_response(self, map_url):
        """Response body in the documented key order; the bulky route geometry goes last."""
        return {
            "summary": self.plan["summary"],
            "fuel_stops": self.plan["fuel_stops"],
            "start": self.start,
            "finish": self.finish,
            "assumptions": self.plan["assumptions"],
            "external_api_calls": self.external_api_calls,
            "cached": self.cached,
            "timings_ms": self.timings_ms,
            "map_url": map_url,
            "warnings": self.plan["warnings"],
            "route": self.plan["route"],
        }


def sha1(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def fmt(value, places):
    return str(Decimal(repr(float(value)) if isinstance(value, float) else value)
               .quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def cached_geocoder(client):
    """Wrap client.geocode with the geo cache: hits for 30 days, "not found" for 1 day."""
    def geocode(text):
        key = f"geo:v1:{sha1(normalize(text))}"
        hit = cache.get(key)
        if hit is not None:
            return tuple(hit["result"]) if hit["result"] else None
        result = client.geocode(text)
        if result:
            cache.set(key, {"result": list(result)}, GEO_TTL)
        else:
            cache.set(key, {"result": None}, GEO_MISS_TTL)
        return result
    return geocode


def point_dict(p):
    return {"query": p.query, "label": p.label, "lat": p.lat, "lng": p.lng, "source": p.source}


def plan_route(start_text, finish_text, start_fuel=None):
    started = time.perf_counter()
    lap = started
    timings = {}

    def tick(name):
        nonlocal lap
        now = time.perf_counter()
        timings[name] = round((now - lap) * 1000, 1)
        lap = now

    tank, mpg, corridor = settings.TANK_GALLONS, settings.MPG, settings.CORRIDOR_MILES
    start_fuel = float(settings.START_FUEL_GALLONS if start_fuel is None else start_fuel)
    profile = settings.ORS_PROFILE
    client = ORSClient()  # per-request call counter

    # 1. Resolve (offline first; ORS geocode fallback only on a miss, behind the geo cache)
    geocoder = cached_geocoder(client) if settings.ORS_GEOCODE_FALLBACK else None
    start = resolve_point(start_text, geocoder)
    finish = resolve_point(finish_text, geocoder)
    if (round(start.lat, 4), round(start.lng, 4)) == (round(finish.lat, 4), round(finish.lng, 4)):
        raise LocationError("Start and finish resolve to the same location.")
    tick("resolve")

    # 2. Plan cache
    route_hash = sha1(f"{profile}|{start.lng:.4f},{start.lat:.4f}|{finish.lng:.4f},{finish.lat:.4f}")
    version = stations_version()
    plan_key = f"plan:v1:{route_hash}:{version}:{tank}:{mpg}:{start_fuel:g}:{corridor:g}"
    plan = cache.get(plan_key)
    if plan is not None:
        tick("plan_cache")
        timings["total"] = round((time.perf_counter() - started) * 1000, 1)
        logger.info("plan_route %s -> %s: plan cache hit; resolve=%.1fms total=%.1fms calls=%d",
                    start.label, finish.label, timings["resolve"], timings["total"], client.calls)
        return PlanResult(plan, point_dict(start), point_dict(finish), client.calls, True, timings)

    # 3. Route cache or ONE directions call
    route_key = f"route:v1:{route_hash}"
    route = cache.get(route_key)
    route_source = "cache"
    if route is None:
        r = client.get_route((start.lat, start.lng), (finish.lat, finish.lng), profile)
        route = {"coordinates": r.coordinates, "distance_miles": r.distance_miles,
                 "duration_seconds": r.duration_seconds, "warnings": list(r.warnings)}
        cache.set(route_key, route, ROUTE_TTL)
        route_source = "ors"
    tick("route")

    # 4. Corridor → optimizer
    candidates = stations_along_route(route["coordinates"], get_station_index(version), corridor)
    tick("corridor")
    fuel = plan_fuel(candidates, route["distance_miles"], tank, mpg, start_fuel)
    tick("optimize")

    plan = build_plan(route, candidates, fuel, start_fuel, profile)
    cache.set(plan_key, plan, PLAN_TTL)
    timings["total"] = round((time.perf_counter() - started) * 1000, 1)
    logger.info(
        "plan_route %s -> %s: resolve=%.1fms route=%.1fms (%s) corridor=%.1fms optimize=%.1fms "
        "total=%.1fms calls=%d candidates=%d stops=%d",
        start.label, finish.label, timings["resolve"], timings["route"], route_source,
        timings["corridor"], timings["optimize"], timings["total"], client.calls,
        len(candidates), len(fuel.stops))
    return PlanResult(plan, point_dict(start), point_dict(finish), client.calls, False, timings)


def build_plan(route, candidates, fuel, start_fuel, profile):
    offsets = {c.station_id: c.offset_miles for c in candidates}
    meta = Station.objects.in_bulk([s.station_id for s in fuel.stops], field_name="external_id")
    stops = []
    for n, s in enumerate(fuel.stops, 1):
        m = meta[s.station_id]
        stops.append({
            "stop": n, "station_id": s.station_id, "name": m.name, "address": m.address,
            "city": m.city, "state": m.state, "lat": m.lat, "lng": m.lng,
            "mile_marker": round(s.mile_marker, 1), "offset_miles": round(offsets[s.station_id], 1),
            "price_per_gallon": fmt(s.price, 3), "gallons": fmt(s.gallons, 3), "cost": fmt(s.cost, 2),
        })
    return {
        "summary": {
            "total_fuel_cost": fmt(fuel.total_cost, 2),
            "total_gallons_purchased": fmt(fuel.total_gallons_purchased, 3),
            "fuel_used_gallons": fmt(fuel.fuel_used_gallons, 3),
            "fuel_remaining_at_finish": fmt(fuel.fuel_remaining_at_finish, 3),
            "stop_count": len(stops),
            "distance_miles": round(route["distance_miles"], 1),
            "duration_hours": round(route["duration_seconds"] / 3600, 1),
            "stations_in_corridor": len(candidates),
        },
        "fuel_stops": stops,
        "assumptions": {
            "tank_gallons": settings.TANK_GALLONS,
            "mpg": settings.MPG,
            "start_fuel_gallons": start_fuel,
            "start_fuel_charged": False,
            "corridor_miles": settings.CORRIDOR_MILES,
            "profile": profile,
            "station_location_precision": "city centroid",
        },
        "warnings": route["warnings"],
        "route": {
            "type": "Feature",
            "properties": {},
            "geometry": {"type": "LineString", "coordinates": route["coordinates"]},
        },
    }
