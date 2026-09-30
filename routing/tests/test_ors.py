from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

import copy
import json
from pathlib import Path

from routing.services.ors import (
    METERS_PER_MILE, NoRouteError, ORSClient, ORSQuotaError, ORSUpstreamError, UnroutablePointError,
)

FIXTURES = Path(__file__).parent / "fixtures"

KEY = "test-key-SECRET-123"

ROUTE_BODY = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.7], [-75.0, 40.0], [-118.2, 34.0]]},
        "properties": {"summary": {"distance": 4500000.0, "duration": 150000.0},
                       "warnings": [{"code": 1, "message": "There may be restrictions on some roads"}]},
    }],
}


def fake_response(status=200, json_body=None, text=""):
    resp = mock.Mock(spec=requests.Response)
    resp.status_code = status
    resp.reason = "reason"
    resp.content = (text or str(json_body)).encode()
    resp.text = text
    if json_body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = json_body
    return resp


@override_settings(ORS_API_KEY=KEY, ORS_BASE_URL="https://ors.test", ORS_PROFILE="driving-hgv",
                   ORS_CONNECT_TIMEOUT_SECONDS=5, ORS_READ_TIMEOUT_SECONDS=30)
class ORSClientTests(SimpleTestCase):
    def client_with(self, response=None, side_effect=None):
        session = mock.Mock()
        session.request.return_value = response
        if side_effect is not None:
            session.request.side_effect = side_effect
        return ORSClient(session=session), session

    def test_get_route_success_parse_and_request_shape(self):
        client, session = self.client_with(fake_response(200, ROUTE_BODY))
        route = client.get_route((40.7128, -74.0060), (34.0522, -118.2437))

        self.assertEqual(route.coordinates, ROUTE_BODY["features"][0]["geometry"]["coordinates"])
        self.assertAlmostEqual(route.distance_miles, 4500000.0 / METERS_PER_MILE)
        self.assertEqual(route.duration_seconds, 150000.0)
        self.assertEqual(route.warnings, ("There may be restrictions on some roads",))
        self.assertEqual(client.calls, 1)

        args, kwargs = session.request.call_args
        self.assertEqual(args, ("POST", "https://ors.test/openrouteservice/v2/directions/driving-hgv/geojson"))
        self.assertEqual(kwargs["json"], {
            "coordinates": [[-74.0060, 40.7128], [-118.2437, 34.0522]],
            "instructions": False,
            "geometry_simplify": True,
        })
        self.assertEqual(kwargs["headers"]["Authorization"], KEY)
        self.assertEqual(kwargs["timeout"], (5, 30))

    def test_profile_override(self):
        client, session = self.client_with(fake_response(200, ROUTE_BODY))
        client.get_route((40.0, -74.0), (34.0, -118.0), profile="driving-car")
        self.assertTrue(session.request.call_args[0][1].endswith("/openrouteservice/v2/directions/driving-car/geojson"))

    def test_429_is_quota_error_503(self):
        client, _ = self.client_with(fake_response(429, {"error": "Rate limit exceeded"}))
        with self.assertRaises(ORSQuotaError) as ctx:
            client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertEqual(ctx.exception.upstream_status, 429)
        self.assertIn("Rate limit exceeded", str(ctx.exception))
        self.assertEqual(client.calls, 1)

    def test_403_is_quota_error(self):
        client, _ = self.client_with(fake_response(403, {"error": "Quota exceeded"}))
        with self.assertRaises(ORSQuotaError):
            client.get_route((40.0, -74.0), (34.0, -118.0))

    def test_500_is_upstream_error_502(self):
        client, _ = self.client_with(fake_response(500, {"error": {"code": 2099, "message": "Unknown internal error"}}))
        with self.assertRaises(ORSUpstreamError) as ctx:
            client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertEqual(ctx.exception.upstream_status, 500)
        self.assertIn("Unknown internal error", str(ctx.exception))

    def test_non_json_error_body(self):
        client, _ = self.client_with(fake_response(502, None, text="<html>Bad gateway</html>"))
        with self.assertRaisesMessage(ORSUpstreamError, "Bad gateway"):
            client.get_route((40.0, -74.0), (34.0, -118.0))

    def test_timeout_is_upstream_error(self):
        client, _ = self.client_with(side_effect=requests.ReadTimeout("read timed out"))
        with self.assertRaisesMessage(ORSUpstreamError, "timed out"):
            client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertEqual(client.calls, 1)

    def test_connection_error_is_upstream_error(self):
        client, _ = self.client_with(side_effect=requests.ConnectionError("dns"))
        with self.assertRaisesMessage(ORSUpstreamError, "ConnectionError"):
            client.get_route((40.0, -74.0), (34.0, -118.0))

    def test_unexpected_shape(self):
        client, _ = self.client_with(fake_response(200, {"features": []}))
        with self.assertRaisesMessage(ORSUpstreamError, "Malformed"):
            client.get_route((40.0, -74.0), (34.0, -118.0))

    @override_settings(ORS_API_KEY="")
    def test_missing_key_makes_no_call(self):
        client, session = self.client_with(fake_response(200, ROUTE_BODY))
        with self.assertRaisesMessage(ORSUpstreamError, "ORS_API_KEY is not configured"):
            client.get_route((40.0, -74.0), (34.0, -118.0))
        session.request.assert_not_called()
        self.assertEqual(client.calls, 0)

    def test_key_never_logged_or_in_errors(self):
        client, _ = self.client_with(fake_response(429, {"error": "slow down"}))
        with self.assertLogs("routing.services.ors", level="INFO") as logs, self.assertRaises(ORSQuotaError) as ctx:
            client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertNotIn(KEY, "\n".join(logs.output))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_geocode_success_and_params(self):
        body = {"type": "FeatureCollection", "features": [{
            "geometry": {"type": "Point", "coordinates": [-95.22, 36.54]},
            "properties": {"label": "Big Cabin, OK, USA"},
        }]}
        client, session = self.client_with(fake_response(200, body))
        self.assertEqual(client.geocode("Big Cabin, OK"), (36.54, -95.22, "Big Cabin, OK, USA"))
        args, kwargs = session.request.call_args
        self.assertEqual(args, ("GET", "https://ors.test/pelias/v1/search"))
        self.assertEqual(kwargs["params"]["boundary.country"], "US")
        self.assertEqual(kwargs["params"]["size"], 1)
        self.assertEqual(kwargs["params"]["text"], "Big Cabin, OK")
        self.assertEqual(client.calls, 1)

    def test_geocode_no_result(self):
        client, _ = self.client_with(fake_response(200, {"type": "FeatureCollection", "features": []}))
        self.assertIsNone(client.geocode("Nowhereville, KS"))

    def test_calls_accumulate_per_client(self):
        client, _ = self.client_with(fake_response(200, ROUTE_BODY))
        client.get_route((40.0, -74.0), (34.0, -118.0))
        client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertEqual(client.calls, 2)
        self.assertEqual(ORSClient(session=mock.Mock()).calls, 0)


