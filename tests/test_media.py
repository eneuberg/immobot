"""Tests for app/media.py: picture sources (Fredy's webhook images > own ImmoScout24 fetch > single image),
Fredy image validation and storage, the "is24_fetch" switch. No network: a fake server answers everything,
and the conftest guard fails any request that isn't routed to it."""

import asyncio
import dataclasses
import functools
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import bot, db, is24, llm, main, media, web
from tests.conftest import TEST_SECRETS

EXPOSE = json.loads((Path(__file__).parent / "fixtures" / "is24_expose.json").read_text())
IS24_URL = "https://www.immobilienscout24.de/expose/123456789"


@functools.cache
def jpeg_bytes() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (1200, 800), (90, 140, 200)).save(out, format="JPEG")
    return out.getvalue()


class FakeHosts:
    """Answers the IS24 exposé API with the fixture and every picture URL with an image; records requests."""

    def __init__(self, fail_at=None):
        self.requests: list[httpx.Request] = []
        self.fail_at = fail_at

    def __call__(self, request):
        self.requests.append(request)
        if request.url.host == "api.mobile.immobilienscout24.de":
            return httpx.Response(200, json=EXPOSE)
        if self.fail_at is not None and len(self.pictures) == self.fail_at:
            return httpx.Response(429)
        return httpx.Response(200, content=jpeg_bytes(), headers={"content-type": "image/jpeg"})

    @property
    def pictures(self):
        return [r for r in self.requests if r.url.host != "api.mobile.immobilienscout24.de"]

    @property
    def is24_api_calls(self):
        return [r for r in self.requests if r.url.host == "api.mobile.immobilienscout24.de"]


@pytest.fixture
def hosts(monkeypatch):
    server = FakeHosts()
    for module in (is24, media):
        monkeypatch.setattr(module, "_transport", httpx.MockTransport(server))
    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0)
    return server


def add_listing(fredy_id="f1", provider="immoscout", url=IS24_URL, **extra):
    return db.insert_listing({"fredy_id": fredy_id, "provider": provider, "title": "Wohnung", "url": url, **extra})[0]


FREDY_IMAGES = [
    {"url": "https://pictures.immowelt.de/a.jpg", "caption": "Wohnzimmer"},
    {"url": "https://cdn.example-portal.de/grundriss.png", "caption": "Grundriss", "floorPlan": True},
    "https://pictures.immowelt.de/c.jpg",
]


# --- URL policy & parsing ------------------------------------------------------

@pytest.mark.parametrize("url,ok", [
    ("https://pictures.immowelt.de/a.jpg", True),
    ("https://cdn.example-portal.de:443/x.webp?w=1200", True),
    ("http://pictures.immowelt.de/a.jpg", False),  # https only
    ("https://192.168.1.10/a.jpg", False), ("https://10.0.0.1/a.jpg", False), ("https://[::1]/a.jpg", False),
    ("https://127.1/a.jpg", False), ("https://0x7f.1/a.jpg", False), ("https://2130706433/a.jpg", False),
    ("https://localhost/a.jpg", False), ("https://nas.local/a.jpg", False), ("https://db.internal/a.jpg", False),
    ("https://app.localhost/a.jpg", False), ("https://intranet/a.jpg", False),
    ("https://user:pw@pictures.immowelt.de/a.jpg", False),
    ("https://pictures.immowelt.de:8443/a.jpg", False),
    ("https://pictures.immowelt.de/" + "a" * 2000, False),
    ("https://pictures.immowelt.de/a b.jpg", False), ("javascript:alert(1)", False), (None, False), (42, False),
])
def test_fredy_image_url_policy(url, ok):
    assert media.fredy_image_url_ok(url) is ok


def test_parse_fredy_images():
    raw = FREDY_IMAGES + [
        {"url": "http://insecure.example.de/x.jpg"}, {"url": "https://10.0.0.1/x.jpg"}, "not a url", 17, None,
        {"caption": "ohne URL"}, {"url": "https://pictures.immowelt.de/a.jpg", "caption": "doppelt"},
        {"url": "https://pictures.immowelt.de/d.jpg", "caption": "  Bad \n mit   Fenster " + "x" * 300, "floorPlan": "yes"},
    ]
    pictures = media.parse_fredy_images(raw)
    assert [p["url"].rsplit("/", 1)[1] for p in pictures] == ["grundriss.png", "a.jpg", "c.jpg", "d.jpg"]  # floor plan first
    assert [p["position"] for p in pictures] == [1, 2, 3, 4]
    assert [p["floor_plan"] for p in pictures] == [True, False, False, False]  # only a real bool counts
    assert pictures[1]["caption"] == "Wohnzimmer" and pictures[2]["caption"] == ""
    assert pictures[3]["caption"].startswith("Bad mit Fenster x") and len(pictures[3]["caption"]) == 200


