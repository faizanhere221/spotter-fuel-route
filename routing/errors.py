"""One error shape for the whole API: {"error": {"code", "message", ...details}}."""
from rest_framework.views import exception_handler as drf_exception_handler

from routing.services.optimizer import UnreachableError
from routing.services.ors import ORSQuotaError, ORSUpstreamError
from routing.services.places import LocationError


def error_body(code, message, **details):
    return {"error": {"code": code, "message": message, **details}}


def domain_error(exc):
    """(status, body) for planner exceptions, or None if exc isn't one of ours."""
    if isinstance(exc, LocationError):
        return 400, error_body("invalid_location", str(exc))
    if isinstance(exc, UnreachableError):
        return 422, error_body("unreachable", str(exc), gap_miles=round(exc.gap_miles, 1),
                               from_mile=round(exc.from_mile, 1), to_mile=round(exc.to_mile, 1))
    if isinstance(exc, ORSQuotaError):
        return 503, error_body("upstream_quota", str(exc), upstream_status=exc.upstream_status)
    if isinstance(exc, ORSUpstreamError):
        return 502, error_body("upstream_error", str(exc), upstream_status=exc.upstream_status)
    return None


def api_exception_handler(exc, context):
    """DRF EXCEPTION_HANDLER: wrap DRF's own errors (parse error, 405, ...) in the same shape."""
    response = drf_exception_handler(exc, context)
    if response is not None:
        detail = response.data.get("detail", response.data) if isinstance(response.data, dict) else response.data
        response.data = error_body(getattr(exc, "default_code", "error"), str(detail))
    return response