def error_fixture(name):
    return json.loads((FIXTURES / name).read_text())


@override_settings(ORS_API_KEY=KEY, ORS_BASE_URL="https://ors.test", ORS_PROFILE="driving-hgv",
                   ORS_CONNECT_TIMEOUT_SECONDS=5, ORS_READ_TIMEOUT_SECONDS=30)
class ORSErrorCodeTests(SimpleTestCase):
    """ORS errors are mapped by the `error.code` in the body (docs: Routing API error codes)."""

    def route_with(self, status, body):
        session = mock.Mock()
        session.request.return_value = fake_response(status, body)
        return ORSClient(session=session).get_route((21.3, -157.8), (34.0, -118.2))

    def test_2009_route_not_found_is_no_route(self):
        with self.assertRaises(NoRouteError) as ctx:
            self.route_with(404, error_fixture("ors_error_2009_route_not_found.json"))
        self.assertEqual(ctx.exception.upstream_status, 404)

    def test_2010_point_not_found_names_the_point(self):
        with self.assertRaises(UnroutablePointError) as ctx:
            self.route_with(404, error_fixture("ors_error_2010_point_not_found.json"))
        self.assertEqual(ctx.exception.point, "start")  # "specified coordinate 0"
        body = error_fixture("ors_error_2010_point_not_found.json")
        body["error"]["message"] = body["error"]["message"].replace("coordinate 0", "coordinate 1")
        with self.assertRaises(UnroutablePointError) as ctx:
            self.route_with(404, body)
        self.assertEqual(ctx.exception.point, "finish")

    def test_2010_without_coordinate_hint(self):
        with self.assertRaises(UnroutablePointError) as ctx:
            self.route_with(404, {"error": {"code": 2010, "message": "Point was not found."}})
        self.assertIsNone(ctx.exception.point)

    def test_mapping_uses_code_not_message(self):
        with self.assertRaises(NoRouteError):  # code wins even with unrelated text
            self.route_with(404, {"error": {"code": 2009, "message": "something else"}})
        with self.assertRaises(ORSUpstreamError) as ctx:  # same text, no code -> generic upstream
            self.route_with(404, {"error": {"message": "Route could not be found"}})
        self.assertNotIsInstance(ctx.exception, NoRouteError)
        with self.assertRaises(ORSUpstreamError):  # other routing codes stay upstream errors
            self.route_with(400, {"error": {"code": 2003, "message": "Invalid parameter value."}})


