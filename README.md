# Fuel Route Optimizer

Django + DRF API that plans the cheapest fuel stops for a US truck route (50 gal tank, 10 mpg),
using OpenRouteService for the route and the provided OPIS fuel price file. Design: [PLAN.md](PLAN.md).

## API

`POST /api/route/` with `{"start": "New York, NY", "finish": "Los Angeles, CA", "start_fuel_gallons": 50}`

- `start` / `finish`: `"City, ST"`, `"City, State Name"` or `"lat,lng"`; `start_fuel_gallons`
  optional, 0–50, default 50.
- Response: `summary`, `fuel_stops`, `start`, `finish`, `assumptions`, `external_api_calls`,
  `cached`, `timings_ms`, `map_url`, `warnings`, `route` (GeoJSON Feature, last).
- Errors: `{"error": {"code", "message", ...}}` — 400 invalid input / unknown place / outside the
  US, 422 unreachable (`gap_miles`, `from_mile`, `to_mile`), 502 upstream error, 503 upstream quota.
- A new route costs 1 external call (ORS directions); a repeat costs 0 (plan cache). Unknown
  places fall back to the ORS geocoder (+1 call each; cached 30 days, "not found" 1 day).

`GET /api/route/map/?start=&finish=&start_fuel_gallons=` renders the same plan on a Leaflet map
(the POST response's `map_url`). `GET /api/health/` returns row counts.

## Setup

```bash
python -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows; venv/bin/python elsewhere
cp .env.example .env                                         # then set ORS_API_KEY
venv/Scripts/python.exe manage.py migrate
venv/Scripts/python.exe manage.py load_places                # data/us_places.csv -> Place (offline)
venv/Scripts/python.exe manage.py load_stations              # data/fuel-prices.csv -> Station
venv/Scripts/python.exe manage.py test
```

`load_places --download` regenerates `data/us_places.csv` from GeoNames (71 MB download, cached in
`data/geonames/`, gitignored).

## Assumptions

- Duplicate OPIS IDs with different prices → lowest listed price kept (along with that row's name).
- Canadian stations in the price file are skipped; routing is US-only.
- Stations are located at their town's centroid (the file only has highway-exit addresses);
  stations whose town isn't found in GeoNames (0.3%) are excluded.
- The truck starts with a full tank (50 gal) that is not charged, unless `start_fuel_gallons` says otherwise.
- The optimizer ignores the detour from the route to a station inside the corridor.
- Optimizer minimizes total fuel cost only; it doesn't model time or overhead per stop, so it may
  make small top-ups when a slightly cheaper station is ahead. A per-stop cost would need DP over
  (station, fuel level).

## Known limitations

- ORS host: `api.openrouteservice.org` is deprecated in favour of `api.heigit.org`
  ([announcement](https://ask.openrouteservice.org/t/deprecating-api-openrouteservice-org-in-favour-of-api-heigit-org/7912)).
  Default `ORS_BASE_URL=https://api.heigit.org`; directions are under `/openrouteservice/v2/...`,
  geocoding under `/pelias/v1/...`.
- Caches are LocMem (per process); use a shared cache (e.g. Redis) with several workers.

- Coordinate input (`"lat,lng"`) is checked against coarse US bounding boxes (lower 48, Alaska,
  Hawaii) only, so points just across the northern border (e.g. Ottawa, 45.42,-75.70) pass the
  check. `"City, ST"` input is US-only: it must name a US state and match a US place.
- `"New York, NY"` resolves through one explicit alias to GeoNames' "New York City"
  (`ALIASES` in `routing/services/places.py`).

## Data attribution

- `data/us_places.csv` is derived from [GeoNames](https://www.geonames.org/) (`US.zip`, populated
  places), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
- Routes: © [openrouteservice.org](https://openrouteservice.org/) by HeiGIT | Map data ©
  [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
