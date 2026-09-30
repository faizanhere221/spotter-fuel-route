from urllib.parse import urlencode

from django.http import JsonResponse
from django.shortcuts import render
from django.urls import reverse
from rest_framework.decorators import api_view
from rest_framework.response import Response
from rest_framework.views import APIView

from .errors import domain_error, error_body
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
                       "start_fuel_gallons": f"{data['start_fuel_gallons']:g}"})
    return request.build_absolute_uri(f"{reverse('route-map')}?{query}")


def run_planner(data):
    """(PlanResult, None) or (None, (status, error body))."""
    try:
        return plan_route(data["start"], data["finish"], data["start_fuel_gallons"]), None
    except Exception as exc:
        mapped = domain_error(exc)
        if mapped is None:
            raise
        return None, mapped


def invalid_request(serializer):
    return 400, error_body("invalid_request", "Invalid request.", fields=serializer.errors)


class RouteView(APIView):
    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        if not serializer.is_valid():
            status, body = invalid_request(serializer)
            return Response(body, status=status)
        result, error = run_planner(serializer.validated_data)
        if error:
            return Response(error[1], status=error[0])
        return Response(result.as_response(map_url_for(request, serializer.validated_data)))


def route_map(request):
    serializer = RouteRequestSerializer(data=request.GET)
    if not serializer.is_valid():
        status, body = invalid_request(serializer)
        return JsonResponse(body, status=status)
    result, error = run_planner(serializer.validated_data)
    if error:
        return JsonResponse(error[1], status=error[0])
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
