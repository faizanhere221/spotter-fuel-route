from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Place, Station, stations_version


@api_view(["GET"])
def health(request):
    return Response({
        "ok": True,
        "stations": Station.objects.count(),
        "stations_geocoded": Station.objects.filter(lat__isnull=False).count(),
        "places": Place.objects.count(),
        "stations_version": stations_version(),
    })
