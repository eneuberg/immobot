"""Where a listing is, for the map on the listing page: a point for an exact address, else the outline of the most
specific region the ad names (PLZ area, else Stadtteil/Bezirk, else the city).

Listings only carry free text ("Venloer Str. 123, 50823 Köln", "50823 Köln - Ehrenfeld", "Ehrenfeld, Köln": ImmoScout24
hides the street until contact). parse_address() tells which of these it is (pure, no network). lookup() turns it into
geometry: OpenStreetMap's Nominatim (search, with polygon_geojson=1 for Stadtteile) and, for a PLZ, Overpass (the
relation boundary=postal_code). Rules for those free services:
- server-side only, and only when a listing page asks (web.listing_geo; never in bulk, nothing on a schedule);
- at most one request per REQUEST_GAP_S to them together, a generic User-Agent (USER_AGENT) without personal data;
- every distinct place is looked up once and kept in the table geo_cache for good ("none" too); failures are retried
  after ERROR_RETRY_S. Polygons are simplified (SIMPLIFY_DEG, MAX_POINTS) and rounded to keep the rows small.
The page never waits for this: it gets what is cached, else polls a small JSON endpoint while the lookup runs in the
background. Without a result it falls back to Google's embed of the address search (portals.maps_embed_url).
"""

import asyncio
import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone

import httpx

from . import db, portals

log = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "immobot (self-hosted)"
REQUEST_GAP_S = 1.1
TIMEOUT_S = 20.0
ERROR_RETRY_S = 6 * 3600
SIMPLIFY_DEG = 0.00015  # ~10-15 m: invisible at the zoom of a district, a fraction of the points
MAX_POINTS = 600
DECIMALS = 5

_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here
_throttle = asyncio.Lock()
_last_request = 0.0
_running: dict[str, asyncio.Task] = {}  # cache key -> lookup in progress (one per place)

# --- what the address says (pure) -------------------------------------------------------------------------------

_PLZ = re.compile(r"(?<!\d)(\d{5})(?!\d)")
# "Venloer Str. 123", "Am Bilderstöckchen 5a", "Hauptstr.1", "Ringstraße 12-14": words, then a house number at the end
_STREET_NO = re.compile(r"^(?=.*[A-Za-zÄÖÜäöüß])[^\d,]*?[A-Za-zÄÖÜäöüß.]\s*\d{1,4}\s*[a-zA-Z]?(\s*[-/]\s*\d{1,4}\s*[a-zA-Z]?)?$")
_STREET_WORD = re.compile(r"(str\.|straße|strasse|weg|gasse|allee|platz|ring|ufer|damm|wall|chaussee|pfad|steig)\b",
                          re.IGNORECASE)


