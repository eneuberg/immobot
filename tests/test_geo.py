"""The listing page's map (geo.py): what an address names, geometry helpers, the cached lookups at Nominatim /
Overpass. Never talks to the real services: every request goes to an httpx.MockTransport."""

import asyncio
import json
import math
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import db, geo

SQUARE = [[6.90, 50.94], [6.92, 50.94], [6.92, 50.96], [6.90, 50.96], [6.90, 50.94]]


@pytest.mark.parametrize("address,kind,street,plz,district", [
    ("50823 Köln - Ehrenfeld", "plz", None, "50823", "Ehrenfeld"),
    ("Ehrenfeld, Köln", "district", None, None, "Ehrenfeld"),
    ("Venloer Str. 123, 50823 Köln", "address", "Venloer Str. 123", "50823", None),
    ("Venloer Straße 123, 50823 Köln, Ehrenfeld", "address", "Venloer Straße 123", "50823", "Ehrenfeld"),
    ("Köln-Ehrenfeld", "district", None, None, "Ehrenfeld"),
    ("Köln (Ehrenfeld)", "district", None, None, "Ehrenfeld"),
    ("Koeln - Lindenthal", "district", None, None, "Lindenthal"),
    ("Neustadt-Nord, Köln", "district", None, None, "Neustadt-Nord"),  # a hyphen inside the name stays
    ("50667 Köln", "plz", None, "50667", None),
    ("50823", "plz", None, "50823", None),
    ("Venloer Straße, 50823 Köln", "plz", None, "50823", None),  # a street without number: not exact
    ("Am Bilderstöckchen 5", "address", "Am Bilderstöckchen 5", None, None),
    ("Ringstraße 12-14, 50996 Köln", "address", "Ringstraße 12-14", "50996", None),
    ("  Neusser   Str. 5\n50670 ", "address", "Neusser Str. 5", "50670", None),
    ("Ehrenfeld", "district", None, None, "Ehrenfeld"),
    ("Köln", "city", None, None, None),
    ("", "none", None, None, None), ("   ", "none", None, None, None), (None, "none", None, None, None),
    (42, "none", None, None, None),
])
def test_parse_address(address, kind, street, plz, district):
    place = geo.parse_address(address)
    assert (place["kind"], place["street"], place["plz"], place["district"]) == (kind, street, plz, district)


def test_cache_keys_and_labels():
    plz = geo.parse_address("50823 Köln - Ehrenfeld")
    assert geo.cache_key(plz) == geo.cache_key(geo.parse_address("50823 Koeln")) == "plz:50823"  # one lookup per PLZ
    assert geo.label(plz) == "Nur Gebiet bekannt: PLZ 50823 (Ehrenfeld)"
    district = geo.parse_address("Köln-Ehrenfeld")
    assert geo.cache_key(district) == geo.cache_key(geo.parse_address("Ehrenfeld, KÖLN")) == "district:ehrenfeld|köln"
    assert geo.label(district) == "Nur Gebiet bekannt: Ehrenfeld"
    address = geo.parse_address("Am Bilderstöckchen 5")
    assert address["query"] == "Am Bilderstöckchen 5, Köln" and geo.label(address) == ""
    assert geo.parse_address("Musterstr. 12, Berlin")["query"] == "Musterstr. 12, Berlin"  # not moved to Köln
    assert geo.cache_key(geo.parse_address(None)) is None


def test_shrink_simplifies_and_rounds():
    ring = [[6.9 + i * 0.0001, 50.94 + 0.000001 * (i % 2)] for i in range(200)] + [[6.95, 50.99], [6.9, 50.94]]
    small = geo.shrink({"type": "Polygon", "coordinates": [ring]})
    assert small["type"] == "Polygon" and len(small["coordinates"][0]) < 10
    assert small["coordinates"][0][0] == small["coordinates"][0][-1] == [6.9, 50.94]
    multi = geo.shrink({"type": "MultiPolygon", "coordinates": [[SQUARE], [[[x + 1, y] for x, y in SQUARE]]]})
    assert multi["type"] == "MultiPolygon" and len(multi["coordinates"]) == 2
    assert geo.shrink({"type": "Point", "coordinates": [6.9, 50.9]}) is None
    assert geo.bounds({"type": "Polygon", "coordinates": [SQUARE]}) == [50.94, 6.90, 50.96, 6.92]
    huge = [[6.9 + 0.05 * math.cos(i / 500), 50.9 + 0.05 * math.sin(i / 500)] for i in range(3200)]
    assert sum(len(r) for r in geo.shrink({"type": "Polygon", "coordinates": [huge + [huge[0]]]})["coordinates"]) <= geo.MAX_POINTS


