"""Tests for app/is24.py with mocked HTTP. tests/fixtures/is24_expose.json is an anonymized copy of a real
exposé (names, company, addresses, phone numbers, ids and picture URLs replaced; structure and captions kept)."""

import asyncio
import copy
import functools
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app import is24, media

EXPOSE = json.loads((Path(__file__).parent / "fixtures" / "is24_expose.json").read_text())
EXPOSE_URL = "https://www.immobilienscout24.de/expose/123456789"
API = "https://api.mobile.immobilienscout24.de/expose/123456789"


@functools.cache
def webp(width=1500, height=1000, color=(120, 160, 200)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="WEBP")
    return out.getvalue()


class FakeIs24:
    """Serves the exposé and pictures; records every request. `fail_at` makes the n-th picture fail."""

    def __init__(self, expose=EXPOSE, fail_at=None, fail_status=403, content_type="image/webp"):
        self.expose, self.fail_at, self.fail_status, self.content_type = expose, fail_at, fail_status, content_type
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.mobile.immobilienscout24.de":
            return httpx.Response(200, json=self.expose)
        pictures = [r for r in self.requests if r.url.host == "pictures.immobilienscout24.de"]
        if self.fail_at is not None and len(pictures) == self.fail_at:
            return httpx.Response(self.fail_status, text="no")
        return httpx.Response(200, content=webp(), headers={"content-type": self.content_type})

    @property
    def picture_urls(self):
        return [str(r.url) for r in self.requests if r.url.host == "pictures.immobilienscout24.de"]


def serve(monkeypatch, handler):
    """One fake server for both the exposé API (is24) and the picture downloads (media)."""
    monkeypatch.setattr(is24, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(media, "_transport", httpx.MockTransport(handler))


@pytest.fixture
def is24_server(monkeypatch):
    server = FakeIs24()
    serve(monkeypatch, server)
    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0)
    return server


def add_is24_listing(db, fredy_id="is1", url=EXPOSE_URL, provider="immoscout", **extra):
    return db.insert_listing({"fredy_id": fredy_id, "provider": provider, "title": "Wohnung", "url": url, **extra})[0]


# --- URLs ----------------------------------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://www.immobilienscout24.de/expose/123456789", "123456789"),
    ("https://www.immobilienscout24.de/expose/123456789/", "123456789"),
    ("https://www.immobilienscout24.de/expose/123456789?referrer=RESULT_LIST#/gallery", "123456789"),
    ("https://immobilienscout24.de/expose/42", "42"),
    ("https://www.immobilienscout24.at/expose/42", None),  # Austria: different API
    ("https://evil.example/expose/42", None),
    ("https://www.immobilienscout24.de.evil.example/expose/42", None),
    ("https://www.immobilienscout24.de/expose/abc", None),
    ("https://www.immobilienscout24.de/Suche/de/berlin", None),
    ("javascript:alert(1)", None),
    (None, None),
])
def test_expose_id(url, expected):
    assert is24.expose_id(url) == expected


def test_only_immoscout_germany_is_supported():
    assert is24.is_supported({"provider": "immoscout", "url": EXPOSE_URL})
    assert not is24.is_supported({"provider": "immoscoutAt", "url": EXPOSE_URL})
    assert not is24.is_supported({"provider": "immowelt", "url": EXPOSE_URL})
    assert not is24.is_supported({"provider": "immoscout", "url": "https://www.immobilienscout24.de/Suche/x"})


@pytest.mark.parametrize("url,ok", [
    ("https://pictures.immobilienscout24.de/listings/x.jpg/ORIG/resize/1500x1000/format/webp/quality/80", True),
    ("http://pictures.immobilienscout24.de/listings/x.jpg", False),  # https only
    ("https://pictures.immobilienscout24.de/listings/x.jpg/ORIG/legacy_thumbnail/%WIDTH%x%HEIGHT%/format/jpg", False),
    ("https://evil.example/x.jpg", False),
    ("https://pictures.immobilienscout24.de.evil.example/x.jpg", False),
    ("https://immobilienscout24.de@evil.example/x.jpg", False),
    ("https://pictures.immobilienscout24.de:8443/x.jpg", False),
    (None, False),
])
def test_image_host_allowlist(url, ok):
    assert is24.allowed_image_url(url) is ok


# --- parsing -------------------------------------------------------------------