def _clean(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _city_words() -> tuple[str, ...]:
    return portals._CITY_WORDS


def _is_city(part: str) -> bool:
    return part.strip().lower() in _city_words()


def parse_address(address: str | None) -> dict:
    """{"kind": "address" | "plz" | "district" | "city" | "none", "street", "plz", "district", "city"} - what the free
    text names, most specific first: street with house number (exact), PLZ, Stadtteil, only the city."""
    out = {"kind": "none", "street": None, "plz": None, "district": None, "city": portals.MAPS_CITY or None}
    if not isinstance(address, str) or not _clean(address):
        return out
    text = _clean(address)[:portals.MAPS_QUERY_MAX]
    plz = _PLZ.search(text)
    out["plz"] = plz.group(1) if plz else None
    # "Köln (Ehrenfeld)" -> "Köln, Ehrenfeld"; " - " / " – " separate like commas; "Köln-Ehrenfeld" only around the city
    text = re.sub(r"\(([^)]*)\)", r", \1", text)
    parts = [p.strip(" .-–") for p in re.split(r",|\s[-–]\s|\n", text)]
    found_city = None
    rest = []
    for part in parts:
        if not part:
            continue
        part = _PLZ.sub("", part).strip(" .-–")
        if not part:
            continue
        hyphen = re.fullmatch(r"(.+?)\s*-\s*(.+)", part)
        if hyphen and (_is_city(hyphen.group(1)) or _is_city(hyphen.group(2))):  # "Köln-Ehrenfeld"
            city, other = (hyphen.group(1), hyphen.group(2)) if _is_city(hyphen.group(1)) else hyphen.groups()[::-1]
            found_city = found_city or city
            rest.append(other.strip())
            continue
        words = part.split()
        if words and _is_city(words[0]) and len(words) > 1 and not _STREET_NO.match(part):  # "Köln Ehrenfeld"
            found_city = found_city or words[0]
            rest.append(" ".join(words[1:]))
            continue
        if _is_city(part):
            found_city = found_city or part
            continue
        rest.append(part)
    if found_city:
        out["city"] = portals.MAPS_CITY or found_city  # "Koeln", "KÖLN": one spelling, one cache key
    street = next((p for p in rest if _STREET_NO.match(p)), None)
    named_street = next((p for p in rest if _STREET_WORD.search(p)), None)  # a street without number: not a district
    district = next((p for p in rest if p not in (street, named_street) and not any(c.isdigit() for c in p)), None)
    out["street"], out["district"] = street, district
    if street:
        out["kind"] = "address"
        # searched as written ("Musterstr. 12, Berlin" stays in Berlin); the city only when nothing else says where
        out["query"] = text if (out["plz"] or found_city or district) else portals.maps_query(text)
    elif out["plz"]:
        out["kind"] = "plz"
    elif district:
        out["kind"] = "district"
    elif out["city"] and (found_city or named_street):
        out["kind"] = "city"
    return out


def cache_key(place: dict) -> str | None:
    """One key per distinct place: the same PLZ of two listings is looked up once."""
    city = (place.get("city") or "").lower()
    kind = place["kind"]
    if kind == "address":
        return f"address:{place['query'].lower()}"
    if kind == "plz":
        return f"plz:{place['plz']}"
    if kind == "district":
        return f"district:{place['district'].lower()}|{city}"
    if kind == "city":
        return f"city:{city}"
    return None


def label(place: dict) -> str:
    """The caption under a region: "Nur Gebiet bekannt: PLZ 50823 (Ehrenfeld)"."""
    kind = place["kind"]
    if kind == "plz":
        return f"Nur Gebiet bekannt: PLZ {place['plz']}" + (f" ({place['district']})" if place.get("district") else "")
    if kind == "district":
        return f"Nur Gebiet bekannt: {place['district']}"
    if kind == "city":
        return f"Nur Gebiet bekannt: {place['city']}"
    return ""


# --- geometry (pure) ---------------------------------------------------------------------------------------------

def _simplify(points: list[list[float]], tolerance: float) -> list[list[float]]:
    """Douglas-Peucker on [lon, lat] points (iterative, keeps first and last)."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        (x1, y1), (x2, y2) = points[start][:2], points[end][:2]
        dx, dy = x2 - x1, y2 - y1
        norm = math.hypot(dx, dy)
        best, index = -1.0, None
        for i in range(start + 1, end):
            x, y = points[i][:2]
            dist = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / norm if norm else math.hypot(x - x1, y - y1)
            if dist > best:
                best, index = dist, i
        if index is not None and best > tolerance:
            keep[index] = True
            stack += [(start, index), (index, end)]
    return [p for p, k in zip(points, keep) if k]


def _rings(geometry: dict) -> list[list[list[list[float]]]]:
    """Polygon / MultiPolygon -> list of polygons, each a list of rings of [lon, lat]."""
    if geometry.get("type") == "Polygon":
        return [geometry["coordinates"]]
    if geometry.get("type") == "MultiPolygon":
        return list(geometry["coordinates"])
    return []


def shrink(geometry: dict) -> dict | None:
    """The polygon simplified (more until it has at most MAX_POINTS) and rounded; None if it isn't one."""
    polygons = _rings(geometry)
    if not polygons:
        return None
    tolerance = SIMPLIFY_DEG
    while True:
        out = []
        for polygon in polygons:
            rings = []
            for ring in polygon:
                small = [[round(x, DECIMALS), round(y, DECIMALS)] for x, y, *_ in _simplify(ring, tolerance)]
                if len(small) >= 4:
                    rings.append(small)
            if rings:
                out.append(rings)
        count = sum(len(r) for p in out for r in p)
        if count <= MAX_POINTS or tolerance > 0.05:
            break
        tolerance *= 2
    if not out:
        return None
    return {"type": "Polygon", "coordinates": out[0]} if len(out) == 1 else {"type": "MultiPolygon", "coordinates": out}


def bounds(geometry: dict) -> list[float]:
    """[south, west, north, east] of a Point / Polygon / MultiPolygon."""
    if geometry["type"] == "Point":
        lon, lat = geometry["coordinates"]
        return [lat, lon, lat, lon]
    xs, ys = [], []
    for polygon in _rings(geometry):
        for ring in polygon:
            xs += [p[0] for p in ring]
            ys += [p[1] for p in ring]
    return [min(ys), min(xs), max(ys), max(xs)]


def stitch(ways: list[list[list[float]]]) -> list[list[list[float]]]:
    """Overpass gives a boundary as loose ways: join them end to end into closed rings ([lon, lat])."""
    pending = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while pending:
        ring = pending.pop(0)
        changed = True
        while ring[0] != ring[-1] and changed:
            changed = False
            for i, way in enumerate(pending):
                if way[0] == ring[-1]:
                    ring += way[1:]
                elif way[-1] == ring[-1]:
                    ring += way[::-1][1:]
                elif way[-1] == ring[0]:
                    ring = way[:-1] + ring
                elif way[0] == ring[0]:
                    ring = way[::-1][:-1] + ring
                else:
                    continue
                pending.pop(i)
                changed = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def overpass_geometry(data: dict) -> dict | None:
    """The first postal_code relation of an Overpass "out geom" answer as a (Multi)Polygon, holes ignored."""
    for element in data.get("elements") or []:
        if element.get("type") != "relation":
            continue
        ways = [[[p["lon"], p["lat"]] for p in member.get("geometry") or []]
                for member in element.get("members") or []
                if member.get("type") == "way" and member.get("role", "outer") in ("outer", "")]
        rings = stitch(ways)
        if rings:
            return {"type": "Polygon", "coordinates": [rings[0]]} if len(rings) == 1 else \
                {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}
    return None


# --- network ---------------------------------------------------------------------------------------------------

class GeoLookupFailed(Exception):
    """The service could not be asked (network, HTTP status, bad answer): retried after ERROR_RETRY_S."""


async def _request(method: str, url: str, **kwargs) -> object:
    """One request to Nominatim / Overpass, at most one per REQUEST_GAP_S (both together), JSON back."""
    global _last_request
    async with _throttle:
        wait = _last_request + REQUEST_GAP_S - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S, headers={"User-Agent": USER_AGENT},
                                         transport=_transport, follow_redirects=False) as client:
                response = await client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeoLookupFailed(f"{type(exc).__name__}") from exc
        finally:
            _last_request = time.monotonic()