@pytest.mark.parametrize("raw", [None, "https://x.de/a.jpg", {"url": "https://x.de/a.jpg"}, 3, []])
def test_parse_fredy_images_not_a_list(raw):
    assert media.parse_fredy_images(raw) == []


def test_parse_fredy_images_limits():
    raw = [{"url": f"https://pictures.immowelt.de/{i}.jpg"} for i in range(80)]
    assert len(media.parse_fredy_images(raw)) == media.MAX_PICTURES  # 30 stored
    raw = [{"url": "https://10.0.0.1/x.jpg"}] * 50 + [{"url": "https://pictures.immowelt.de/late.jpg"}]
    assert media.parse_fredy_images(raw) == []  # only the first 50 entries are looked at


# --- storage -------------------------------------------------------------------------

def test_store_fredy_images_once(fresh_db):
    listing_id = add_listing(provider="immowelt", url="https://www.immowelt.de/expose/1")
    pictures = media.parse_fredy_images(FREDY_IMAGES)
    assert media.store_fredy_images(listing_id, pictures)
    rows = fresh_db.list_media(listing_id)
    assert [(r["position"], r["file"], r["floor_plan"]) for r in rows] == [(1, "", 1), (2, "", 0), (3, "", 0)]
    assert fresh_db.get_listing(listing_id)["media_source"] == "fredy"
    assert not media.store_fredy_images(listing_id, pictures)  # sent again: no duplicates
    assert len(fresh_db.list_media(listing_id)) == 3
    assert media.stored_pictures(listing_id) == []  # nothing downloaded yet
    assert media.media_file(listing_id, 1) is None


def test_store_fredy_images_keeps_existing_is24_pictures(fresh_db, hosts):
    listing_id = add_listing()
    asyncio.run(media.ensure_media(listing_id))  # own IS24 fetch: 11 pictures
    assert not media.store_fredy_images(listing_id, media.parse_fredy_images(FREDY_IMAGES))
    assert fresh_db.get_listing(listing_id)["media_source"] == "is24" and len(fresh_db.list_media(listing_id)) == 11


def test_store_fredy_images_after_failed_is24_attempt(fresh_db):
    listing_id = add_listing()
    fresh_db.set_media_state(listing_id, fresh_db.now_iso(), "Exposé nicht abrufbar (HTTP 404).")
    assert media.store_fredy_images(listing_id, media.parse_fredy_images(FREDY_IMAGES))
    listing = fresh_db.get_listing(listing_id)
    assert listing["media_fetched_at"] is None and listing["media_error"] is None  # downloads on next use


# --- precedence: Fredy > own IS24 fetch > single image --------------------------------

def test_fredy_images_win_and_is24_is_never_asked(fresh_db, hosts):
    listing_id = add_listing()  # an ImmoScout24 listing, own fetch switched on
    media.store_fredy_images(listing_id, media.parse_fredy_images(FREDY_IMAGES))
    rows = asyncio.run(media.ensure_media(listing_id))
    assert [r["position"] for r in rows] == [1, 2, 3] and all(r["file"] for r in rows)
    assert hosts.is24_api_calls == []
    assert [str(r.url) for r in hosts.pictures] == [
        "https://cdn.example-portal.de/grundriss.png", "https://pictures.immowelt.de/a.jpg", "https://pictures.immowelt.de/c.jpg"]
    assert hosts.pictures[0].headers["user-agent"].startswith("Mozilla/5.0")
    with Image.open(media.media_file(listing_id, 1)) as img:
        assert img.format == "JPEG" and max(img.size) == 1024
    asyncio.run(media.ensure_media(listing_id))
    assert len(hosts.requests) == 3  # cached


def test_own_is24_fetch_without_fredy_images(fresh_db, hosts):
    listing_id = add_listing()
    assert len(asyncio.run(media.ensure_media(listing_id))) == 11
    assert len(hosts.is24_api_calls) == 1
    listing = fresh_db.get_listing(listing_id)
    assert listing["media_source"] == "is24" and "Wohnungstyp: Penthouse" in listing["details"]


