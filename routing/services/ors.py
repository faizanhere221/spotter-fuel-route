"""OpenRouteService client — the only module in the project that makes HTTP calls.

One ORSClient per API request: `client.calls` counts every HTTP request it attempted, which
becomes `external_api_calls` in the response. The API key is sent in the Authorization header
and is never logged or included in exception messages.
"""
import logging
import time
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

_session = requests.Session()  # shared for connection pooling; headers are set per request


class ORSError(Exception):
    """Base class; `http_status` is what our API should return."""

    http_status = 502

    def __init__(self, message, upstream_status=None):
        super().__init__(message)
        self.upstream_status = upstream_status


class ORSQuotaError(ORSError):
    """ORS daily (403) or per-minute (429) quota exceeded."""

    http_status = 503


class ORSUpstreamError(ORSError):
    """Any other ORS failure: HTTP error, timeout, connection error, unparseable response."""

    http_status = 502


@dataclass(frozen=True)
class Route:
    coordinates: list  # [[lon, lat], ...] as returned by ORS GeoJSON
    distance_miles: float
    duration_seconds: float
    profile: str


def _error_message(resp):
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200] or resp.reason
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        return str(err.get("message") or err)
    return str(err)


class ORSClient:
    def __init__(self, api_key=None, base_url=None, session=None):
        self.api_key = settings.ORS_API_KEY if api_key is None else api_key
        self.base_url = (base_url or settings.ORS_BASE_URL).rstrip("/")
        self.session = session or _session
        self.timeout = (settings.ORS_CONNECT_TIMEOUT_SECONDS, settings.ORS_READ_TIMEOUT_SECONDS)
        self.calls = 0

    def _request(self, method, path, **kwargs):
        if not self.api_key:
            raise ORSUpstreamError("ORS_API_KEY is not configured.")
        headers = {"Authorization": self.api_key, "Accept": "application/json, application/geo+json"}
        self.calls += 1
        started = time.monotonic()
        try:
            resp = self.session.request(method, self.base_url + path, headers=headers,
                                        timeout=self.timeout, **kwargs)
        except requests.Timeout as exc:
            logger.warning("ORS %s %s timed out after %.0f ms", method, path, (time.monotonic() - started) * 1000)
            raise ORSUpstreamError(f"ORS request timed out ({type(exc).__name__}).") from None
        except requests.RequestException as exc:
            logger.warning("ORS %s %s failed: %s", method, path, type(exc).__name__)
            raise ORSUpstreamError(f"ORS request failed ({type(exc).__name__}).") from None

        elapsed_ms = (time.monotonic() - started) * 1000
        logger.info("ORS %s %s -> %s in %.0f ms (%d bytes)", method, path, resp.status_code,
                    elapsed_ms, len(resp.content))
        if resp.status_code in (403, 429):
            raise ORSQuotaError(f"ORS quota exceeded: {_error_message(resp)}", resp.status_code)
        if resp.status_code >= 400:
            raise ORSUpstreamError(f"ORS error {resp.status_code}: {_error_message(resp)}", resp.status_code)
        try:
            return resp.json()
        except ValueError:
            raise ORSUpstreamError("ORS returned a non-JSON response.", resp.status_code) from None

    def get_route(self, start, finish, profile=None):
        """Driving route between two (lat, lng) points. One HTTP call."""
        profile = profile or settings.ORS_PROFILE
        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],  # ORS wants [lon, lat]
            # We only use geometry + summary: drop turn-by-turn steps and thin the polyline.
            "instructions": False,
            "geometry_simplify": True,
        }
        data = self._request("POST", f"/v2/directions/{profile}/geojson", json=body)
        try:
            feature = data["features"][0]
            coords = feature["geometry"]["coordinates"]
            summary = feature["properties"].get("summary", {})
            distance_m = float(summary.get("distance", 0.0))
            duration_s = float(summary.get("duration", 0.0))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ORSUpstreamError("Unexpected ORS directions response shape.") from None
        if len(coords) < 2:
            raise ORSUpstreamError("ORS returned a route with fewer than 2 points.")
        return Route(coords, distance_m / METERS_PER_MILE, duration_s, profile)

    def geocode(self, text):
        """Best US locality match for free text -> (lat, lng, label), or None. One HTTP call."""
        params = {"text": text, "boundary.country": "US", "layers": "locality,localadmin", "size": 1}
        data = self._request("GET", "/geocode/search", params=params)
        features = (data.get("features") or []) if isinstance(data, dict) else []
        if not features:
            return None
        try:
            lng, lat = features[0]["geometry"]["coordinates"][:2]
            label = features[0].get("properties", {}).get("label") or text
        except (KeyError, IndexError, TypeError, ValueError):
            raise ORSUpstreamError("Unexpected ORS geocode response shape.") from None
        return float(lat), float(lng), label