async def _nominatim(params: dict) -> list:
    data = await _request("GET", NOMINATIM_URL, params={"format": "jsonv2", "limit": "1", "countrycodes": "de",
                                                        "accept-language": "de", **params})
    if not isinstance(data, list):
        raise GeoLookupFailed("unexpected answer")
    return data


async def _find(place: dict) -> dict | None:
    """Geometry for the place: {"type": "Point", ...} for an address, a (Multi)Polygon for a region; None if the
    services know no such place (or only a point for a region: a pin there would claim a precision there isn't)."""
    city = place.get("city") or ""
    kind = place["kind"]
    if kind == "address":
        hits = await _nominatim({"q": place["query"]})
        if hits:
            return {"type": "Point", "coordinates": [round(float(hits[0]["lon"]), 6), round(float(hits[0]["lat"]), 6)]}
        return None
    if kind == "plz":
        query = f'[out:json][timeout:25];relation["boundary"="postal_code"]["postal_code"="{place["plz"]}"];out geom;'
        data = await _request("POST", OVERPASS_URL, data={"data": query})
        geometry = overpass_geometry(data if isinstance(data, dict) else {})
        if geometry is None:  # no boundary in OSM: Nominatim's postcode search, if it has an outline
            hits = await _nominatim({"postalcode": place["plz"], "polygon_geojson": "1",
                                     "polygon_threshold": str(SIMPLIFY_DEG)})
            geometry = hits[0].get("geojson") if hits else None
        if geometry is None and place.get("district"):  # no PLZ area at all: the Stadtteil the ad names
            return await _find({**place, "kind": "district"})
        return shrink(geometry) if geometry else None
    name = place["district"] if kind == "district" else city
    hits = await _nominatim({"q": f"{name}, {city}" if kind == "district" and city else name,
                             "polygon_geojson": "1", "polygon_threshold": str(SIMPLIFY_DEG)})
    geometry = hits[0].get("geojson") if hits else None
    return shrink(geometry) if geometry else None


# --- cache & API for the page ------------------------------------------------------------------------------------

def _row_answer(place: dict, row: dict) -> dict:
    if row["status"] != "ok":
        return {"state": "failed", "kind": place["kind"]}
    geometry = json.loads(row["geometry"])
    return {"state": "ok", "kind": place["kind"], "geometry": geometry, "bounds": bounds(geometry),
            "label": label(place) if geometry["type"] != "Point" else ""}


def _usable(row: dict | None) -> bool:
    """A cached row counts unless it is an error older than ERROR_RETRY_S (then it is asked again)."""
    if row is None:
        return False
    if row["status"] != "error":
        return True
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
    return age < timedelta(seconds=ERROR_RETRY_S)


def cached(address: str | None) -> dict:
    """What the page can show right now: {"state": "ok", "kind", "geometry", "bounds", "label"} from the cache,
    {"state": "failed"} (nothing found or the lookup failed: Google's embed instead), {"state": "pending"} (not
    looked up yet) or {"state": "none"} (no address)."""
    place = parse_address(address)
    key = cache_key(place)
    if key is None:
        return {"state": "none", "kind": "none"}
    row = db.get_geo(key)
    if _usable(row):
        return _row_answer(place, row)
    return {"state": "pending", "kind": place["kind"]}


async def _lookup(key: str, place: dict) -> None:
    try:
        geometry = await _find(place)
    except GeoLookupFailed as exc:
        log.info("Map lookup failed for %s: %s", place["kind"], exc)
        db.set_geo(key, "error", None)
    except Exception:
        log.exception("Map lookup failed for %s", place["kind"])
        db.set_geo(key, "error", None)
    else:
        db.set_geo(key, "ok" if geometry else "none", json.dumps(geometry, separators=(",", ":")) if geometry else None)


def ensure(address: str | None) -> dict:
    """cached(), and a background lookup when it says "pending" (one per place at a time). Call from the event loop."""
    answer = cached(address)
    if answer["state"] == "pending":
        place = parse_address(address)
        key = cache_key(place)
        if key not in _running:
            task = asyncio.create_task(_lookup(key, place))
            _running[key] = task
            task.add_done_callback(lambda _t, k=key: _running.pop(k, None))
    return answer
