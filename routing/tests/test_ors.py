from unittest import mock

import requests
from django.test import SimpleTestCase, override_settings

from routing.services.ors import METERS_PER_MILE, ORSClient, ORSQuotaError, ORSUpstreamError

KEY = "test-key-SECRET-123"

ROUTE_BODY = {
    "type": "FeatureCollection",
    "features": [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": [[-74.0, 40.7], [-75.0, 40.0], [-118.2, 34.0]]},
        "properties": {"summary": {"distance": 4500000.0, "duration": 150000.0}},
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
        self.assertEqual(route.profile, "driving-hgv")
        self.assertEqual(client.calls, 1)

        args, kwargs = session.request.call_args
        self.assertEqual(args, ("POST", "https://ors.test/v2/directions/driving-hgv/geojson"))
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
        self.assertTrue(session.request.call_args[0][1].endswith("/v2/directions/driving-car/geojson"))

    def test_429_is_quota_error_503(self):
        client, _ = self.client_with(fake_response(429, {"error": "Rate limit exceeded"}))
        with self.assertRaises(ORSQuotaError) as ctx:
            client.get_route((40.0, -74.0), (34.0, -118.0))
        self.assertEqual((ctx.exception.http_status, ctx.exception.upstream_status), (503, 429))
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
        self.assertEqual((ctx.exception.http_status, ctx.exception.upstream_status), (502, 500))
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
        with self.assertRaisesMessage(ORSUpstreamError, "Unexpected"):
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
        self.assertEqual(args, ("GET", "https://ors.test/geocode/search"))
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