def test_other_portals_without_fredy_images_have_no_gallery(fresh_db, hosts):
    listing_id = add_listing(provider="immowelt", url="https://www.immowelt.de/expose/1")
    assert media.source_of(fresh_db.get_listing(listing_id)) is None
    assert asyncio.run(media.ensure_media(listing_id, retry=True)) == [] and hosts.requests == []


def test_fredy_download_failure_and_retry_continues(fresh_db, monkeypatch):
    server = FakeHosts(fail_at=3)  # the 3rd picture request gets a 429
    monkeypatch.setattr(media, "_transport", httpx.MockTransport(server))
    monkeypatch.setattr(media, "DOWNLOAD_GAP_S", 0)
    listing_id = add_listing(provider="immowelt", url="https://www.immowelt.de/expose/1")
    media.store_fredy_images(listing_id, media.parse_fredy_images(FREDY_IMAGES))
    assert [r["position"] for r in asyncio.run(media.ensure_media(listing_id))] == [1, 2]
    assert len(server.pictures) == 3  # stopped at the 429
    assert "HTTP 429" in fresh_db.get_listing(listing_id)["media_error"]
    assert "(Fredy-Bilder)" in fresh_db.get_meta("media_last_error")
    server.fail_at = None
    stale = "2000-01-01T00:00:00+00:00"
    fresh_db.set_media_state(listing_id, stale, "HTTP 429")
    assert [r["position"] for r in asyncio.run(media.ensure_media(listing_id, retry=True))] == [1, 2, 3]
    assert [str(r.url) for r in server.pictures[3:]] == ["https://pictures.immowelt.de/c.jpg"]  # only the missing one


def test_stored_url_that_breaks_the_policy_is_not_fetched(fresh_db, hosts):
    listing_id = add_listing(provider="immowelt", url="https://www.immowelt.de/expose/1")
    fresh_db.replace_media(listing_id, [{"position": 1, "caption": "", "source_url": "https://10.0.0.1/x.jpg", "file": ""}])
    fresh_db.set_media_source(listing_id, "fredy")
    assert asyncio.run(media.ensure_media(listing_id)) == [] and hosts.requests == []
    assert "nicht erlaubt" in fresh_db.get_listing(listing_id)["media_error"]


def test_without_pictures_wanted(fresh_db, hosts):
    fredy = add_listing("a", provider="immowelt", url="https://www.immowelt.de/expose/1")
    media.store_fredy_images(fredy, media.parse_fredy_images(FREDY_IMAGES))
    long_text = add_listing("b", url="https://www.immobilienscout24.de/expose/2", description="Viel Text. " * 60)
    asyncio.run(media.ensure_media(fredy, want_images=False))
    asyncio.run(media.ensure_media(long_text, want_images=False))
    assert hosts.requests == []  # nothing to download, Fredy's text is long enough
    short_text = add_listing("c", url="https://www.immobilienscout24.de/expose/3", description="Kurz.")
    asyncio.run(media.ensure_media(short_text, want_images=False))
    assert len(hosts.is24_api_calls) == 1  # the exposé is worth it for the text


# --- the "is24_fetch" switch: no ImmoScout24 request anywhere -----------------------------

def switch_off():
    db.update_settings({"is24_fetch": "0"})


def test_switch_off_no_is24_request_from_ensure_media(fresh_db):
    """The conftest guard fails the test on any request."""
    listing_id = add_listing()
    switch_off()
    assert media.source_of(fresh_db.get_listing(listing_id)) is None
    assert asyncio.run(media.ensure_media(listing_id)) == []
    assert asyncio.run(media.ensure_media(listing_id, retry=True)) == []


def test_switch_off_no_is24_request_from_drafts(fresh_db, monkeypatch):
    listing_id = add_listing(description="Kurz.")
    switch_off()
    seen = []

    async def generate(listing, settings, kind, previous_text=None, instruction=None, pictures=None, **learned):
        seen.append(pictures)
        return llm.DraftResult(text="x", hint=None, model="claude-opus-5-5", input_tokens=1, output_tokens=1,
                               cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.0)

    monkeypatch.setattr(llm, "generate_draft", generate)
    asyncio.run(llm.create_and_store_draft(listing_id, "initial"))
    assert seen == [None]  # Fredy's single image instead


