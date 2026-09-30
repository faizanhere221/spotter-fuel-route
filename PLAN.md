# Fuel Route Optimizer — Plan

> Status: **approved decisions applied; scaffold in progress.** All numbers below come from
> `scratch/inspect_fuel.py` and `scratch/match_rate.py` run on `data/fuel-prices.csv` (2026-09-30).

## 0. Stack (verified on PyPI, 2026-09-30)

| Package | Version | Notes |
|---|---|---|
| Python | 3.14.4 | Django 6.1 supports 3.12–3.14 |
| Django | 6.1.1 | |
| djangorestframework | 3.18.1 | `requires django>=5.2`; classifiers list Django 5.2 / 6.0 / 6.1 → compatible |
| requests | 2.34.2 | ORS HTTP client |
| numpy | 2.5.3 | station/route arrays |
| scipy | 1.18.1 | `cKDTree`; binary wheel installs on 3.14 (verified: import + query) |
| python-dotenv | 1.2.3 | loads `.env` |

openpyxl is **not** a runtime dependency. The xlsx was converted once to `data/fuel-prices.csv` by `scratch/convert_xlsx.py`, and that CSV is the committed source.

The ORS key comes from `ORS_API_KEY` (env / `.env`, gitignored). `.env.example` ships with an empty value.

## 1. Layout

```
manage.py
fuelroute/                # Django project (settings, urls, wsgi/asgi)
routing/                  # the single app
  models.py               # Place, Station
  services/
    places.py             # normalisation + offline "City, ST" lookup
    ors.py                # the ONLY module that makes HTTP calls (directions + geocode fallback)
    stations_index.py     # module-level numpy cache of geocoded stations, keyed by stations_version
    corridor.py           # resample route, cKDTree, distance + mile_marker
    optimizer.py          # greedy refuelling (pure functions, no Django)
    planner.py            # orchestrates a request: resolve → route → corridor → optimize, caching, timings
  serializers.py
  views.py                # POST /api/route/, GET /api/route/map/, GET /api/health/
  templates/routing/map.html
  urls.py
  management/commands/
    load_places.py        # GeoNames → data/us_places.csv (once) → Place table
    load_stations.py      # data/fuel-prices.csv → dedupe → geocode via Place → Station table
  tests/
data/
  fuel-prices.csv         # committed (8,151 rows, converted from the provided xlsx)
  us_places.csv           # committed (trimmed GeoNames, 183,980 rows, 6.91 MB, CC BY 4.0)
  geonames/               # gitignored: raw US.zip / US.txt download
```

## 2. The fuel data (measured)

