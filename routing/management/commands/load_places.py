"""Load US populated places into the Place table.

Default: import data/us_places.csv (committed, no network).
--download: fetch GeoNames US.zip (once, cached in data/geonames/), regenerate data/us_places.csv,
then import it. --refresh forces a new download.
"""
import csv
import io
import time
import zipfile
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import Place
from routing.services.places import US_STATES, normalize, nospace

GEONAMES_URL = "https://download.geonames.org/export/dump/US.zip"
EXCLUDED_FEATURE_CODES = {"PPLH", "PPLQ", "PPLW", "PPLCH"}  # historical, abandoned, destroyed
CSV_HEADER = ["name", "state", "lat", "lng", "population"]
BATCH_SIZE = 5000


def data_dir():
    return Path(settings.BASE_DIR) / "data"


def trim_geonames(lines):
    """GeoNames TSV lines -> sorted [name, state, lat, lng, population] rows of populated places."""
    rows = []
    for r in csv.reader(lines, delimiter="\t", quoting=csv.QUOTE_NONE):
        # 6 feature class, 7 feature code, 8 country, 10 admin1 (US: 2-letter state), 14 population
        if len(r) < 15 or r[6] != "P" or r[7] in EXCLUDED_FEATURE_CODES or r[8] != "US":
            continue
        if r[10] not in US_STATES:
            continue
        rows.append([r[1], r[10], r[4], r[5], r[14] or "0"])
    rows.sort(key=lambda r: (r[1], r[0], r[2], r[3]))
    return rows


class Command(BaseCommand):
    help = "Load US places (GeoNames, CC BY 4.0) into the Place table."

    def add_arguments(self, parser):
        parser.add_argument("--download", action="store_true", help="Regenerate data/us_places.csv from GeoNames.")
        parser.add_argument("--refresh", action="store_true", help="With --download: re-fetch US.zip even if cached.")
        parser.add_argument("--csv", default=None, help="Path to places CSV (default data/us_places.csv).")

    def handle(self, *args, **opts):
        started = time.monotonic()
        csv_path = Path(opts["csv"]) if opts["csv"] else data_dir() / "us_places.csv"
        if opts["download"]:
            self.regenerate_csv(csv_path, refresh=opts["refresh"])
        if not csv_path.exists():
            raise CommandError(f"{csv_path} not found; run with --download.")

        with csv_path.open(encoding="utf-8", newline="") as f:
            reader = csv.reader(f)
            if next(reader, None) != CSV_HEADER:
                raise CommandError(f"{csv_path}: expected header {CSV_HEADER}")
            places = []
            for name, state, lat, lng, pop in reader:
                n = normalize(name)
                places.append(Place(name=name, state=state, lat=float(lat), lng=float(lng),
                                    population=int(pop), name_norm=n, name_nospace=nospace(n)))

        with transaction.atomic():
            deleted, _ = Place.objects.all().delete()
            Place.objects.bulk_create(places, batch_size=BATCH_SIZE)

        self.stdout.write(
            f"load_places: read {len(places)} rows from {csv_path.name}; "
            f"deleted {deleted}, inserted {len(places)}; "
            f"{len({p.state for p in places})} states; {time.monotonic() - started:.1f}s"
        )

    def regenerate_csv(self, csv_path, refresh):
        zip_path = data_dir() / "geonames" / "US.zip"
        if refresh or not zip_path.exists():
            zip_path.parent.mkdir(parents=True, exist_ok=True)
            self.stdout.write(f"Downloading {GEONAMES_URL} ...")
            with requests.get(GEONAMES_URL, stream=True, timeout=(10, 120)) as resp:
                resp.raise_for_status()
                tmp = zip_path.with_suffix(".part")
                with tmp.open("wb") as f:
                    for chunk in resp.iter_content(1 << 20):
                        f.write(chunk)
                tmp.replace(zip_path)
        else:
            self.stdout.write(f"Using cached {zip_path} (pass --refresh to re-download)")

        with zipfile.ZipFile(zip_path) as zf, zf.open("US.txt") as raw:
            rows = trim_geonames(io.TextIOWrapper(raw, encoding="utf-8", newline=""))
        with csv_path.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(CSV_HEADER)
            w.writerows(rows)
        self.stdout.write(f"Wrote {csv_path} ({len(rows)} rows, {csv_path.stat().st_size / 1e6:.2f} MB)")