def test_switch_off_no_is24_request_from_the_pictures_button(fresh_db, monkeypatch):
    listing_id = add_listing(tg_message_id=None)
    fresh_db.set_listing_tg_message(listing_id, 555)
    switch_off()
    monkeypatch.setattr(bot, "configured_chat_id", lambda: 42)
    sent = []
    fake_bot = SimpleNamespace(send_media_group=lambda **k: sent.append(k), send_photo=lambda **k: sent.append(k))
    query = SimpleNamespace(data=f"img:{listing_id}", message=SimpleNamespace(chat=SimpleNamespace(id=42)), answers=[])

    async def answer(text=None, **kwargs):
        query.answers.append(text)

    query.answer = answer
    update = SimpleNamespace(callback_query=query, message=None, effective_user=SimpleNamespace(id=1))
    asyncio.run(bot._on_callback(update, SimpleNamespace(bot=fake_bot)))
    assert query.answers == ["Für dieses Inserat gibt es keine Bildergalerie."] and sent == []


def test_switch_off_no_is24_request_from_the_dashboard(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)
    monkeypatch.setattr(main.app.state, "config", cfg)
    with TestClient(main.app, follow_redirects=False) as client:
        client.post("/login", data={"password": TEST_SECRETS["DASHBOARD_PASSWORD"]})
        listing_id = add_listing()
        switch_off()
        page = client.get(f"/listing/{listing_id}").text
        assert 'id="bilder"' not in page and "Bilder &amp; Details laden" not in page
        assert client.post(f"/listing/{listing_id}/media").status_code == 303  # does nothing
        assert db.get_listing(listing_id)["media_fetched_at"] is None


# --- Telegram button & dashboard ------------------------------------------------------------

def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def test_pictures_button_visibility(fresh_db):
    is24_listing = fresh_db.get_listing(add_listing("a"))
    other = fresh_db.get_listing(add_listing("b", provider="immowelt", url="https://www.immowelt.de/expose/1"))
    with_fredy = add_listing("c", provider="immowelt", url="https://www.immowelt.de/expose/2")
    media.store_fredy_images(with_fredy, media.parse_fredy_images(FREDY_IMAGES))
    with_fredy = fresh_db.get_listing(with_fredy)
    assert f"img:{is24_listing['id']}" in buttons(bot.followup_view(is24_listing)[1])
    assert f"img:{other['id']}" not in buttons(bot.followup_view(other)[1])
    assert f"img:{with_fredy['id']}" in buttons(bot.followup_view(with_fredy)[1])  # Fredy images, any portal
    switch_off()
    assert f"img:{is24_listing['id']}" not in buttons(bot.followup_view(is24_listing)[1])
    assert f"img:{with_fredy['id']}" in buttons(bot.draft_keyboard(with_fredy))


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)
    monkeypatch.setattr(main.app.state, "config", cfg)
    monkeypatch.setattr(web, "LOGIN_FAIL_DELAY_S", 0)
    monkeypatch.setattr(bot, "schedule_announce", lambda ids: None)
    with TestClient(main.app, follow_redirects=False) as c:
        c.post("/login", data={"password": TEST_SECRETS["DASHBOARD_PASSWORD"]})
        yield c


def test_dashboard_shows_the_source(client, hosts):
    fredy = add_listing("a", provider="immowelt", url="https://www.immowelt.de/expose/1")
    media.store_fredy_images(fredy, media.parse_fredy_images(FREDY_IMAGES))
    page = client.get(f"/listing/{fredy}").text
    assert "Bilder von Fredy" in page and ">Bilder laden<" in page and "die Fredy mitgeschickt hat" in page
    assert client.post(f"/listing/{fredy}/media").status_code == 303
    page = client.get(f"/listing/{fredy}").text
    assert "Bilder von Fredy" in page and "Bilder laden" not in page  # done: no button any more
    assert page.count(f'<img src="/listing/{fredy}/media/') >= 3
    assert f'href="/listing/{fredy}/media/2"' in page  # the hero is the first non-floor-plan picture
    own = add_listing("b")
    page = client.get(f"/listing/{own}").text
    assert "von ImmoScout24 geladen" in page and "Bilder &amp; Details laden" in page
    other = add_listing("c", provider="immowelt", url="https://www.immowelt.de/expose/3")
    assert 'id="bilder"' not in client.get(f"/listing/{other}").text


