from django.db import models


class Place(models.Model):
    """US populated place from GeoNames (data/us_places.csv), used for offline geocoding."""

    name = models.CharField(max_length=200)
    state = models.CharField(max_length=2)
    lat = models.FloatField()
    lng = models.FloatField()
    population = models.PositiveIntegerField(default=0)
    name_norm = models.CharField(max_length=200)
    name_nospace = models.CharField(max_length=200)

    class Meta:
        indexes = [
            models.Index(fields=["state", "name_norm"]),
            models.Index(fields=["state", "name_nospace"]),
        ]

    def __str__(self):
        return f"{self.name}, {self.state}"


class Station(models.Model):
    """Truck stop from the fuel price file, deduplicated by OPIS ID (lowest price kept)."""

    GEOCODE_SOURCES = [
        ("place", "Place exact"),
        ("place_suffix", "Place, suffix stripped"),
        ("place_nospace", "Place, spaces ignored"),
    ]

    external_id = models.PositiveIntegerField(unique=True, help_text="OPIS Truckstop ID")
    name = models.CharField(max_length=200)
    address = models.CharField(max_length=300)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField()
    # Source prices have up to 8 decimal places and one integer digit.
    price = models.DecimalField(max_digits=10, decimal_places=8)
    lat = models.FloatField(null=True, blank=True)
    lng = models.FloatField(null=True, blank=True)
    geocode_source = models.CharField(max_length=20, choices=GEOCODE_SOURCES, null=True, blank=True)
    imported_at = models.DateTimeField()

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state})"