def test_parse_expose_pictures_skip_ads():
    parsed = is24.parse_expose(EXPOSE)
    media = parsed["media"]
    assert len(media) == 11  # 11 PICTURE + 1 AD in the exposé
    assert [m["position"] for m in media] == list(range(1, 12))
    assert media[0]["caption"] == "Balkon" and media[1]["caption"] == "Außenansicht"
    assert all("/resize/1500x1000/" in m["url"] for m in media)  # fullImageUrl, IS24's own variant
    assert not any(m["is_floorplan"] for m in media)


def test_parse_expose_floor_plans_first():
    expose = copy.deepcopy(EXPOSE)
    pictures = expose["sections"][0]["media"]
    pictures[5]["caption"] = "Grundriss OG"
    pictures[9]["caption"] = "grundriss Keller"
    media = is24.parse_expose(expose)["media"]
    assert [m["caption"] for m in media[:3]] == ["Grundriss OG", "grundriss Keller", "Balkon"]
    assert [m["is_floorplan"] for m in media[:3]] == [True, True, False]
    assert [m["position"] for m in media] == list(range(1, 12))


def test_parse_expose_drops_foreign_and_template_urls():
    expose = copy.deepcopy(EXPOSE)
    pictures = expose["sections"][0]["media"]
    pictures[0]["fullImageUrl"] = "https://evil.example/x.jpg"  # falls back to IS24's preview variant
    pictures[1]["fullImageUrl"] = pictures[1]["previewImageUrl"] = "https://evil.example/y.jpg"
    media = is24.parse_expose(expose)["media"]
    assert len(media) == 10
    assert "/resize/800x600/" in media[0]["url"]
    assert all(is24.allowed_image_url(m["url"]) for m in media)


def test_parse_expose_details_text():
    parsed = is24.parse_expose(EXPOSE)
    details = parsed["details_text"]
    assert parsed["contact_name"] == "Frau Erika Mustermann"
    for expected in ["Zimmer: 3", "Adresse: Musterstraße 1, 12345 Musterstadt", "## Hauptkriterien",
                     "Wohnungstyp: Penthouse", "Balkon/Terrasse: ja", "## Kosten", "Gesamtmiete: 1.400 €",
                     "## Objektbeschreibung\nHelle Dachgeschosswohnung", "## Sonstiges",
                     "Anbieter: Frau Erika Mustermann, Beispiel Hausverwaltung GmbH"]:
        assert expected in details, expected
    assert "\xa0" not in details
    assert "+49 30" not in details and "Beispielweg" not in details  # no phone, no agent address
    assert "SCHUFA" not in details  # LINK attributes are skipped


def test_parse_expose_tolerates_junk():
    assert is24.parse_expose({"sections": [None, 1, {"type": "MEDIA", "media": "x"}, {"type": "TEXT_AREA"}]}) == {
        "details_text": None, "contact_name": None, "media": []}


# --- fetching ------------------------------------------------------------------

def test_fetch_expose_request(is24_server):
    data = asyncio.run(is24.fetch_expose("123456789"))
    assert data["header"]["id"] == "123456789"
    (request,) = is24_server.requests
    assert str(request.url) == API
    assert request.headers["user-agent"] == "ImmoScout_28.1_26.5.2_._"


@pytest.mark.parametrize("response,message", [
    (httpx.Response(404, text="gone"), "HTTP 404"),
    (httpx.Response(200, text="<html>"), "kein JSON"),
    (httpx.Response(200, json={"no": "sections"}), "unerwartetes Format"),
    (httpx.Response(301, headers={"location": "https://evil.example/"}), "HTTP 301"),  # redirects are not followed
])
def test_fetch_expose_errors(monkeypatch, response, message):
    serve(monkeypatch, lambda r: response)
    with pytest.raises(is24.Is24Error, match=message):
        asyncio.run(is24.fetch_expose("123456789"))


def test_fetch_expose_network_error(monkeypatch):
    def down(request):
        raise httpx.ConnectError("down", request=request)

    serve(monkeypatch, down)
    with pytest.raises(is24.Is24Error, match="nicht erreichbar"):
        asyncio.run(is24.fetch_expose("123456789"))
    with pytest.raises(is24.Is24Error, match="Ungültige"):
        asyncio.run(is24.fetch_expose("../x"))


# --- ensure_media: download, cache, errors -------------------------------------------

