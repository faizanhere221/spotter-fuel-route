import json
from decimal import Decimal
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from routing.models import Place, Station
from routing.services import corridor, ors
from routing.services.corridor import route_miles
from routing.services.places import normalize, nospace

FIXTURES = Path(__file__).parent / "fixtures"
ROUTE_JSON = json.loads((FIXTURES / "ny_la_route.json").read_text())
COORDS = ROUTE_JSON["features"][0]["geometry"]["coordinates"]
GEOCODE_JSON = {"type": "FeatureCollection", "features": [{
    "geometry": {"type": "Point", "coordinates": [-97.0, 38.0]},
    "properties": {"label": "Nowhereville, KS, USA", "layer": "locality"},
}]}
EXPECTED_KEYS = ["summary", "fuel_stops", "start", "finish", "assumptions", "external_api_calls",
                 "cached", "timings_ms", "map_url", "warnings", "route"]


def response(status=200, body=None):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status
    resp.reason = "reason"
    resp.content = json.dumps(body).encode()
    resp.text = json.dumps(body)
    resp.json.return_value = body
    return resp


class FakeORS:
    """Stands in for ors._session.request; records calls, serves the NY->LA fixture."""

    def __init__(self, directions=None, geocode=None):
        self.calls = []
        self.directions = directions or response(200, ROUTE_JSON)
        self.geocode = geocode or response(200, GEOCODE_JSON)

    def __call__(self, method, url, **kwargs):
        self.calls.append((method, url))
        return self.directions if "/directions/" in url else self.geocode


@override_settings(ORS_API_KEY="test-key", ORS_BASE_URL="https://ors.test", ORS_GEOCODE_FALLBACK=True,
                   ORS_PROFILE="driving-hgv", CORRIDOR_MILES=5.0, START_FUEL_GALLONS=50)
class RouteAPITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name, state, lat, lng, pop in [("New York City", "NY", 40.71427, -74.00597, 8804190),
                                           ("Los Angeles", "CA", 34.05223, -118.24368, 3898747)]:
            n = normalize(name)
            Place.objects.create(name=name, state=state, lat=lat, lng=lng, population=pop,
                                 name_norm=n, name_nospace=nospace(n))
        # Synthetic stations on the fixture route every ~150 mi (first at mile ~150).
        cum = route_miles(COORDS)
        now = timezone.now()
        for k, target in enumerate(range(150, int(cum[-1]), 150)):
            v = int(abs(cum - target).argmin())
            Station.objects.create(
                external_id=1000 + k, name=f"STOP {k}", address=f"I-80, EXIT {k}", city=f"Town{k}",
                state="NE", rack_id=1, price=Decimal("3.0") + Decimal(k % 5) / 10,
                lat=COORDS[v][1], lng=COORDS[v][0], imported_at=now)

    def setUp(self):
        cache.clear()
        corridor._index = None
        self.fake = FakeORS()
        patcher = mock.patch.object(ors._session, "request", side_effect=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, **body):
        body = {"start": "New York, NY", "finish": "Los Angeles, CA", **body}
        return self.client.post(reverse("route"), body, content_type="application/json")

    # --- happy path + caching -------------------------------------------------------------

    def test_first_call_hits_ors_once_repeat_is_cached(self):
        first = self.post()
        self.assertEqual(first.status_code, 200, first.content)
        body = first.json()
        self.assertEqual((body["external_api_calls"], body["cached"]), (1, False))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertTrue(self.fake.calls[0][1].endswith("/openrouteservice/v2/directions/driving-hgv/geojson"))
        self.assertGreater(body["summary"]["stop_count"], 0)
        self.assertEqual(body["summary"]["stop_count"], len(body["fuel_stops"]))
        self.assertEqual(body["warnings"], ["There may be restrictions on some roads"])
        self.assertEqual(body["route"]["geometry"]["type"], "LineString")
        self.assertEqual(body["start"]["label"], "New York City, NY")

        second = self.post().json()
        self.assertEqual((second["external_api_calls"], second["cached"]), (0, True))
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(second["fuel_stops"], body["fuel_stops"])
        self.assertEqual(second["summary"], body["summary"])

    def test_response_key_order_route_last(self):
        self.assertEqual(list(self.post().json()), EXPECTED_KEYS)

    def test_totals_consistent(self):
        body = self.post().json()
        stops = body["fuel_stops"]
        self.assertEqual(Decimal(body["summary"]["total_fuel_cost"]), sum(Decimal(s["cost"]) for s in stops))
        self.assertEqual(Decimal(body["summary"]["total_gallons_purchased"]), sum(Decimal(s["gallons"]) for s in stops))
        self.assertEqual(body["assumptions"]["start_fuel_charged"], False)
        self.assertEqual(body["assumptions"]["profile"], "driving-hgv")
        self.assertAlmostEqual(float(body["summary"]["fuel_remaining_at_finish"]), 0.0, places=3)

    def test_equivalent_inputs_share_plan_cache(self):
        self.post()
        other = self.post(start="40.71427,-74.00597").json()  # same point as New York City
        self.assertEqual((other["external_api_calls"], other["cached"]), (0, True))
        self.assertEqual(other["start"]["source"], "coordinates")

    def test_map_url_round_trip(self):
        body = self.post(start_fuel_gallons=25).json()
        url = urlparse(body["map_url"])
        self.assertEqual(url.path, reverse("route-map"))
        self.assertEqual(parse_qs(url.query), {"start": ["New York, NY"], "finish": ["Los Angeles, CA"],
                                               "start_fuel_gallons": ["25"]})

    def test_map_after_post_uses_cache(self):
        body = self.post().json()
        resp = self.client.get(body["map_url"])
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-External-API-Calls"], "0")
        self.assertEqual(resp["X-Cached"], "true")
        self.assertEqual(len(self.fake.calls), 1)
        html = resp.content.decode()
        self.assertIn('<script id="map-data" type="application/json">', html)
        self.assertIn("leaflet", html)
        data = json.loads(html.split('<script id="map-data" type="application/json">')[1].split("</script>")[0])
        self.assertEqual(len(data["stops"]), body["summary"]["stop_count"])
        self.assertEqual(len(data["route"]), len(COORDS))

    def test_map_escapes_station_names(self):
        Station.objects.filter(external_id=1000).update(name="</script><script>alert(1)</script>")
        Station.objects.update(imported_at=timezone.now())
        body = self.post().json()  # station 1000 ($3.00, mile ~150) is the first stop
        self.assertTrue(any("alert(1)" in s["name"] for s in body["fuel_stops"]))
        html = self.client.get(body["map_url"]).content.decode()
        self.assertNotIn("<script>alert(1)", html)
        self.assertIn("\\u003C/script\\u003E\\u003Cscript\\u003Ealert(1)", html)

    def test_map_without_prior_post_fetches_route(self):
        resp = self.client.get(reverse("route-map"), {"start": "New York, NY", "finish": "Los Angeles, CA"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-External-API-Calls"], "1")

    def test_geocode_fallback_counts_and_is_cached(self):
        first = self.post(start="Nowhereville, KS").json()
        self.assertEqual(first["external_api_calls"], 2)
        self.assertEqual(first["start"]["source"], "ors_geocode")
        self.assertEqual([m for m, _ in self.fake.calls], ["GET", "POST"])
        self.assertTrue(self.fake.calls[0][1].endswith("/pelias/v1/search"))
        second = self.post(start="nowhereville, ks").json()  # same normalized query
        self.assertEqual((second["external_api_calls"], second["cached"]), (0, True))
        self.assertEqual(len(self.fake.calls), 2)

    def test_station_reimport_misses_plan_cache_but_hits_route_cache(self):
        self.post()
        Station.objects.update(imported_at=timezone.now())  # new stations_version
        again = self.post().json()
        self.assertEqual((again["external_api_calls"], again["cached"]), (0, False))
        self.assertEqual(len(self.fake.calls), 1)

    def test_timing_logs(self):
        with self.assertLogs("routing.services.planner", level="INFO") as logs:
            self.post()
        line = "\n".join(logs.output)
        for stage in ("resolve=", "route=", "(ors)", "corridor=", "optimize=", "total=", "calls=1"):
            self.assertIn(stage, line)
        body = self.post().json()
        self.assertEqual(set(body["timings_ms"]), {"resolve", "plan_cache", "total"})

    # --- errors ------------------------------------------------------------------------------

    def assertError(self, resp, status, code):
        self.assertEqual(resp.status_code, status, resp.content)
        err = resp.json()["error"]
        self.assertEqual(err["code"], code)
        self.assertTrue(err["message"])
        return err

    def test_400_bad_body(self):
        err = self.assertError(self.client.post(reverse("route"), {"start": "New York, NY"},
                                                content_type="application/json"), 400, "invalid_request")
        self.assertIn("finish", err["fields"])

    def test_400_malformed_json(self):
        self.assertError(self.client.post(reverse("route"), "{not json", content_type="application/json"),
                         400, "parse_error")

    def test_400_start_fuel_out_of_range(self):
        err = self.assertError(self.post(start_fuel_gallons=51), 400, "invalid_request")
        self.assertIn("start_fuel_gallons", err["fields"])
        self.assertError(self.post(start_fuel_gallons=-1), 400, "invalid_request")

    def test_400_unknown_city(self):
        self.fake.geocode = response(200, {"type": "FeatureCollection", "features": []})
        err = self.assertError(self.post(finish="Nowhereville, KS"), 400, "invalid_location")
        self.assertIn("Place not found", err["message"])

    def test_geo_cache_ttls_found_30_days_not_found_1_day(self):
        from routing.services import planner

        with mock.patch.object(planner.cache, "set", wraps=planner.cache.set) as cache_set:
            self.post(start="Nowhereville, KS")  # found by the fake geocoder
            self.fake.geocode = response(200, {"type": "FeatureCollection", "features": []})
            self.post(finish="Faketown, TX")     # not found -> 400
        geo = {c.args[1]["result"] is not None: c.args[2] for c in cache_set.call_args_list
               if c.args[0].startswith("geo:v1:")}
        self.assertEqual(geo, {True: 30 * 24 * 3600, False: 24 * 3600})

        # The cached miss is served without another call.
        calls = len(self.fake.calls)
        self.assertEqual(self.post(finish="Faketown, TX").status_code, 400)
        self.assertEqual(len(self.fake.calls), calls)

    @override_settings(ORS_GEOCODE_FALLBACK=False)
    def test_400_unknown_city_without_fallback_makes_no_call(self):
        self.assertError(self.post(finish="Nowhereville, KS"), 400, "invalid_location")
        self.assertEqual(self.fake.calls, [])

    def test_400_outside_us_and_same_place(self):
        self.assertError(self.post(finish="Toronto, ON"), 400, "invalid_location")
        self.assertError(self.post(finish="51.5074,-0.1278"), 400, "invalid_location")
        self.assertError(self.post(finish="New York, NY"), 400, "invalid_location")

    def test_422_start_fuel_zero(self):
        err = self.assertError(self.post(start_fuel_gallons=0), 422, "unreachable")
        self.assertEqual(err["from_mile"], 0.0)
        self.assertGreater(err["gap_miles"], 100)

    def test_422_no_stations_in_corridor(self):
        Station.objects.all().delete()
        err = self.assertError(self.post(), 422, "unreachable")
        self.assertEqual(err["from_mile"], 0.0)

    def test_429_maps_to_503(self):
        self.fake.directions = response(429, {"error": "Rate limit exceeded"})
        err = self.assertError(self.post(), 503, "upstream_quota")
        self.assertEqual(err["upstream_status"], 429)

    def test_500_maps_to_502(self):
        self.fake.directions = response(500, {"error": {"code": 2099, "message": "boom"}})
        err = self.assertError(self.post(), 502, "upstream_error")
        self.assertEqual(err["upstream_status"], 500)

    def test_errors_are_not_cached(self):
        self.fake.directions = response(500, {"error": "boom"})
        self.post()
        self.fake.directions = response(200, ROUTE_JSON)
        self.assertEqual(self.post().status_code, 200)

    def test_405_uses_error_shape(self):
        self.assertError(self.client.get(reverse("route")), 405, "method_not_allowed")

    def test_map_errors_use_error_shape(self):
        self.assertError(self.client.get(reverse("route-map"), {"start": "New York, NY"}), 400, "invalid_request")
        self.assertError(self.client.get(reverse("route-map"), {"start": "New York, NY", "finish": "Los Angeles, CA",
                                                                "start_fuel_gallons": "0"}), 422, "unreachable")
