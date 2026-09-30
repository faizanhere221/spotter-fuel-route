"""Tests for the audit findings (error mapping, validation, consistency). ORS is mocked."""
import json
import re
import subprocess
import sys
from decimal import ROUND_HALF_UP, Decimal
from unittest import mock
from urllib.parse import parse_qs, urlparse

from django.conf import settings
from django.test import Client, SimpleTestCase
from django.urls import reverse

from routing.models import Station
from routing.tests.test_api import FIXTURES, ROUTE_JSON, APITestBase, response


def fixture_json(name):
    return json.loads((FIXTURES / name).read_text())


def walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


class AuditFixTests(APITestBase):
    # #1 + #2: ORS error codes -> our codes
    def test_no_route_is_422(self):
        self.fake.directions = response(404, fixture_json("ors_error_2009_route_not_found.json"))
        err = self.assertError(self.post(), 422, "no_route")
        self.assertEqual(err["upstream_status"], 404)
        self.assertIn("No road route", err["message"])

    def test_unroutable_point_is_400_and_names_point(self):
        self.fake.directions = response(404, fixture_json("ors_error_2010_point_not_found.json"))
        err = self.assertError(self.post(start="27.0,-90.0"), 400, "unroutable_point")
        self.assertEqual(err["point"], "start")

    def test_no_route_on_map_is_html_422(self):
        self.fake.directions = response(404, fixture_json("ors_error_2009_route_not_found.json"))
        resp = self.client.get(reverse("route-map"), {"start": "New York, NY", "finish": "Los Angeles, CA"})
        self.assertHTMLError(resp, 422, "no_route", "No road route")

    # #3: catch-all
    def test_unexpected_error_is_json_500_without_details(self):
        with mock.patch("routing.services.planner.plan_fuel", side_effect=RuntimeError("secret internals")), \
                self.assertLogs("routing.errors", level="ERROR") as logs:
            resp = self.post()
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.json(), {"error": {"code": "internal_error", "message": "Unexpected server error"}})
        self.assertNotIn("secret internals", resp.content.decode())
        self.assertIn("Traceback", "\n".join(logs.output))  # logged via logger.exception

    def test_unexpected_error_on_map_is_html_500(self):
        with mock.patch("routing.services.planner.plan_fuel", side_effect=RuntimeError("secret internals")), \
                self.assertLogs("routing.errors", level="ERROR"):
            resp = self.client.get(reverse("route-map"), {"start": "New York, NY", "finish": "Los Angeles, CA"})
        self.assertHTMLError(resp, 500, "internal_error", "Unexpected server error")
        self.assertNotIn("secret internals", resp.content.decode())

    # #4 + #5: malformed route -> 502, never cached
    def test_malformed_route_is_502_and_not_cached(self):
        bad = json.loads(json.dumps(ROUTE_JSON))
        del bad["features"][0]["properties"]["summary"]["distance"]
        self.fake.directions = response(200, bad)
        err = self.assertError(self.post(), 502, "upstream_error")
        self.assertIn("Malformed", err["message"])
        self.fake.directions = response(200, ROUTE_JSON)
        ok = self.post().json()
        self.assertEqual((ok["external_api_calls"], ok["cached"]), (1, False))
        self.assertEqual(len(self.fake.calls), 2)

    def test_zero_distance_route_is_502(self):
        bad = json.loads(json.dumps(ROUTE_JSON))
        bad["features"][0]["properties"]["summary"]["distance"] = 0
        self.fake.directions = response(200, bad)
        self.assertError(self.post(), 502, "upstream_error")

    # #6: stops come from the in-memory index, not a second DB lookup
    def test_stops_built_from_index_without_db_lookup(self):
        self.assertTrue(self.post().json()["fuel_stops"])
        # Same count and imported_at -> same stations_version -> the index is reused as-is.
        Station.objects.update(name="CHANGED IN DB")
        second = self.post(start_fuel_gallons=40).json()
        self.assertFalse(second["cached"])
        self.assertTrue(second["fuel_stops"])
        self.assertTrue(all(s["name"].startswith("STOP ") for s in second["fuel_stops"]))

    # #7: start fuel rounded once to 3 dp, same value everywhere
    def test_start_fuel_rounded_once_everywhere(self):
        # (≥15 gal needed to reach the first synthetic station at mile ~150)
        a = self.post(start_fuel_gallons=40.12345).json()
        b = self.post(start_fuel_gallons=40.1232).json()  # also 40.123 -> same plan
        c = self.post(start_fuel_gallons=40.1236).json()  # 40.124 -> different plan
        self.assertEqual(a["assumptions"]["start_fuel_gallons"], "40.123")
        self.assertEqual(parse_qs(urlparse(a["map_url"]).query)["start_fuel_gallons"], ["40.123"])
        self.assertEqual((b["cached"], b["assumptions"]["start_fuel_gallons"]), (True, "40.123"))
        self.assertEqual((c["cached"], c["assumptions"]["start_fuel_gallons"]), (False, "40.124"))

    # #12: price_per_gallon at full precision reproduces cost to the cent on every stop
    def test_price_times_gallons_reproduces_cost(self):
        stops = self.post().json()["fuel_stops"]
        self.assertTrue(stops)
        stored = dict(Station.objects.values_list("external_id", "price"))
        for s in stops:
            with self.subTest(stop=s["stop"]):
                self.assertEqual(Decimal(s["price_per_gallon"]), stored[s["station_id"]])
                self.assertEqual((Decimal(s["gallons"]) * Decimal(s["price_per_gallon"]))
                                 .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), Decimal(s["cost"]))
        self.assertTrue(any(len(s["price_per_gallon"].split(".")[1]) == 8 for s in stops))

    # #13: every money / gallon / price field is a string
    def test_decimal_fields_are_strings(self):
        body = self.post().json()
        checked = 0
        for path, value in walk({k: v for k, v in body.items() if k != "route"}):
            if re.search(r"cost|price|gallon", path.rsplit(".", 1)[-1]):
                checked += 1
                with self.subTest(path=path):
                    self.assertIsInstance(value, str)
                    Decimal(value)  # parses as a decimal
        self.assertGreater(checked, 10)
        self.assertEqual(body["assumptions"]["start_fuel_gallons"], "50.000")
        self.assertEqual(body["assumptions"]["tank_gallons"], "50.000")

    # #14: map is GET-only (405 even with CSRF checks on); unknown /api paths -> JSON 404
    def test_map_post_is_405_not_csrf_page(self):
        resp = Client(enforce_csrf_checks=True).post(reverse("route-map"), {"start": "a"})
        self.assertEqual(resp.status_code, 405)
        self.assertEqual(resp["Allow"], "GET")

    def test_unknown_api_path_is_json_404(self):
        for path in ("/api/nope/", "/api/route/extra/", "/api/"):
            with self.subTest(path=path):
                err = self.assertError(self.client.get(path), 404, "not_found")
                self.assertIn(path, err["message"])

    # #16: names that normalize to empty -> 400, no geocode call
    def test_unnormalizable_city_is_400_without_geocode(self):
        for name in ("東京, CA", "!!!, TX", "北京, CA"):
            with self.subTest(name=name):
                err = self.assertError(self.post(start=name), 400, "invalid_location")
                self.assertIn("no letters or digits", err["message"])
        self.assertEqual(self.fake.calls, [])


class SecretKeySettingTests(SimpleTestCase):
    """#9: settings are evaluated at import, so check them in a fresh interpreter."""

    def load_settings(self, **env):
        code = "import django, os; os.environ['DJANGO_SETTINGS_MODULE']='fuelroute.settings'; " \
               "django.setup(); from django.conf import settings; print(settings.DEBUG, len(settings.SECRET_KEY))"
        base = {k: v for k, v in __import__("os").environ.items() if not k.startswith("DJANGO_")}
        return subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, capture_output=True,
                              text=True, env={**base, **env}, timeout=60)

    def test_debug_false_without_key_refuses_to_start(self):
        r = self.load_settings(DJANGO_DEBUG="false", DJANGO_SECRET_KEY="")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ImproperlyConfigured", r.stderr)
        self.assertIn("DJANGO_SECRET_KEY must be set", r.stderr)

    def test_debug_false_with_key_starts(self):
        r = self.load_settings(DJANGO_DEBUG="false", DJANGO_SECRET_KEY="x" * 60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split(), ["False", "60"])

    def test_debug_true_uses_dev_fallback(self):
        r = self.load_settings(DJANGO_DEBUG="true", DJANGO_SECRET_KEY="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split()[0], "True")