def test_stitch_and_overpass_geometry():
    a, b, c = [6.90, 50.94], [6.92, 50.94], [6.92, 50.96]
    d = [6.90, 50.96]
    ways = [[a, b], [c, b], [c, d, a]]  # the middle one reversed
    assert geo.stitch(ways) == [[a, b, c, d, a]]
    data = {"elements": [{"type": "relation", "members": [
        {"type": "way", "role": "outer", "geometry": [{"lon": p[0], "lat": p[1]} for p in way]} for way in ways]}]}
    assert geo.overpass_geometry(data) == {"type": "Polygon", "coordinates": [[a, b, c, d, a]]}
    assert geo.overpass_geometry({"elements": []}) is None


class Services:
    """Fake Nominatim + Overpass: records every request, answers from `answers` by service."""

    def __init__(self, nominatim=None, overpass=None, status=200):
        self.requests = []
        self.nominatim, self.overpass, self.status = nominatim or [], overpass or {"elements": []}, status
        self.overpass_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="busy")
        if request.url.host == "overpass-api.de":
            if self.overpass_status != 200:
                return httpx.Response(self.overpass_status, text="busy")
            return httpx.Response(200, json=self.overpass)
        assert request.url.host == "nominatim.openstreetmap.org"
        return httpx.Response(200, json=self.nominatim)


