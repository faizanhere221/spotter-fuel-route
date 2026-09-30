from django.db.models import Count, Max
from rest_framework.decorators import api_view
from rest_framework.response import Response

from .models import Place, Station


def stations_version():
    agg = Station.objects.aggregate(n=Count("id"), last=Max("imported_at"))
    return f"{agg['n']}:{agg['last'].isoformat() if agg['last'] else ''}"


@api_view(["GET"])
def health(request):
    return Response({
        "ok": True,
        "stations": Station.objects.count(),
        "stations_geocoded": Station.objects.filter(lat__isnull=False).count(),
        "places": Place.objects.count(),
        "stations_version": stations_version(),
    })
