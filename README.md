# Fuel Route Optimizer

Django + DRF API that plans the cheapest fuel stops for a US truck route (50 gal tank, 10 mpg),
using OpenRouteService for the route and the provided OPIS fuel price file. Design: [PLAN.md](PLAN.md).

> Work in progress — the route endpoint is not built yet.

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

## Known limitations

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