def test_ensure_media_downloads_sequentially_and_caches(fresh_db, is24_server, monkeypatch):
    sleeps = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0.25)
    monkeypatch.setattr(asyncio, "sleep", record_sleep)
    listing_id = add_is24_listing(fresh_db)

    rows = asyncio.run(media.ensure_media(listing_id))
    assert [r["position"] for r in rows] == list(range(1, 12)) and rows[0]["caption"] == "Balkon"
    assert len(is24_server.requests) == 12  # 1 exposé + 11 pictures, nothing else
    assert is24_server.picture_urls == [m["url"] for m in is24.parse_expose(EXPOSE)["media"]]  # IS24's URLs as given
    assert sleeps == [0.25] * 10  # a pause between pictures
    listing = fresh_db.get_listing(listing_id)
    assert listing["media_fetched_at"] and listing["media_error"] is None
    assert listing["contact_name"] == "Frau Erika Mustermann" and "Wohnungstyp: Penthouse" in listing["details"]
    path = media.media_file(listing_id, 1)
    with Image.open(path) as img:  # 1500x1000 webp -> 1024x683 JPEG
        assert img.format == "JPEG" and img.size == (1024, 683)

    again = asyncio.run(media.ensure_media(listing_id))
    assert again == rows and len(is24_server.requests) == 12  # cached: no network


def test_ensure_media_one_keep_alive_client(fresh_db, is24_server, monkeypatch):
    clients = []
    real_client = httpx.AsyncClient

    def counting_client(*args, **kwargs):
        clients.append(kwargs.get("follow_redirects"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(is24.httpx, "AsyncClient", counting_client)
    asyncio.run(media.ensure_media(add_is24_listing(fresh_db)))
    assert clients == [False, False]  # one for the exposé, one for all pictures; no redirects followed


@pytest.mark.parametrize("status", [403, 429])
def test_download_stops_at_refusal(fresh_db, monkeypatch, status):
    server = FakeIs24(fail_at=4, fail_status=status)
    serve(monkeypatch, server)
    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0)
    listing_id = add_is24_listing(fresh_db)
    rows = asyncio.run(media.ensure_media(listing_id))
    assert [r["position"] for r in rows] == [1, 2, 3]  # kept what worked
    assert len(server.picture_urls) == 4  # and asked for nothing after the refusal
    listing = fresh_db.get_listing(listing_id)
    assert f"HTTP {status}" in listing["media_error"] and "abgebrochen" in listing["media_error"]
    assert f"Inserat {listing_id} (ImmoScout24): Bilder-Server lehnt ab" in fresh_db.get_meta("media_last_error")


def test_download_stops_at_non_image_content(fresh_db, monkeypatch):
    server = FakeIs24(content_type="text/html")
    serve(monkeypatch, server)
    listing_id = add_is24_listing(fresh_db)
    assert asyncio.run(media.ensure_media(listing_id)) == []
    assert len(server.picture_urls) == 1 and "kein Bild" in fresh_db.get_listing(listing_id)["media_error"]


def test_failed_expose_is_not_retried_automatically(fresh_db, monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503)

    serve(monkeypatch, handler)
    listing_id = add_is24_listing(fresh_db)
    assert asyncio.run(media.ensure_media(listing_id)) == []
    assert "HTTP 503" in fresh_db.get_listing(listing_id)["media_error"]
    asyncio.run(media.ensure_media(listing_id))  # e.g. the next draft
    asyncio.run(media.ensure_media(listing_id, retry=True))  # a user retry within a minute
    assert len(calls) == 1
    an_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    fresh_db.set_media_state(listing_id, an_hour_ago, "HTTP 503")
    server = FakeIs24()
    serve(monkeypatch, server)
    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0)
    assert len(asyncio.run(media.ensure_media(listing_id, retry=True))) == 11  # the user's retry works
    assert fresh_db.get_listing(listing_id)["media_error"] is None


def test_concurrent_triggers_fetch_once(fresh_db, is24_server):
    listing_id = add_is24_listing(fresh_db)

    async def main():
        return await asyncio.gather(*(media.ensure_media(listing_id) for _ in range(3)))

    results = asyncio.run(main())
    assert all(len(r) == 11 for r in results)
    assert len(is24_server.requests) == 12


def test_other_portals_never_fetch(fresh_db, is24_server):
    other = add_is24_listing(fresh_db, "o1", provider="immowelt", url="https://www.immowelt.de/expose/1")
    austria = add_is24_listing(fresh_db, "o2", provider="immoscoutAt", url="https://www.immobilienscout24.at/expose/2")
    assert asyncio.run(media.ensure_media(other)) == [] and asyncio.run(media.ensure_media(austria)) == []
    assert asyncio.run(media.ensure_media(999)) == []
    assert is24_server.requests == []


