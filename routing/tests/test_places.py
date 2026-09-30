import csv
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from routing.models import Place
from routing.services.places import LocationError, PlaceIndex, normalize, nospace, resolve_point

# Resolver tests run against the real GeoNames names from data/us_places.csv, restricted to
# rows whose name contains one of these tokens (keeps real distractors like "New York Mills",
# "East Chicago" or "Washington" in other states).
TOKENS = ("new york", "los angeles", "louis", "washington", "chicago", "big cabin", "houston")


def load_real_places():
    path = Path(settings.BASE_DIR) / "data" / "us_places.csv"
    rows = []
    with path.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            n = normalize(r["name"])
            if any(t in n for t in TOKENS):
                rows.append(Place(name=r["name"], state=r["state"], lat=float(r["lat"]), lng=float(r["lng"]),
                                  population=int(r["population"]), name_norm=n, name_nospace=nospace(n)))
    Place.objects.bulk_create(rows)


class NormalizeTests(SimpleTestCase):
    def test_cases(self):
        cases = {
            "St. Louis": "saint louis",
            "Saint Louis": "saint louis",
            "Sault Ste. Marie": "sault saint marie",
            "Ste Genevieve": "saint genevieve",
            "Ft Worth": "fort worth",
            "Mt. Vernon": "mount vernon",
            "  NEW   york ": "new york",
            "Coeur d'Alene": "coeur dalene",
            "Española": "espanola",
            "S Coffeyville": "south coffeyville",
            "Winston-Salem": "winston salem",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), expected)

    def test_nospace_joins_prefixes(self):
        self.assertEqual(nospace(normalize("Mc Calla")), nospace(normalize("McCalla")))
        self.assertEqual(nospace(normalize("De Forest")), "deforest")


class PlaceIndexTests(SimpleTestCase):
    def test_highest_population_wins_and_passes_in_order(self):
        places = [
            Place(name="Springfield", state="IL", lat=1, lng=1, population=10, name_norm="springfield", name_nospace="springfield"),
            Place(name="Springfield", state="IL", lat=2, lng=2, population=100000, name_norm="springfield", name_nospace="springfield"),
            Place(name="McCalla", state="AL", lat=3, lng=3, population=5, name_norm="mccalla", name_nospace="mccalla"),
        ]
        index = PlaceIndex(places)
        self.assertEqual(index.match("Springfield", "IL")[0].lat, 2)
        self.assertEqual(index.match("Mc Calla", "AL")[1], "place_nospace")
        self.assertEqual(index.match("Springfield", "MO"), (None, None))


class ResolverRequiredInputsTests(TestCase):
    """The inputs the brief requires, against real GeoNames names."""

    @classmethod
    def setUpTestData(cls):
        load_real_places()

    def assertResolves(self, query, label, lat, lng, source="place"):
        r = resolve_point(query)
        self.assertEqual(r.label, label)
        self.assertAlmostEqual(r.lat, lat, places=2)
        self.assertAlmostEqual(r.lng, lng, places=2)
        self.assertEqual(r.source, source)

    def test_new_york(self):
        self.assertResolves("New York, NY", "New York City, NY", 40.71, -74.01)  # via ALIASES

    def test_los_angeles(self):
        self.assertResolves("Los Angeles, CA", "Los Angeles, CA", 34.05, -118.24)

    def test_st_louis(self):
        self.assertResolves("St. Louis, MO", "St. Louis, MO", 38.63, -90.20)

    def test_saint_louis(self):
        self.assertResolves("Saint Louis, MO", "St. Louis, MO", 38.63, -90.20)

    def test_washington_dc(self):
        self.assertResolves("Washington, DC", "Washington, DC", 38.90, -77.04)

    def test_chicago(self):
        self.assertResolves("Chicago, IL", "Chicago, IL", 41.85, -87.65)

    def test_big_cabin(self):
        self.assertResolves("Big Cabin, OK", "Big Cabin, OK", 36.54, -95.22)

    def test_coordinates(self):
        self.assertResolves("40.7128,-74.0060", "40.7128,-74.0060", 40.7128, -74.0060, source="coordinates")

    def test_full_state_names(self):
        self.assertResolves("Saint Louis, Missouri", "St. Louis, MO", 38.63, -90.20)
        self.assertResolves("Washington, District of Columbia", "Washington, DC", 38.90, -77.04)
        self.assertResolves("houston,  texas", "Houston, TX", 29.76, -95.36)


class ResolverErrorsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        load_real_places()

    def test_coordinates_outside_us_rejected(self):
        # London, Mexico City, Edmonton, Havana, Vancouver-north (49.5)
        for q in ("51.5074,-0.1278", "19.4326,-99.1332", "53.5461,-113.4938", "23.1136,-82.3666", "49.5,-123.1"):
            with self.subTest(q=q), self.assertRaisesMessage(LocationError, "outside the US"):
                resolve_point(q)

    def test_bounding_box_limitation_documented(self):
        # Ottawa lies inside the coarse lower-48 box; accepted here by design (see US_BOUNDS).
        self.assertEqual(resolve_point("45.4215,-75.6972").source, "coordinates")

    def test_alaska_and_hawaii_coordinates_accepted(self):
        self.assertEqual(resolve_point("61.2181,-149.9003").source, "coordinates")  # Anchorage
        self.assertEqual(resolve_point("21.3069,-157.8583").source, "coordinates")  # Honolulu

    def test_invalid_coordinates(self):
        with self.assertRaisesMessage(LocationError, "Invalid coordinates"):
            resolve_point("95.0,-74.0")

    def test_bad_formats(self):
        for q in ("", "Chicago", "Chicago, ", "Toronto, ON", "Chicago, Narnia"):
            with self.subTest(q=q), self.assertRaises(LocationError):
                resolve_point(q)

    def test_unknown_place_without_fallback(self):
        with self.assertRaisesMessage(LocationError, "Place not found"):
            resolve_point("Nowhereville, KS")

    def test_fallback_used_only_on_miss(self):
        calls = []

        def geocoder(text):
            calls.append(text)
            return 38.0, -97.0, "Nowhereville, KS, USA"

        r = resolve_point("Nowhereville, KS", geocoder=geocoder)
        self.assertEqual((r.source, r.label), ("ors_geocode", "Nowhereville, KS, USA"))
        resolve_point("Chicago, IL", geocoder=geocoder)
        self.assertEqual(calls, ["Nowhereville, KS"])

    def test_fallback_result_outside_us_rejected(self):
        with self.assertRaisesMessage(LocationError, "outside the US"):
            resolve_point("Nowhereville, KS", geocoder=lambda t: (51.5, -0.12, "London"))

    def test_fallback_no_result(self):
        with self.assertRaisesMessage(LocationError, "Place not found"):
            resolve_point("Nowhereville, KS", geocoder=lambda t: None)
