"""Import data/fuel-prices.csv into Station: skip non-US, dedupe by OPIS ID, geocode offline."""
import csv
import time
from collections import Counter
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from routing.models import Place, Station
from routing.services.places import US_STATES, PlaceIndex

COLUMNS = ["OPIS Truckstop ID", "Truckstop Name", "Address", "City", "State", "Rack ID", "Retail Price"]
BATCH_SIZE = 2000


def read_rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise CommandError(f"{path}: missing columns {sorted(missing)}")
        return [{k: (v or "").strip() for k, v in row.items()} for row in reader]


def dedupe(rows):
    """One row per OPIS ID: the lowest price wins (first seen on ties). Returns (rows, conflicts)."""
    best, seen = {}, {}
    for row in rows:
        oid = int(row["OPIS Truckstop ID"])
        row["price"] = Decimal(row["Retail Price"])
        seen.setdefault(oid, []).append(row)
        if oid not in best or row["price"] < best[oid]["price"]:
            best[oid] = row
    conflicts = Counter()
    examples = {}
    for oid, group in seen.items():
        for field in ("price", "Truckstop Name", "Address", "City", "State", "Rack ID"):
            if len({r[field] for r in group}) > 1:
                conflicts[field] += 1
                examples.setdefault(field, (oid, sorted({str(r[field]) for r in group})))
    return list(best.values()), conflicts, examples


class Command(BaseCommand):
    help = "Import fuel prices into Station (US only, deduped by OPIS ID, geocoded via Place)."

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=None, help="Path to fuel CSV (default data/fuel-prices.csv).")

    def handle(self, *args, **opts):
        started = time.monotonic()
        path = Path(opts["csv"]) if opts["csv"] else Path(settings.BASE_DIR) / "data" / "fuel-prices.csv"
        if not Place.objects.exists():
            raise CommandError("Place table is empty; run `manage.py load_places` first.")

        rows = read_rows(path)
        us_rows = [r for r in rows if r["State"].upper() in US_STATES]
        skipped = Counter(r["State"] for r in rows if r["State"].upper() not in US_STATES)
        unique, conflicts, examples = dedupe(us_rows)

        index = PlaceIndex(Place.objects.only("name", "state", "lat", "lng", "population",
                                              "name_norm", "name_nospace"))
        now = timezone.now()
        stations, sources, unmatched = [], Counter(), Counter()
        for r in unique:
            state = r["State"].upper()
            place, source = index.match(r["City"], state)
            sources[source] += 1
            if place is None:
                unmatched[(r["City"], state)] += 1
            stations.append(Station(
                external_id=int(r["OPIS Truckstop ID"]), name=r["Truckstop Name"], address=r["Address"],
                city=r["City"], state=state, rack_id=int(r["Rack ID"]), price=r["price"],
                lat=place.lat if place else None, lng=place.lng if place else None,
                geocode_source=source, imported_at=now,
            ))

        with transaction.atomic():
            deleted, _ = Station.objects.all().delete()
            Station.objects.bulk_create(stations, batch_size=BATCH_SIZE)

        matched = len(stations) - sources[None]
        out = self.stdout.write
        out(f"load_stations: read {len(rows)} rows from {path.name}")
        out(f"  skipped non-US: {sum(skipped.values())} rows {dict(sorted(skipped.items()))}")
        out(f"  US rows: {len(us_rows)}; unique OPIS IDs: {len(unique)} "
            f"(duplicates collapsed: {len(us_rows) - len(unique)})")
        out(f"  conflicts among duplicate IDs: {dict(conflicts) or 'none'}")
        for field, (oid, vals) in examples.items():
            out(f"    e.g. {field}: ID {oid} -> {vals}")
        out(f"  matched: {matched} ({matched / max(len(stations), 1):.1%}) "
            f"{ {k: v for k, v in sources.items() if k} }")
        out(f"  unmatched: {sources[None]} stations in {len(unmatched)} (city, state) pairs"
            + (":" if unmatched else ""))
        for (city, state), n in sorted(unmatched.items()):
            out(f"    {city}, {state} ({n})")
        out(f"  deleted {deleted}, inserted {len(stations)}; {time.monotonic() - started:.1f}s")
