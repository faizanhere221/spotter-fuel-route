"""One error shape for the whole API, {"error": {"code", "message", ...details}}, and ONE mapping
from exception type to (HTTP status, error code), used by the DRF handler and the map view."""
import logging

from rest_framework.exceptions import APIException, ValidationError
from rest_framework.response import Response

from routing.services.optimizer import UnreachableError
from routing.services.ors import NoRouteError, ORSQuotaError, ORSUpstreamError, UnroutablePointError
from routing.services.places import LocationError

logger = logging.getLogger(__name__)

# Order matters only for subclasses; these types don't overlap.
ERROR_MAP = [
    (LocationError, 400, "invalid_location"),
    (UnroutablePointError, 400, "unroutable_point"),
    (UnreachableError, 422, "unreachable"),
    (NoRouteError, 422, "no_route"),
    (ORSQuotaError, 503, "upstream_quota"),
    (ORSUpstreamError, 502, "upstream_error"),
]


def error_body(code, message, **details):
    return {"error": {"code": code, "message": message, **details}}


def _details(exc):
    if isinstance(exc, UnreachableError):
        return {"gap_miles": round(exc.gap_miles, 1), "from_mile": round(exc.from_mile, 1),
                "to_mile": round(exc.to_mile, 1)}
    if isinstance(exc, UnroutablePointError):
        return {"point": exc.point, "upstream_status": exc.upstream_status}
    if hasattr(exc, "upstream_status"):
        return {"upstream_status": exc.upstream_status}
    return {}


def error_for(exc):
    """(status, body) for any exception. Unknown exceptions are logged and become a generic 500."""
    for exc_type, status, code in ERROR_MAP:
        if isinstance(exc, exc_type):
            return status, error_body(code, str(exc), **_details(exc))
    if isinstance(exc, ValidationError):
        return 400, error_body("invalid_request", "Invalid request.", fields=exc.detail)
    if isinstance(exc, APIException):  # DRF's own: parse error, 405, 415, ...
        detail = exc.detail.get("detail", exc.detail) if isinstance(exc.detail, dict) else exc.detail
        return exc.status_code, error_body(exc.default_code, str(detail))
    logger.exception("Unhandled error while planning a route")
    return 500, error_body("internal_error", "Unexpected server error")


def api_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER: every exception from an APIView goes through error_for()."""
    status, body = error_for(exc)
    return Response(body, status=status)