def test_overview_thumbnail_only_from_downloaded_pictures(client, hosts):
    listing_id = add_listing("a", provider="immowelt", url="https://www.immowelt.de/expose/1",
                             image_url="https://img.example/fredy.jpg")
    media.store_fredy_images(listing_id, media.parse_fredy_images(FREDY_IMAGES))
    assert f"/listing/{listing_id}/media/" not in client.get("/").text  # nothing downloaded: Fredy's image
    asyncio.run(media.ensure_media(listing_id))
    assert f'src="/listing/{listing_id}/media/2"' in client.get("/").text  # not the floor plan (position 1)


def test_settings_switch(client):
    page = client.get("/settings/verhalten").text
    assert 'name="is24_fetch" value="1" checked' in page
    assert "Bilder &amp; Details selbst von ImmoScout24 laden" in page
    assert ("Wenn Fredy keine Bilder mitschickt, holt immobot bei ImmoScout24-Inseraten das Exposé selbst (1 Abruf pro "
            "Inserat, nur bei Bedarf). Schickt Fredy die Bilder mit (Fredy-PR #489 bzw. gepatchtes Fredy), wird nichts "
            "zusätzlich abgerufen.") in page
    assert page.index('name="llm_max_images"') < page.index('name="is24_fetch"')  # right after the photo count


# --- webhook -----------------------------------------------------------------------------------

def hook(client, listings):
    return client.post("/hook/fredy", json={"jobId": "j", "provider": "immowelt", "listings": listings},
                       headers={"Authorization": f"Bearer {TEST_SECRETS['FREDY_WEBHOOK_TOKEN']}"})


def test_webhook_stores_images_without_duplicates(client):
    listing = {"id": "w1", "title": "T", "url": "https://www.immowelt.de/expose/1", "imageUrl": "https://img.example/1.jpg",
               "images": FREDY_IMAGES + [{"url": "http://insecure.example.de/x.jpg"}, {"url": "https://127.0.0.1/x"}, 5]}
    r = hook(client, [listing, {"id": "w2", "title": "ohne Bilder"}])
    assert r.status_code == 200 and r.json() == {"received": 2, "new": 2}  # bad entries don't fail the webhook
    rows = {r["fredy_id"]: r["id"] for r in db.list_listings()}
    stored = db.list_media(rows["w1"])
    assert [r["source_url"].rsplit("/", 1)[1] for r in stored] == ["grundriss.png", "a.jpg", "c.jpg"]
    assert db.get_listing(rows["w1"])["media_source"] == "fredy" and db.list_media(rows["w2"]) == []
    assert db.get_listing(rows["w1"])["image_url"] == "https://img.example/1.jpg"  # imageUrl unchanged
    hook(client, [listing])  # sent again
    assert len(db.list_media(rows["w1"])) == 3
    hook(client, [{"id": "w2", "title": "jetzt mit Bildern", "images": FREDY_IMAGES[:1]}])  # later webhook with images
    assert len(db.list_media(rows["w2"])) == 1 and db.get_listing(rows["w2"])["media_source"] == "fredy"


@pytest.mark.parametrize("images", ["not a list", {"url": "https://x.de/a.jpg"}, [None] * 60, 12])
def test_webhook_tolerates_bad_images(client, images):
    r = hook(client, [{"id": "b1", "title": "T", "images": images}])
    assert r.status_code == 200 and r.json()["received"] == 1
    assert db.list_media(db.list_listings()[0]["id"]) == []


def test_status_shows_whether_fredy_sends_images(client):
    assert "Fredy schickt Bilder mit:\n      noch unbekannt" in client.get("/status").text
    hook(client, [{"id": "s1", "title": "T"}])
    assert db.get_meta("last_webhook_had_images") == "0" and "Fredy schickt Bilder mit:" in client.get("/status").text
    assert ">ja<" not in client.get("/status").text.split("Fredy schickt Bilder mit:")[1][:200]
    hook(client, [{"id": "s2", "title": "T", "images": FREDY_IMAGES}])
    page = client.get("/status").text
    assert db.get_meta("last_webhook_had_images") == "1" and db.get_meta("fredy_images_last_at")
    assert '<span class="success">ja</span>' in page and "(letztes Inserat mit Bildern am" in page
    hook(client, [{"id": "s3", "title": "T"}])
    page = client.get("/status").text
    assert "nein" in page.split("Fredy schickt Bilder mit:")[1][:200] and "(letztes Inserat mit Bildern am" in page