def test_media_file_stays_inside_the_listing_folder(fresh_db, is24_server):
    listing_id = add_is24_listing(fresh_db)
    asyncio.run(media.ensure_media(listing_id))
    assert media.media_file(listing_id, 1).parent == (media.media_root() / str(listing_id)).resolve()
    assert media.media_file(listing_id, 99) is None
    fresh_db.replace_media(listing_id, [{"position": 1, "caption": "", "source_url": "x", "file": "../../immobot.db"}])
    assert media.media_file(listing_id, 1) is None


def test_read_pictures(fresh_db, is24_server):
    listing_id = add_is24_listing(fresh_db)
    rows = asyncio.run(media.ensure_media(listing_id))
    (media.media_root() / str(listing_id) / "2.jpg").unlink()  # a missing file is skipped
    pictures = media.read_pictures(listing_id, rows[:3])
    assert [row["position"] for row, _ in pictures] == [1, 3]
    assert all(jpeg[:2] == b"\xff\xd8" for _, jpeg in pictures)


def test_cleanup_old_media(fresh_db, is24_server):
    old = add_is24_listing(fresh_db, "old")
    new = add_is24_listing(fresh_db, "new")
    asyncio.run(media.ensure_media(old))
    asyncio.run(media.ensure_media(new))
    sixty_one_days_ago = (datetime.now(timezone.utc) - timedelta(days=61)).isoformat(timespec="seconds")
    with fresh_db.connect() as conn:
        conn.execute("UPDATE listings SET received_at = ? WHERE id = ?", (sixty_one_days_ago, old))
    assert media.cleanup_old_media() == 1
    assert fresh_db.list_media(old) == [] and not (media.media_root() / str(old)).exists()
    assert fresh_db.get_listing(old)["media_fetched_at"] is None
    assert "Penthouse" in fresh_db.get_listing(old)["details"]  # the text stays
    assert len(fresh_db.list_media(new)) == 11 and (media.media_root() / str(new) / "1.jpg").exists()
    assert media.cleanup_old_media() == 0


def test_listing_fields_like_fredy():
    assert is24.listing_fields(EXPOSE) == {
        "title": "Handwerklich geschickte Mieter gesucht", "price": "1.100 €", "size": "85 m²",
        "address": "Musterstraße 1, 12345 Musterstadt",
        "description": EXPOSE_DESCRIPTION}
    junk = {"sections": [None, {"type": "TOP_ATTRIBUTES", "attributes": "x"}, {"type": "TITLE"}, {"type": "MAP"}]}
    assert is24.listing_fields(junk) == {"title": "", "price": "", "size": "", "address": "", "description": ""}
    assert is24.canonical_url("123") == "https://www.immobilienscout24.de/expose/123"


EXPOSE_DESCRIPTION = ("Helle Dachgeschosswohnung in einem gepflegten Mehrfamilienhaus.\n\nDrei Balkone, offene Küche, "
                      "zwei Schlafzimmer. Das Bad hat eine Wanne, dazu ein Gäste-WC. Ein Stellplatz kann angemietet werden.")


# --- exposé link in shared text, text-only fetch (warm rent) -------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("https://www.immobilienscout24.de/expose/171725784", "171725784"),
    ("Schau dir dieses Angebot an: Helle 2-Zimmer-Wohnung\nhttps://www.immobilienscout24.de/expose/171725784?referrer=APP_SHARE&utm_x=1",
     "171725784"),
    ("www.immobilienscout24.de/expose/42#/gallery", "42"),
    ("https://evilimmobilienscout24.de/expose/42", None),
    ("https://www.immowelt.de/expose/abc", None),
    ("", None),
])
def test_expose_in_text(text, expected):
    assert is24.expose_in_text(text) == expected


