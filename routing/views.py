from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import error_body, error_for
from .models import Place, Station, stations_version
from .serializers import RouteRequestSerializer
from .services.planner import plan_route


@api_view(["GET"])
def health(request):
    return Response({
        "ok": True,
        "stations": Station.objects.count(),
        "stations_geocoded": Station.objects.filter(lat__isnull=False).count(),
        "places": Place.objects.count(),
        "stations_version": stations_version(),
    })


def map_url_for(request, data):
    query = urlencode({"start": data["start"], "finish": data["finish"],
                       "start_fuel_gallons": str(data["start_fuel_gallons"])})
    return request.build_absolute_uri(f"{reverse('route-map')}?{query}")


def run_planner(data):
    """Plan from validated serializer data; raises the planner's typed exceptions."""
    return plan_route(data["start"], data["finish"], data["start_fuel_gallons"])


class RouteView(APIView):
    """POST /api/route/. Errors propagate to routing.errors.api_exception_handler."""

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        result = run_planner(serializer.validated_data)
        return Response(result.as_response(map_url_for(request, serializer.validated_data)))


@csrf_exempt  # GET-only page; exempt so a POST gets 405 from require_GET, not the CSRF 403 page
@require_GET
def route_map(request):
    """GET /api/route/map/: the same plan on a Leaflet map. Errors render an HTML error page."""
    try:
        serializer = RouteRequestSerializer(data=request.GET)
        serializer.is_valid(raise_exception=True)
        result = run_planner(serializer.validated_data)
    except Exception as exc:  # noqa: BLE001 — error_for() maps known types, logs the rest as 500
        status, body = error_for(exc)
        return render(request, "routing/map_error.html", {"status": status, "error": body["error"]},
                      status=status)
    plan = result.plan
    map_data = {
        "route": plan["route"]["geometry"]["coordinates"],
        "start": result.start,
        "finish": result.finish,
        "stops": plan["fuel_stops"],
        "summary": plan["summary"],
    }
    response = render(request, "routing/map.html", {"map_data": map_data, "summary": plan["summary"],
                                                     "start": result.start, "finish": result.finish})
    response["X-External-API-Calls"] = str(result.external_api_calls)
    response["X-Cached"] = str(result.cached).lower()
    return response


def api_not_found(request, path=""):
    """Catch-all for unknown /api/... paths, in the API error shape (works with DEBUG on or off)."""
    return JsonResponse(error_body("not_found", f"No API endpoint at {request.path}."), status=404)