- **Rows:** 8,151, with 0 malformed rows and 0 nulls in any column.
- **Columns:** `OPIS Truckstop ID` (int), `Truckstop Name`, `Address`, `City`, `State`, `Rack ID` (int), `Retail Price`.
- **Addresses** are highway exits (`"I-44, EXIT 283 & US-69"`), not street addresses. Geocoding the address itself isn't practical, so **(City, State)** is the geocoding key.
- **States:** 57 codes. **620 rows (112 unique IDs) are Canadian** (ON 217, AB 180, BC 121, MB 42, SK 36, YT 8, QC 6, NS 6, NB 4). They're skipped at import and counted as `skipped_non_us`, since the gazetteer and routing are US-only.
- **Price:** min 2.6873, max 6.3990, mean 3.4990, median 3.4323.
  - Outliers above $6: 3 rows (Pilot #1194 Phoenix AZ 6.039 ×2, Chevron Jacumba CA 6.399). None are under $2. They're plausible for CA/AZ, so they're kept.
  - **Precision:** at most **8 decimal places** (dp distribution 3: 5,389 · 4: 10 · 6: 3 · 7: 65 · 8: 2,684), with at most 1 integer digit.
  - The conversion kept every price exactly: 0 of 24,457 numeric cells changed value. Excel's raw XML stores 17 significant digits (`3.0073333299999998`), and the CSV holds the shortest string for the same double (`3.00733333`).
- **Duplicates:** 6,738 unique OPIS IDs, of which **678 appear more than once** (2–6 rows each), plus 26 fully identical rows.
  - 597 of them differ in **price**.
  - 227 differ in **name**, only cosmetically (`PILOT TRAVEL CENTER #87` vs `PILOT TRAVEL CENTERS #87`).
  - **0** differ in address, city, state or rack ID.
- **Distinct (city, state) pairs:** 3,893 overall, 3,813 in the US.

## 3. Models

**Place** (offline gazetteer)
- `name`, `state` (2-letter), `lat`, `lng` (float), `population` (int)
- `name_norm` (normalised, see §4) and `name_nospace` (`name_norm` without spaces)
- indexes: `(state, name_norm)`, `(state, name_nospace)`

**Station**
- `external_id`: `PositiveIntegerField(unique=True)`, the OPIS Truckstop ID
- `name`, `address`, `city`, `state`
- `rack_id`: `PositiveIntegerField`
- `price`: `DecimalField(max_digits=10, decimal_places=8)`. That covers the 8 dp in the data with no loss, with headroom to $99.99999999. Values are parsed with `Decimal(csv_string)`, never through float.
- `lat`, `lng`: nullable floats
- `geocode_source`: `"place"` | `"place_suffix"` | `"place_nospace"` | null
- `imported_at`: timestamp shared by the whole import batch
- **No (lat, lng) index:** routing never filters stations in SQL (§7).

**Dedupe by OPIS ID:** keep the row with the **lowest price**, including that row's name. `load_stations` logs the number of IDs with price conflicts (597) and name conflicts (227), plus a few examples. Address, city and state never conflict in this data, but if they ever did, that would be logged too.

**stations_version** = `f"{count}:{max(imported_at).isoformat()}"`. It's one aggregate query, so there's no extra model, and it changes on every re-import.

## 4. Geocoding stations (offline, 0 API calls)

- **Normalisation** (the same for stations, places and user input):
  1. NFKD with accents stripped, then lowercase, `&` → `and`, drop `.` `'` `’`, non-alphanumerics → space, collapse spaces
  2. `\bst\b` / `\bste\b` / `sainte` → `saint`; leading `ft` → `fort`; `\bmt\b` → `mount`; leading `s` / `n` / `e` / `w` → `south` / `north` / `east` / `west`
- **Match passes** on `(state, …)`:
  1. `name_norm` exact
  2. drop a trailing `city` / `town` / `village` and retry
  3. `name_nospace` (handles `Mc Calla` ↔ `McCalla`, `De Forest` ↔ `DeForest`, `La Place` ↔ `LaPlace`)
  - If several places match, the **highest population** wins.
- **Measured match rate:** **6,606 of 6,626 unique US stations = 99.7%** (exact 6,582, nospace 24). The number is re-measured and printed by `load_stations` every run.
- **Unmatched:** 20 stations across 15 (city, state) pairs, e.g. Willow Beach AZ, Willington CT, Sault Sainte Marie MI, Dundee IL, Bronx NY, Jacumba CA.
  - Most are New England "towns" that GeoNames holds as admin areas rather than populated places. The `\bste\b` rule should also recover Sault Ste. Marie.
  - These stations are **stored with NULL lat/lng and excluded from routing**. `load_stations` reports them with a count plus a list.
- **Precision:** each station sits at its town's centroid, not the exit (`station_location_precision: "city centroid"` in the response).
- Station import does the matching in memory: all Places are loaded into two dicts once, so there's no query per station.

## 5. Places dataset

- `load_places --download` fetches GeoNames `US.zip` (71 MB) once into `data/geonames/` (gitignored) and streams `US.txt` (307 MB).
- It keeps feature class `P` except `PPLH` / `PPLQ` / `PPLW` / `PPLCH` (historical, abandoned, destroyed) and writes **`data/us_places.csv`** (`name,state,lat,lng,population`). That's **183,980 rows, 6.91 MB → committed**, with CC BY 4.0 attribution to GeoNames in the README.
- `load_places` without `--download` just imports the committed CSV into `Place`, so a fresh clone needs no network.

## 6. Start / finish input

- Accept `"City, ST"` (primary) or `"lat,lng"`.
- `"City, ST"` resolves through the same normalisation and match passes against `Place` → **0 external calls**.
- **ORS geocode fallback is ON by default** (`ORS_GEOCODE_FALLBACK=true`): `GET https://api.openrouteservice.org/geocode/search?text=…&boundary.country=US&size=1`.
  - That's **+1 external call per endpoint that misses**, counted in `external_api_calls`.
  - Fallback results are cached under `geo:v1:{sha1(normalized query)}` for 30 days (§10), so a repeat request with fallback-geocoded input makes **0 external calls**.
- A result outside the US, or not found at all, returns `400`.

## 7. Request flow (`POST /api/route/`)

Each stage is timed and logged at INFO: `resolve`, `route`, `corridor`, `optimize`, `total` (ms).

1. Validate the body (serializer).
2. **resolve**: start and finish each go to the Place DB (0 calls), or ⚡ the ORS geocode fallback (1 call each).
3. Compute `route_hash = sha1(profile | lon1,lat1 | lon2,lat2)` with coordinates rounded to 4 dp. This happens before any directions call.
4. Look up `stations_version` (1 aggregate query). Check the plan cache; on a hit, go to step 9 with `cached: true`.
5. **route**: check the route cache; on a miss, ⚡ **ORS directions** — `POST https://api.openrouteservice.org/v2/directions/driving-hgv/geojson`, header `Authorization: <key>`, body `{"coordinates": [[lon1,lat1],[lon2,lat2]]}`. That's **exactly 1 call**. The response gives the `LineString` coordinates and `summary.distance` in metres.
6. **corridor** (§9) → candidate stations with `distance_mi` and `mile_marker`.
7. **optimize** (§8).
8. Build the response and store it in the plan cache.
9. Return the response with `external_api_calls` = the geocode calls from step 2 + the directions call from step 5.

**Normally 1 external call. A repeat request needs 0. The worst case is 3.**

**Profile:** `driving-hgv` (setting `ORS_PROFILE`). I'll check it with one real call during implementation. If HGV fails (error or no route), the setting falls back to `driving-car`, and I'll note the result in the README. The profile is part of `route_hash`.

## 8. Optimizer (pure functions)

- **Constants** (settings, overridable per request): `TANK_GALLONS = 50`, `MPG = 10` → `RANGE_MILES = 500`.
- **Start fuel:** `START_FUEL_GALLONS = 50` by default, meaning a full tank that is **not charged** (`start_fuel_charged: false`). It can be overridden per request with `start_fuel_gallons` from 0 to 50.
  - `start_fuel_gallons = 0` usually returns **422**: with an empty tank the only reachable point is a station at mile ~0.
  - That only succeeds when a station in the start town projects onto the route's first point. It can happen, because station and start both sit at the town centroid, but you can't count on it.

**Points:** `[start] + stations sorted by (mile_marker, price) + [finish]`
- `start` is at mile 0 and can't be bought from. Its fuel is free and capped at `start_fuel`.
- `finish` is a **virtual station at mile = route distance with price 0**.

**Greedy algorithm:**
```
at point i with fuel f (gallons):
  cap   = start_fuel if i is start else TANK
  reach = points ahead with mile - mile_i <= cap * MPG
  if reach is empty: raise Unreachable(gap from i to the next point)
  j = nearest point in reach with price < price_i          # finish (price 0) counts
  if j:  buy max(0, (mile_j - mile_i)/MPG - f) at i; go to j
  else:  fill to cap at i; go to the cheapest point in reach (ties → farthest)
```
- The start is treated as price 0 with supply limited to `start_fuel`, so it never buys. It spends its free fuel first, then moves to the cheapest reachable station.
- Because the finish is price 0, "cheaper station in range → buy just enough" also covers the end of the trip. The truck buys only what it needs to finish and **arrives with ~0 fuel**.
- **Unreachable:** any gap bigger than the reachable range returns **422** with `{"error": "unreachable", "gap_miles", "from", "to"}`. That means any station-to-station gap over 500 mi, or a start-to-first-station gap over `start_fuel × MPG`.
- **Money:**
  - Gallons and prices are kept at full precision as `Decimal`. Miles are floats from the geometry, converted with `Decimal(repr(x))`.
  - Each stop's `cost = (gallons × price).quantize(0.01, ROUND_HALF_UP)`.
  - `total_fuel_cost` = sum of the rounded stop costs, so the total always equals the sum of the displayed stops.
  - Output formatting: `price_per_gallon` 3 dp (ROUND_HALF_UP), `gallons` 3 dp, costs 2 dp. All are strings.

## 9. Corridor: 5 mi (`CORRIDOR_MILES`, overridable per request)

Five miles covers the error from placing stations at the town centroid, which is typically 1–5 mi from the interstate exit for highway towns. A wider corridor admits stations that need real detours, and the optimizer doesn't charge for detours.

- **No per-request SQL for stations.**
  - `stations_index.py` holds a module-level cache: `{version, ids, xyz (N×3 float64), price (Decimal list), meta}`, built from all geocoded stations.
  - It's reloaded only when `stations_version` changes.
- **Per request:**
  1. Resample the route LineString to ~0.5 mi spacing (haversine segment lengths, linear interpolation) and keep **cumulative miles** for every point.
  2. Project the route points and stations to xyz on a sphere of Earth's radius in miles. The one-line comment in code: *"Sphere xyz: Euclidean chord ≈ great-circle distance at corridor scale (error < 0.001 % at 5 mi) and no per-route reference latitude, unlike equirectangular."*
  3. `cKDTree(route_xyz).query(station_xyz, distance_upper_bound=CORRIDOR_MILES)` gives each station's distance to the nearest route point and that point's index. `mile_marker = cum_miles[idx]`.
  4. Keep stations with a finite distance.
- **Error bounds:** with 0.5 mi spacing, both the distance and the mile marker are off by at most ~0.25 mi. That's negligible against the centroid error. There's no point-to-segment math.
- Stations just before the start or after the finish snap to mile 0 or the end. In practice those are in the start or finish town, which is acceptable.

## 10. Caching (Django LocMem only)

| Key | Value | TTL |
|---|---|---|
| `geo:v1:{sha1(normalized query)}` | ORS geocode fallback result (lat, lng, label) | 30 days |
| `route:v1:{route_hash}` | resampled-ready route: coords, total miles | 7 days |
| `plan:v1:{route_hash}:{stations_version}:{tank}:{mpg}:{start_fuel}:{corridor}` | full response body (minus `cached`/`external_api_calls`) | 1 day |

- `route_hash = sha1(f"{profile}|{lon1:.4f},{lat1:.4f}|{lon2:.4f},{lat2:.4f}")`, computed before any ORS call.
- A station re-import changes `stations_version`. That invalidates the plan cache but keeps the route cache, so there are still 0 directions calls.
- The geo key uses the same `normalize()` as the Place lookup, so `"St. Louis, MO"` and `"saint louis, mo"` share one entry.
- **README production note:** LocMem is per-process. With several workers, use Redis (`django.core.cache.backends.redis.RedisCache`) so the caches are shared.

## 11. Endpoints & response shape

### `POST /api/route/`
```json
{ "start": "Dallas, TX", "finish": "Denver, CO", "start_fuel_gallons": 50 }
```
Optional: `start_fuel_gallons` (0–50), `corridor_miles` (0.5–25).

`200`
```json
{
  "start":  {"query": "Dallas, TX", "lat": 32.78, "lng": -96.80, "source": "place"},
  "finish": {"query": "Denver, CO", "lat": 39.74, "lng": -104.98, "source": "place"},
  "route": {
    "distance_miles": 792.4,
    "geometry": {"type": "LineString", "coordinates": [[-96.80, 32.78], "..."]}
  },
  "fuel_stops": [
    {"stop": 1, "station_id": 7, "name": "WOODSHED OF BIG CABIN", "address": "I-44, EXIT 283 & US-69",
     "city": "Big Cabin", "state": "OK", "lat": 36.538, "lng": -95.221,
     "mile_marker": 312.6, "distance_from_route_miles": 1.8,
     "price_per_gallon": "3.007", "gallons": "31.260", "cost": "94.01"}
  ],
  "summary": {
    "total_fuel_cost": "94.01",
    "total_gallons_purchased": "31.260",
    "total_gallons_used": "79.240",
    "stations_considered": 143
  },
  "assumptions": {
    "tank_gallons": 50, "mpg": 10, "start_fuel_gallons": 50, "start_fuel_charged": false,
    "corridor_miles": 5, "profile": "driving-hgv", "station_location_precision": "city centroid"
  },
  "map_url": "/api/route/map/?start=Dallas%2C+TX&finish=Denver%2C+CO&start_fuel_gallons=50",
  "external_api_calls": 1,
  "cached": false
}
```
Errors:
- `400`: bad input / place not found / non-US
- `422`: unreachable (`gap_miles`, `from`, `to`)
- `502`: ORS error or timeout (upstream message included)
- `503`: ORS quota hit (ORS 403 or 429)

### `GET /api/route/map/?start=&finish=[&start_fuel_gallons=&corridor_miles=]`
- Returns Leaflet HTML: the route polyline, start and finish markers, and numbered stop markers whose popups show name, price, gallons and cost.
- It goes through the same `planner` as POST, so it's **0 external calls when cached**, and 1 when not (counted in an `X-External-API-Calls` header).
- Map tiles and Leaflet load in the viewer's browser (OSM tiles, Leaflet from cdnjs). Those are client-side and not server API calls.

### `GET /api/health/`
```json
{"ok": true, "stations": 6626, "stations_geocoded": 6606, "places": 183980, "stations_version": "6626:2026-…"}
```

## 12. README-bound assumptions
- Duplicate OPIS IDs with different prices → lowest listed price kept.
- Canadian stations are skipped; routing is US-only.
- Stations are located at their town centroid (`station_location_precision: "city centroid"`); stations whose town isn't in GeoNames are excluded.
- The starting tank is full (50 gal) and not charged unless `start_fuel_gallons` says otherwise.
- The optimizer ignores detour distance to reach a station inside the corridor.

## 13. Tests (`python manage.py test`; ORS always mocked; the `requests` session is patched to fail on any real call)

**Optimizer**
- Start fuel covers the whole trip → no stops, cost 0
- **Finish reachable, no cheaper station ahead → buy only enough to finish** (not a full tank)
- **Arrive with ~0 fuel**: fuel left at the finish ≈ 0 (within 1e-9 gal) whenever a purchase happened
- Nearest-cheaper rule buys just enough to reach it; with no cheaper station in range, fill up → cheapest in range
- Start fuel spent first; start never buys
- Gap exactly 500 mi → OK; 500.01 → `Unreachable` with the right `from` / `to` / `gap_miles`
- `start_fuel = 0` + station at mile 0 → works; `start_fuel = 0` without one → `Unreachable`
- Equal prices → farther station
- Money: per-stop ROUND_HALF_UP to cents (e.g. 0.005 → 0.01); total == sum of rounded stops; prices with 8 dp are used unrounded
- Greedy cost == brute-force optimum (DP over 0.1-gal fuel levels) on 200 random small instances

**Corridor / stations index**
- Station 3 mi off a straight route is included with the correct mile marker (±0.3); 7 mi off is excluded
- Resampling: cumulative miles match the haversine total; spacing ≤ 0.5 mi
- Index reloads when `stations_version` changes and not otherwise (build counter)
- NULL-coordinate stations never enter the index

**Places / resolution**
- Normalisation: `St. Louis` ↔ `Saint Louis`, `Sault Ste. Marie`, `Ft Worth`, `Mc Calla` ↔ `McCalla`, accents, case and whitespace
- Ambiguous name → highest population
- Unknown city → fallback called once (mocked), `external_api_calls` incremented; a fallback result outside the US → 400
- `"lat,lng"` input bypasses the lookup

**Import commands** (fixtures, no network)
- `load_stations`:
  - dedupe keeps the lowest price and that row's name
  - conflict counts are logged
  - Canadian rows are skipped and counted
  - unmatched rows get NULL coordinates
  - 8-dp prices are stored exactly
  - re-running is idempotent and changes `stations_version`
- `load_places`: trims a tiny GeoNames TSV (drops non-P and PPLH/PPLQ) and imports the CSV

**API**
- Happy path: exactly 1 directions call, response schema including `assumptions` and `map_url`
- Identical second request → 0 calls, `cached: true`
- Re-import stations → plan cache miss, route cache hit → 0 directions calls
- Map endpoint after POST → 0 calls, HTML contains the polyline and N stop markers
- ORS 429/403 → 503; 5xx / timeout → 502; validation errors (missing field, start_fuel 51, non-US)
- Timing: `assertLogs` sees `resolve` / `route` / `corridor` / `optimize` / `total` at INFO
