from django.conf import settings
from rest_framework import serializers


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200)
    finish = serializers.CharField(max_length=200)
    start_fuel_gallons = serializers.FloatField(
        required=False, min_value=0, max_value=settings.TANK_GALLONS, default=settings.START_FUEL_GALLONS)