@override_settings(ORS_API_KEY=KEY, ORS_BASE_URL="https://ors.test", ORS_PROFILE="driving-hgv",
                   ORS_CONNECT_TIMEOUT_SECONDS=5, ORS_READ_TIMEOUT_SECONDS=30)
class ORSResponseValidationTests(SimpleTestCase):
    def route_from(self, body):
        session = mock.Mock()
        session.request.return_value = fake_response(200, body)
        return ORSClient(session=session).get_route((40.0, -74.0), (34.0, -118.0))

    def mutated(self, fn):
        body = copy.deepcopy(ROUTE_BODY)
        fn(body["features"][0])
        return body

    def test_malformed_directions_responses_are_upstream_errors(self):
        cases = {
            "no features": {"type": "FeatureCollection"},
            "features not a list": {"features": {}},
            "summary missing": self.mutated(lambda f: f["properties"].pop("summary")),
            "summary is a list": self.mutated(lambda f: f["properties"].__setitem__("summary", [])),
            "distance missing": self.mutated(lambda f: f["properties"]["summary"].pop("distance")),
            "distance zero": self.mutated(lambda f: f["properties"]["summary"].__setitem__("distance", 0)),
            "distance non-numeric": self.mutated(lambda f: f["properties"]["summary"].__setitem__("distance", "far")),
            "distance bool": self.mutated(lambda f: f["properties"]["summary"].__setitem__("distance", True)),
            "duration missing": self.mutated(lambda f: f["properties"]["summary"].pop("duration")),
            "geometry is a Point": self.mutated(lambda f: f["geometry"].__setitem__("type", "Point")),
            "one point": self.mutated(lambda f: f["geometry"].__setitem__("coordinates", [[-74.0, 40.7]])),
            "non-numeric coordinate": self.mutated(lambda f: f["geometry"]["coordinates"].__setitem__(1, ["a", "b"])),
            "short coordinate": self.mutated(lambda f: f["geometry"]["coordinates"].__setitem__(1, [1.0])),
            "properties null": self.mutated(lambda f: f.__setitem__("properties", None)),
            "warnings not a list": self.mutated(lambda f: f["properties"].__setitem__("warnings", "careful")),
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaisesMessage(ORSUpstreamError, "Malformed ORS directions"):
                self.route_from(body)

    def test_string_warnings_are_accepted(self):
        route = self.route_from(self.mutated(lambda f: f["properties"].__setitem__("warnings", ["careful"])))
        self.assertEqual(route.warnings, ("careful",))

    def geocode_from(self, body):
        session = mock.Mock()
        session.request.return_value = fake_response(200, body)
        return ORSClient(session=session).geocode("Big Cabin, OK")

    def test_malformed_geocode_responses_are_upstream_errors(self):
        point = {"type": "Point", "coordinates": [-95.2, 36.5]}
        cases = {
            "no features key": {"type": "FeatureCollection"},
            "features not a list": {"features": {}},
            "string coordinates": {"features": [{"geometry": {"coordinates": ["a", "b"]}}]},
            "one coordinate": {"features": [{"geometry": {"coordinates": [1.0]}}]},
            "no geometry": {"features": [{"properties": {"label": "x"}}]},
        }
        for name, body in cases.items():
            with self.subTest(name), self.assertRaisesMessage(ORSUpstreamError, "Malformed ORS geocode"):
                self.geocode_from(body)

    def test_geocode_missing_label_falls_back_to_query_text(self):
        # The label is optional; coordinates are what we need.
        for props in (None, ["x"], {}, {"label": ""}):
            with self.subTest(props=props):
                body = {"features": [{"geometry": {"type": "Point", "coordinates": [-95.2, 36.5]}, "properties": props}]}
                self.assertEqual(self.geocode_from(body), (36.5, -95.2, "Big Cabin, OK"))