def test_ensure_details_fetches_text_only_then_pictures_on_first_use(fresh_db, is24_server):
    listing_id = add_is24_listing(fresh_db, price="1.100 €")
    assert asyncio.run(media.ensure_details(listing_id)) is True
    assert len(is24_server.requests) == 1  # the exposé, no pictures yet
    listing = fresh_db.get_listing(listing_id)
    assert "Warmmiete: 1.400 €" in listing["details"] and listing["media_error"] is None
    assert media.stored_pictures(listing_id) == [] and len(fresh_db.list_media(listing_id)) == 11
    assert asyncio.run(media.ensure_details(listing_id)) is False  # once

    rows = asyncio.run(media.ensure_media(listing_id))
    assert len(rows) == 11 and len(is24_server.requests) == 12  # pictures now, no second exposé request
    assert asyncio.run(media.ensure_media(listing_id)) == rows and len(is24_server.requests) == 12


def test_ensure_details_skips_other_sources(fresh_db, is24_server):
    other = add_is24_listing(fresh_db, fredy_id="iw", url="https://www.immowelt.de/expose/abc", provider="immowelt")
    assert asyncio.run(media.ensure_details(other)) is False and is24_server.requests == []


def test_backfill_details_newest_first_once(fresh_db, is24_server, monkeypatch):
    first = add_is24_listing(fresh_db, fredy_id="a")
    second = add_is24_listing(fresh_db, fredy_id="b")
    done = []
    monkeypatch.setattr(media, "_on_details", done.append)
    assert asyncio.run(media.backfill_details(gap_s=0)) == 2
    assert done == [second, first] and len(is24_server.requests) == 2
    assert asyncio.run(media.backfill_details(gap_s=0)) == 0 and len(is24_server.requests) == 2


def test_refusal_pauses_automatic_requests_and_is_retried_later(fresh_db, monkeypatch):
    status = {"code": 429}

    def server(request):
        if status["code"] != 200:
            return httpx.Response(status["code"])
        return httpx.Response(200, json=EXPOSE)

    serve(monkeypatch, server)
    first = add_is24_listing(fresh_db, fredy_id="a")
    second = add_is24_listing(fresh_db, fredy_id="b")
    assert asyncio.run(media.ensure_details(second)) is False and media.is24_paused()
    assert fresh_db.get_listing(second)["media_fetched_at"] is None  # not recorded: tried again later
    assert asyncio.run(media.ensure_details(first)) is False  # paused: no request at all

    # a listing refused by the earlier version (recorded as 429) counts as missing too
    fresh_db.set_media_state(first, fresh_db.now_iso(), "Exposé nicht abrufbar (HTTP 429).")
    status["code"] = 200
    monkeypatch.setattr(media, "_paused_until", 0.0)  # the pause is over
    assert asyncio.run(media.backfill_details(gap_s=0)) == 2
    assert "Warmmiete" in fresh_db.get_listing(first)["details"] and fresh_db.get_listing(first)["media_error"] is None


def test_gone_listing_is_recorded_and_not_asked_again(fresh_db, monkeypatch):
    requests = []
    serve(monkeypatch, lambda request: requests.append(request) or httpx.Response(404))
    listing_id = add_is24_listing(fresh_db)
    assert asyncio.run(media.ensure_details(listing_id)) is False and not media.is24_paused()
    assert "HTTP 404" in fresh_db.get_listing(listing_id)["media_error"]
    assert asyncio.run(media.backfill_details(gap_s=0)) == 0 and len(requests) == 1


def test_big_batch_fetches_only_the_first_ones_right_away(fresh_db, is24_server, monkeypatch):
    monkeypatch.setattr(media, "NEW_DETAILS_MAX", 2)
    started = []
    monkeypatch.setattr(media, "schedule_backfill", lambda: started.append(True))
    ids = [add_is24_listing(fresh_db, fredy_id=f"x{i}") for i in range(4)]
    asyncio.run(media.fetch_details(ids, gap_s=0))
    assert len(is24_server.requests) == 2 and started == [True]
    assert [bool(fresh_db.get_listing(i)["details"]) for i in ids] == [True, True, False, False]


def test_reload_fetches_the_exposé_again_at_most_once_a_minute(fresh_db, is24_server):
    listing_id = add_is24_listing(fresh_db)
    assert media.pictures_missing(fresh_db.get_listing(listing_id))
    assert asyncio.run(media.ensure_details(listing_id)) is True
    assert media.pictures_missing(fresh_db.get_listing(listing_id))  # only known by URL
    assert asyncio.run(media.reload(listing_id)) is False  # just fetched
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    fresh_db.set_media_state(listing_id, old, None)
    assert asyncio.run(media.reload(listing_id)) is True
    assert len(is24_server.requests) == 2 + 11 and not media.pictures_missing(fresh_db.get_listing(listing_id))
