from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from rest_framework import serializers

FUEL_Q = Decimal("0.001")


def to_fuel_decimal(value):
    """Gallons rounded once to 3 dp; this exact value feeds the cache key, optimizer and output."""
    return Decimal(repr(float(value))).quantize(FUEL_Q, rounding=ROUND_HALF_UP)


class RouteRequestSerializer(serializers.Serializer):
    start = serializers.CharField(max_length=200)
    finish = serializers.CharField(max_length=200)
    # FloatField rejects NaN/inf and out-of-range values; validate_* converts to Decimal.
    start_fuel_gallons = serializers.FloatField(
        required=False, min_value=0, max_value=settings.TANK_GALLONS, default=settings.START_FUEL_GALLONS)

    def validate_start_fuel_gallons(self, value):
        return to_fuel_decimal(value)
