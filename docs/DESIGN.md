# Design

Cheapest fuel stops for a US truck route: 50 gal tank, 10 mpg (500 mi range), start and finish
given as `"City, ST"` or `"lat,lng"`. One Django project (`fuelroute`), one app (`routing`).

## Layout

```
routing/
  models.py                 Place (GeoNames), Station (fuel file), stations_version()
  serializers.py            request validation; start_fuel_gallons -> 3-dp Decimal
  errors.py                 ONE exception -> (status, error code) mapping, used by both views
  views.py                  POST /api/route/, GET /api/route/map/, GET /api/health/, JSON 404
  services/places.py        normalize(), 3-pass matcher, resolve_point()
  services/ors.py           the only module making request-time HTTP calls
  services/corridor.py      StationIndex (in-memory), resample + cKDTree corridor search
  services/optimizer.py     greedy cheapest-ahead plan (pure, no Django)
  services/planner.py       plan_route(): resolve -> route -> corridor -> optimize, caching, timings
  management/commands/      load_places, load_stations
  templates/routing/        map.html (Leaflet, data via json_script), map_error.html
data/fuel-prices.csv        provided price file (converted from xlsx, prices exact)
data/us_places.csv          GeoNames US populated places, trimmed (CC BY 4.0)
```

## Data

- **Fuel file:** 8,151 rows.
  - 620 Canadian rows are skipped.
  - The 7,531 US rows collapse to 6,626 stations by OPIS ID, keeping the lowest listed price and that row's name.
  - Prices have up to 8 dp and are stored exactly (`DecimalField(10, 8)`).
- **Geocoding:** offline, by (city, state) against GeoNames populated places (183,965 rows).
  - `normalize()` handles accents, punctuation, St/Ste/Saint, Ft, Mt and N/S/E/W.
  - The match passes are exact, then suffix-stripped (city/town/village), then spaces ignored.
  - If several places match, the one with the highest population wins.
  - Result: 6,608 of 6,626 stations matched (99.7%). Unmatched stations keep NULL coordinates and are excluded.
  - Explicit alias: "New York, NY" → GeoNames "New York City".
- **`stations_version`:** row count plus the latest `imported_at`. Only `load_stations` changes it.

## Request flow (`plan_route`)

1. **Resolve** start and finish: `lat,lng` inside the US bounding boxes, or a Place lookup.
   - On a miss, fall back to the ORS geocoder (`/pelias/v1/search`, +1 call), cached under `geo:v1`.
   - A city that normalizes to nothing is rejected (400) without a lookup.
2. **Plan cache:** `plan:v1:{route_hash}:{stations_version}:{tank}:{mpg}:{start_fuel}:{corridor}`.
   - `route_hash` = sha1(profile + both coordinates at 4 dp).
   - `start_fuel` is the serializer's 3-dp Decimal, the same value used everywhere else.
3. **Route cache** `route:v1:{route_hash}`, or ONE directions call.
   - `POST /openrouteservice/v2/directions/driving-hgv/geojson` with `instructions: false` and `geometry_simplify: true`.
   - The response is fully validated before it's cached; a malformed one raises 502 and is never cached.
4. **Corridor:** the in-memory `StationIndex` holds ids, lat/lng, prices and name/address/city/state, and is rebuilt only when `stations_version` changes.
   - Resample the route to 0.5 mi.
   - Project to unit-sphere xyz.
   - Run a bounding-box prefilter, then a `cKDTree` nearest-route-point query.
   - Keep stations with offset ≤ 5 mi. Mile marker = cumulative miles at that point.
5. **Optimize** (`plan_fuel`). Nodes are the start (can't buy), the stations, and the finish (a virtual station at price 0).
   - If a cheaper node is within 500 mi, buy just enough to reach the nearest one.
   - Otherwise fill the tank and go to the cheapest node in range (tie → farther).
   - Unreachable gaps raise `UnreachableError`.
   - The start tank (default 50 gal) is free.
   - Gallons are 3 dp. `cost = round_half_up(gallons × full price, cents)`, and the total is the sum of stop costs.
6. **Build the response** from the index only (no DB lookup) and store it in the plan cache.
   - Each stage is timed and logged at INFO (resolve / route / corridor / optimize / total).

Happy path: 1 external call. Repeat: 0. Worst case: 3 (two geocodes plus directions).

## Caches (LocMem; use Redis with several workers)

| key | TTL |
|---|---|
| `geo:v1:{sha1(normalized query)}` | 30 days found, 1 day not found |
| `route:v1:{route_hash}` | 7 days |
| `plan:v1:…` | 1 day |

## API

- **`POST /api/route/`** `{"start", "finish", "start_fuel_gallons"?}`.
  - Response keys, in order: `summary`, `fuel_stops`, `start`, `finish`, `assumptions`, `external_api_calls`, `cached`, `timings_ms`, `map_url`, `warnings`, `route` (a GeoJSON Feature, last).
  - Decimal values (money, gallons, prices) are JSON strings. `price_per_gallon` is the full stored precision, so `gallons × price` reproduces `cost`.
- **`GET /api/route/map/`:** the same `plan_route()` call, rendered as Leaflet HTML.
  - Errors render `map_error.html` with the same status and message.
  - Methods other than GET get 405.
- **`GET /api/health/`:** row counts. Unknown `/api/...` paths get a JSON 404.

## Errors (`routing/errors.py`, one mapping)

| exception | status | code |
|---|---|---|
| serializer `ValidationError` | 400 | `invalid_request` (+ `fields`) |
| `LocationError` | 400 | `invalid_location` |
| `UnroutablePointError` (ORS 2010) | 400 | `unroutable_point` (+ `point`: start/finish) |
| `UnreachableError` | 422 | `unreachable` (+ `gap_miles`, `from_mile`, `to_mile`) |
| `NoRouteError` (ORS 2009) | 422 | `no_route` |
| `ORSQuotaError` (ORS 403/429) | 503 | `upstream_quota` |
| `ORSUpstreamError` (other, malformed, timeout) | 502 | `upstream_error` |
| DRF errors (parse, 405, 415) | as DRF | DRF `default_code` |
| anything else | 500 | `internal_error` (logged with traceback, generic message) |

## Tests

`manage.py test routing` covers:
- **import commands:** dedupe, the Canada skip, NULL coordinates, idempotent re-runs
- **resolver:** the required inputs against real GeoNames names
- **ORS client:** errors mapped by code, response validation, the key never logged
- **corridor**
- **optimizer:** the listed cases, plus a brute-force comparison over 200 random cases
- **API:** caching and call counts, error shapes, key order, string types, the XSS escape in the map

A custom test runner blocks real ORS HTTP.
