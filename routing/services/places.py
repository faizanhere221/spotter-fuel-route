"""Place-name normalisation, offline (City, ST) matching, and start/finish resolution.

`normalize()` is the single normalisation function used everywhere: Place import, station
geocoding and user input lookups.
"""
import re
import unicodedata
from dataclasses import dataclass

US_STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
US_STATES = frozenset(US_STATE_NAMES)

# Explicit aliases for well-known inputs whose GeoNames name differs in a way no general rule
# should cover: (normalised input name, state) -> GeoNames name (same state).
ALIASES = {
    # GeoNames lists the city as "New York City"; the only "new york*" alternative is New York Mills.
    ("new york", "NY"): "New York City",
}

# (min_lat, max_lat, min_lng, max_lng). Coarse boxes by design: the lower-48 box also covers
# southern Ontario/Quebec and northern Mexico border towns; the Alaska box ignores the Aleutians
# west of 180°. Points there pass this check and fail later (no route / no stations).
US_BOUNDS = {
    "contiguous US": (24.3, 49.39, -125.0, -66.8),  # 49.39 = Northwest Angle, MN
    "Alaska": (51.0, 71.6, -180.0, -129.9),
    "Hawaii": (18.8, 22.4, -160.6, -154.7),
}

_DIRECTIONS = {"n": "north", "s": "south", "e": "east", "w": "west"}
_SUFFIX = re.compile(r"\s(city|town|village)$")


def normalize(name):
    """Canonical form of a place name for matching: 'St. Louis' -> 'saint louis'."""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.'`’]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\b(st|ste|sainte)\b", "saint", s)
    s = re.sub(r"^ft\b", "fort", s)
    s = re.sub(r"\bmt\b", "mount", s)
    s = re.sub(r"^([nsew]) ", lambda m: _DIRECTIONS[m.group(1)] + " ", s)
    return s


def nospace(norm):
    return norm.replace(" ", "")


def match_keys(city, state):
    """Ordered match passes for a city name: (geocode_source, field, value)."""
    n = normalize(city)
    n = normalize(ALIASES.get((n, state), n))
    keys = [("place", "name_norm", n)]
    stripped = _SUFFIX.sub("", n)
    if stripped != n:
        keys.append(("place_suffix", "name_norm", stripped))
    keys.append(("place_nospace", "name_nospace", nospace(n)))
    return keys


class PlaceIndex:
    """In-memory (state, key) -> best place, for bulk station geocoding."""

    def __init__(self, places):
        self._by = {"name_norm": {}, "name_nospace": {}}
        for p in places:
            for field in self._by:
                k = (p.state, getattr(p, field))
                best = self._by[field].get(k)
                if best is None or p.population > best.population:
                    self._by[field][k] = p

    def match(self, city, state):
        """Return (place, geocode_source) or (None, None)."""
        for source, field, value in match_keys(city, state):
            p = self._by[field].get((state, value))
            if p is not None:
                return p, source
        return None, None


# ---------------------------------------------------------------- start / finish resolution

class LocationError(ValueError):
    """Input can't be turned into a US location (bad format, unknown place, outside the US)."""


@dataclass(frozen=True)
class ResolvedPoint:
    query: str
    lat: float
    lng: float
    source: str  # "coordinates" | "place" | "place_suffix" | "place_nospace" | "ors_geocode"
    label: str


_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_STATE_BY_NAME = {normalize(v): k for k, v in US_STATE_NAMES.items()}


def in_us_bounds(lat, lng):
    return any(a <= lat <= b and c <= lng <= d for a, b, c, d in US_BOUNDS.values())


def parse_city_state(text):
    """'Big Cabin, OK' / 'Saint Louis, Missouri' -> ('Big Cabin', 'OK')."""
    if "," not in text:
        raise LocationError(f"Expected 'City, ST' or 'lat,lng', got {text!r}.")
    city, state = (part.strip() for part in text.rsplit(",", 1))
    if not city or not state:
        raise LocationError(f"Expected 'City, ST' or 'lat,lng', got {text!r}.")
    code = state.upper() if state.upper() in US_STATES else _STATE_BY_NAME.get(normalize(state))
    if code is None:
        raise LocationError(f"Unknown US state {state!r} in {text!r}.")
    if not normalize(city):
        # e.g. non-Latin or punctuation-only names: nothing to match, and all such names would
        # share one geo cache key.
        raise LocationError(f"City name {city!r} has no letters or digits usable for a US place lookup.")
    return city, code


def lookup_place(city, state):
    """DB lookup with the same passes as station matching. Returns (Place, source) or (None, None)."""
    from routing.models import Place

    for source, field, value in match_keys(city, state):
        p = Place.objects.filter(state=state, **{field: value}).order_by("-population").first()
        if p is not None:
            return p, source
    return None, None


def resolve_point(text, geocoder=None):
    """Resolve 'City, ST' / 'City, State Name' / 'lat,lng' to a ResolvedPoint.

    `geocoder(text) -> (lat, lng, label) | None` is the optional network fallback used when the
    offline lookup misses.
    """
    text = (text or "").strip()
    if not text:
        raise LocationError("Location is required.")

    m = _COORDS.match(text)
    if m:
        lat, lng = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise LocationError(f"Invalid coordinates {text!r}; expected 'lat,lng'.")
        if not in_us_bounds(lat, lng):
            raise LocationError(f"Coordinates {text!r} are outside the US (lower 48, Alaska, Hawaii).")
        return ResolvedPoint(text, lat, lng, "coordinates", text)

    city, state = parse_city_state(text)
    place, source = lookup_place(city, state)
    if place is not None:
        return ResolvedPoint(text, place.lat, place.lng, source, f"{place.name}, {place.state}")

    if geocoder is not None:
        hit = geocoder(f"{city}, {state}")
        if hit is not None:
            lat, lng, label = hit
            if not in_us_bounds(lat, lng):
                raise LocationError(f"{text!r} geocoded outside the US.")
            return ResolvedPoint(text, lat, lng, "ors_geocode", label)
    raise LocationError(f"Place not found: {text!r}.")
