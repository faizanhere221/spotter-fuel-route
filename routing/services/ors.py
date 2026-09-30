"""OpenRouteService client — the only module that makes HTTP calls at request time.

(`manage.py load_places --download` also fetches a file, but it is an offline data-prep command,
never on the request path.)

One ORSClient per API request: `client.calls` counts every HTTP request it attempted, which
becomes `external_api_calls` in the response. The API key is sent in the Authorization header
and is never logged or included in exception messages.
"""
import logging
import math
import re
import time
from dataclasses import dataclass

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344

_session = requests.Session()  # shared for connection pooling; headers are set per request

# Service paths under ORS_BASE_URL (https://api.heigit.org). HeiGIT serves directions and the
# Pelias geocoder under different prefixes.
DIRECTIONS_PATH = "/openrouteservice/v2/directions/{profile}/geojson"
GEOCODE_PATH = "/pelias/v1/search"

# ORS routing error codes (docs: api-reference/error-codes, "Routing API").
ORS_ROUTE_NOT_FOUND = 2009  # "Route could not be found between locations."
ORS_POINT_NOT_FOUND = 2010  # "Point was not found." (no routable road near a coordinate)
POINT_NAMES = {0: "start", 1: "finish"}


class ORSError(Exception):
    def __init__(self, message, upstream_status=None):
        super().__init__(message)
        self.upstream_status = upstream_status


class ORSQuotaError(ORSError):
    """ORS daily (403) or per-minute (429) quota exceeded."""


class ORSUpstreamError(ORSError):
    """Any other ORS failure: HTTP error, timeout, connection error, malformed response."""


class NoRouteError(ORSError):
    """ORS found both points but no road route between them (e.g. Hawaii -> mainland)."""


class UnroutablePointError(ORSError):
    """No routable road near one of the points (e.g. a coordinate in the sea)."""

    def __init__(self, message, upstream_status=None, point=None):
        super().__init__(message, upstream_status)
        self.point = point  # "start" | "finish" | None when ORS doesn't say


@dataclass(frozen=True)
class Route:
    coordinates: list  # [[lon, lat], ...] as returned by ORS GeoJSON
    distance_miles: float
    duration_seconds: float
    warnings: tuple = ()  # ORS warning messages, e.g. "There may be restrictions on some roads"


def _json_or_none(resp):
    try:
        return resp.json()
    except ValueError:
        return None


def _error_parts(resp):
    """(ors_error_code or None, message) from an ORS error response."""
    body = _json_or_none(resp)
    err = body.get("error", body) if isinstance(body, dict) else body
    if isinstance(err, dict):
        code = err.get("code")
        return (code if isinstance(code, int) else None), str(err.get("message") or err)
    if err is None:
        return None, (resp.text[:200] or resp.reason)
    return None, str(err)


def _number(value):
    """A finite real number (bools rejected), else ValueError."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"not a finite number: {value!r}")
    return float(value)


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
        if resp.status_code >= 400:
            self._raise_for_error(resp)
        body = _json_or_none(resp)
        if body is None:
            raise ORSUpstreamError("ORS returned a non-JSON response.", resp.status_code)
        return body

    @staticmethod
    def _raise_for_error(resp):
        code, message = _error_parts(resp)
        status = resp.status_code
        if code == ORS_ROUTE_NOT_FOUND:
            raise NoRouteError(f"No road route between start and finish: {message}", status)
        if code == ORS_POINT_NOT_FOUND:
            # ORS names the point only in its message ("... specified coordinate 1: ...").
            m = re.search(r"coordinate (\d+)", message)
            point = POINT_NAMES.get(int(m.group(1))) if m else None
            raise UnroutablePointError(f"No routable road near the {point or 'given'} point: {message}",
                                       status, point)
        if status in (403, 429):
            raise ORSQuotaError(f"ORS quota exceeded: {message}", status)
        raise ORSUpstreamError(f"ORS error {status}: {message}", status)

    def get_route(self, start, finish, profile=None):
        """Driving route between two (lat, lng) points. One HTTP call."""
        profile = profile or settings.ORS_PROFILE
        body = {
            "coordinates": [[start[1], start[0]], [finish[1], finish[0]]],  # ORS wants [lon, lat]
            # We only use geometry + summary: drop turn-by-turn steps and thin the polyline.
            "instructions": False,
            "geometry_simplify": True,
        }
        data = self._request("POST", DIRECTIONS_PATH.format(profile=profile), json=body)
        try:
            feature = data["features"][0]
            geometry, props = feature["geometry"], feature["properties"]
            if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
                raise ValueError("geometry is not a LineString")
            coords = [[_number(p[0]), _number(p[1])] for p in geometry["coordinates"]]
            if len(coords) < 2:
                raise ValueError("fewer than 2 points")
            distance_m = _number(props["summary"]["distance"])
            duration_s = _number(props["summary"]["duration"])
            if distance_m <= 0 or duration_s < 0:
                raise ValueError("non-positive distance")
            raw_warnings = props.get("warnings", [])
            if not isinstance(raw_warnings, list):
                raise ValueError("warnings is not a list")
            warnings = tuple(str(w.get("message", w)) if isinstance(w, dict) else str(w) for w in raw_warnings)
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ORSUpstreamError(f"Malformed ORS directions response ({exc}).") from None
        return Route(coords, distance_m / METERS_PER_MILE, duration_s, warnings)

    def geocode(self, text):
        """Best US locality match for free text -> (lat, lng, label), or None. One HTTP call."""
        params = {"text": text, "boundary.country": "US", "layers": "locality,localadmin", "size": 1}
        data = self._request("GET", GEOCODE_PATH, params=params)
        try:
            features = data["features"]
            if not isinstance(features, list):
                raise ValueError("features is not a list")
            if not features:
                return None
            lng, lat = (_number(v) for v in features[0]["geometry"]["coordinates"][:2])
            props = features[0].get("properties") or {}
            label = props.get("label") if isinstance(props, dict) else None
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            raise ORSUpstreamError(f"Malformed ORS geocode response ({exc}).") from None
        return lat, lng, (label if isinstance(label, str) and label else text)