@pytest.fixture
def services(fresh_db, monkeypatch):
    fake = Services()
    monkeypatch.setattr(geo, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(geo, "REQUEST_GAP_S", 0)
    monkeypatch.setattr(geo, "_running", {})
    monkeypatch.setattr(geo, "_throttle", asyncio.Lock())
    return fake


def run_ensure(address):
    """geo.ensure as the endpoint calls it, then wait for its background lookup."""
    async def main():
        answer = geo.ensure(address)
        await asyncio.gather(*geo._running.values())
        return answer
    return asyncio.run(main())


def test_plz_from_nominatim_then_from_the_cache(services):
    services.nominatim = [{"lat": "50.95", "lon": "6.91", "geojson": {"type": "Polygon", "coordinates": [SQUARE]}}]
    assert geo.cached("50823 Köln - Ehrenfeld") == {"state": "pending", "kind": "plz"}
    assert run_ensure("50823 Köln - Ehrenfeld")["state"] == "pending"
    assert len(services.requests) == 1
    request = services.requests[0]
    assert request.url.host == "nominatim.openstreetmap.org" and request.url.params["postalcode"] == "50823"
    assert request.headers["user-agent"] == "immobot (self-hosted)"  # generic, nothing personal
    answer = run_ensure("50823 Köln")  # another listing in the same PLZ: from the cache, no request
    assert len(services.requests) == 1
    assert answer["state"] == "ok" and answer["geometry"]["type"] == "Polygon" and answer["label"].startswith("Nur Gebiet")
    assert answer["bounds"] == [50.94, 6.9, 50.96, 6.92]


def test_plz_from_overpass_when_nominatim_has_no_area(services):
    services.nominatim = [{"lat": "50.95", "lon": "6.91", "geojson": {"type": "Point", "coordinates": [6.91, 50.95]}}]
    ring = [{"lon": x, "lat": y} for x, y in SQUARE]
    services.overpass = {"elements": [{"type": "relation", "members": [{"type": "way", "role": "outer", "geometry": ring}]}]}
    run_ensure("50823 Köln")
    hosts = [r.url.host for r in services.requests]
    assert hosts == ["nominatim.openstreetmap.org", "overpass-api.de"]
    assert services.requests[1].method == "POST"
    assert '"postal_code"="50823"' in httpx.QueryParams(services.requests[1].content.decode())["data"]
    assert geo.cached("50823 Köln")["geometry"]["type"] == "Polygon"


def test_plz_with_overpass_down_falls_back_to_the_district(services):
    """Overpass overloaded (timeouts, 504): not an error for the PLZ - the Stadtteil the ad names is shown instead."""
    services.overpass_status = 504
    district = {"lat": "50.95", "lon": "6.91", "geojson": {"type": "Polygon", "coordinates": [SQUARE]}}

    def answer(request):
        services.requests.append(request)
        if request.url.host == "overpass-api.de":
            return httpx.Response(504, text="busy")
        if request.url.params.get("postalcode"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[district])
    geo._transport = httpx.MockTransport(answer)
    run_ensure("50823 Köln - Ehrenfeld")
    hosts = [r.url.host for r in services.requests]
    assert hosts == ["nominatim.openstreetmap.org", "overpass-api.de", "nominatim.openstreetmap.org"]
    assert services.requests[-1].url.params["q"] == "Ehrenfeld, Köln"
    assert geo.cached("50823 Köln - Ehrenfeld")["state"] == "ok"


def test_district_polygon_and_address_point(services):
    services.nominatim = [{"lat": "50.95", "lon": "6.91", "geojson": {"type": "Polygon", "coordinates": [SQUARE]}}]
    run_ensure("Ehrenfeld, Köln")
    params = services.requests[-1].url.params
    assert params["q"] == "Ehrenfeld, Köln" and params["polygon_geojson"] == "1" and params["countrycodes"] == "de"
    assert geo.cached("Köln-Ehrenfeld")["geometry"]["type"] == "Polygon"
    services.nominatim = [{"lat": "50.9512345", "lon": "6.9123456"}]
    run_ensure("Venloer Str. 123, 50823 Köln")
    assert services.requests[-1].url.params["q"] == "Venloer Str. 123, 50823 Köln"
    answer = geo.cached("Venloer Str. 123, 50823 Köln")
    assert answer["geometry"] == {"type": "Point", "coordinates": [6.912346, 50.951234]} and answer["label"] == ""


def test_region_known_only_as_a_point_gets_no_pin(services):
    """A district Nominatim knows only as a point would be a pin at a made-up exact spot: no map of ours then."""
    services.nominatim = [{"lat": "50.95", "lon": "6.91", "geojson": {"type": "Point", "coordinates": [6.91, 50.95]}}]
    run_ensure("Ehrenfeld")
    assert geo.cached("Ehrenfeld") == {"state": "failed", "kind": "district"}
    assert db.get_geo("district:ehrenfeld|köln")["status"] == "none"
    run_ensure("Ehrenfeld")  # "none" is final: not asked again
    assert len(services.requests) == 1


def test_failed_lookup_is_retried_later(services):
    services.status = 503
    run_ensure("Ehrenfeld, Köln")
    assert geo.cached("Ehrenfeld, Köln") == {"state": "failed", "kind": "district"}  # the page shows Google's map
    run_ensure("Ehrenfeld, Köln")
    assert len(services.requests) == 1  # not hammered
    old = (datetime.now(timezone.utc) - timedelta(seconds=geo.ERROR_RETRY_S + 5)).isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE geo_cache SET fetched_at = ?", (old,))
    assert geo.cached("Ehrenfeld, Köln")["state"] == "pending"
    services.status = 200
    services.nominatim = [{"lat": "1", "lon": "2", "geojson": {"type": "Polygon", "coordinates": [SQUARE]}}]
    run_ensure("Ehrenfeld, Köln")
    assert geo.cached("Ehrenfeld, Köln")["state"] == "ok" and len(services.requests) == 2


def test_bad_answer_and_no_address(services):
    services.nominatim = {"error": "nope"}  # not a list
    run_ensure("Ehrenfeld")
    assert db.get_geo("district:ehrenfeld|köln")["status"] == "error"
    assert geo.ensure(None) == {"state": "none", "kind": "none"} and geo.cached("  ") == {"state": "none", "kind": "none"}


def test_stored_rows_are_small(services):
    big = [[6.9 + 0.05 * math.cos(i / 400), 50.9 + 0.05 * math.sin(i / 400)] for i in range(2600)]
    services.nominatim = [{"lat": "1", "lon": "2", "geojson": {"type": "Polygon", "coordinates": [big + [big[0]]]}}]
    run_ensure("Lindenthal, Köln")
    row = db.get_geo("district:lindenthal|köln")
    assert row["status"] == "ok" and len(row["geometry"]) < 20_000 and json.loads(row["geometry"])["type"] == "Polygon"
