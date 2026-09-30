import csv
import shutil
import tempfile
import zipfile
from decimal import Decimal
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from routing.management.commands.load_places import CSV_HEADER, trim_geonames
from routing.models import Place, Station, stations_version

FIXTURES = Path(__file__).parent / "fixtures"


def run(cmd, *args):
    out = StringIO()
    call_command(cmd, *args, stdout=out)
    return out.getvalue()


class LoadPlacesTests(TestCase):
    def test_loads_csv_and_normalises(self):
        output = run("load_places", "--csv", str(FIXTURES / "places_small.csv"))
        self.assertEqual(Place.objects.count(), 6)
        self.assertIn("inserted 6", output)
        p = Place.objects.get(name="McCalla")
        self.assertEqual((p.name_norm, p.name_nospace), ("mccalla", "mccalla"))

    def test_rerun_is_idempotent(self):
        run("load_places", "--csv", str(FIXTURES / "places_small.csv"))
        output = run("load_places", "--csv", str(FIXTURES / "places_small.csv"))
        self.assertEqual(Place.objects.count(), 6)
        self.assertIn("deleted 6, inserted 6", output)

    def test_trim_geonames_keeps_only_current_populated_places_in_states(self):
        with (FIXTURES / "geonames_small.txt").open(encoding="utf-8") as f:
            rows = trim_geonames(f)
        # Drops PPLH (historical), class H (stream) and admin1 "00"; sorted by state then name.
        self.assertEqual(rows, [
            ["Big Cabin", "OK", "36.53787", "-95.22136", "262"],
            ["Tomah", "WI", "43.97858", "-90.50402", "9000"],
        ])

    def test_download_mode_uses_cached_zip_and_regenerates_csv(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "data" / "geonames").mkdir(parents=True)
        with zipfile.ZipFile(tmp / "data" / "geonames" / "US.zip", "w") as zf:
            zf.write(FIXTURES / "geonames_small.txt", "US.txt")
        with override_settings(BASE_DIR=tmp):
            output = run("load_places", "--download")  # cached zip -> no network
        self.assertIn("Using cached", output)
        with (tmp / "data" / "us_places.csv").open(encoding="utf-8", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], CSV_HEADER)
        self.assertEqual(len(rows), 3)
        self.assertEqual(Place.objects.count(), 2)


class LoadStationsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("load_places", "--csv", str(FIXTURES / "places_small.csv"), stdout=StringIO())

    def load(self):
        return run("load_stations", "--csv", str(FIXTURES / "fuel_small.csv"))

    def test_skips_canada_and_counts_it(self):
        output = self.load()
        self.assertFalse(Station.objects.filter(state__in=["AB", "ON"]).exists())
        self.assertIn("skipped non-US: 2 rows {'AB': 1, 'ON': 1}", output)

    def test_dedupe_keeps_lowest_price_and_that_rows_name(self):
        output = self.load()
        self.assertEqual(Station.objects.count(), 6)  # IDs 7, 9, 20, 30, 40, 50
        s = Station.objects.get(external_id=20)
        self.assertEqual(s.price, Decimal("3.799"))
        self.assertEqual(s.name, "PILOT #1243")
        self.assertIn("unique OPIS IDs: 6 (duplicates collapsed: 3)", output)
        self.assertIn("'price': 1", output)
        self.assertIn("'Truckstop Name': 1", output)

    def test_prices_stored_with_full_precision(self):
        self.load()
        self.assertEqual(Station.objects.get(external_id=7).price, Decimal("3.00733333"))

    def test_geocoding_passes_and_unmatched_null_coords(self):
        output = self.load()
        big_cabin = Station.objects.get(external_id=7)
        self.assertEqual((big_cabin.lat, big_cabin.lng, big_cabin.geocode_source), (36.53787, -95.22136, "place"))
        mccalla = Station.objects.get(external_id=30)
        self.assertEqual(mccalla.geocode_source, "place_nospace")
        springfield = Station.objects.get(external_id=50)
        self.assertEqual(springfield.lat, 39.80172)  # highest-population Springfield, IL
        nowhere = Station.objects.get(external_id=40)
        self.assertIsNone(nowhere.lat)
        self.assertIsNone(nowhere.lng)
        self.assertIsNone(nowhere.geocode_source)
        self.assertIn("unmatched: 1 stations in 1 (city, state) pairs", output)
        self.assertIn("Willow Beach, AZ (1)", output)

    def test_rerun_is_idempotent_and_bumps_version(self):
        self.load()
        first = stations_version()
        snapshot = list(Station.objects.order_by("external_id").values_list("external_id", "price", "lat"))
        output = self.load()
        self.assertEqual(
            list(Station.objects.order_by("external_id").values_list("external_id", "price", "lat")), snapshot)
        self.assertIn("deleted 6, inserted 6", output)
        self.assertNotEqual(stations_version(), first)

    def test_requires_places(self):
        Place.objects.all().delete()
        with self.assertRaisesMessage(CommandError, "load_places"):
            self.load()
