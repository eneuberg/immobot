import asyncio
import dataclasses
import hmac
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import bot, db, llm, main, settings, vault, web

from .conftest import TEST_SECRETS

PASSWORD = TEST_SECRETS["DASHBOARD_PASSWORD"]
TOKEN = TEST_SECRETS["FREDY_WEBHOOK_TOKEN"]

# Distinctive values entered through the dashboard; tests assert they never show up in a response.
DASHBOARD_SECRETS = {
    "anthropic_api_key": "sk-ant-DASHBOARD-ANTHROPIC-SECRET-777",
    "telegram_bot_token": "987654:DASHBOARDtelegramSECRETxxxxxxxxxxxxxx",
    "telegram_chat_id": "-100555",  # not secret
    "fredy_webhook_token": "dashboard-webhook-SECRET-" + "w" * 32,
    "deutschlandgpt_api_key": "dgpt-DASHBOARD-SECRET-0123456789abcdef",
}

# main.py may not register the middleware yet; it has to be added before the app first starts.
if not any(m.kwargs.get("dispatch") is web.security_headers for m in main.app.user_middleware):
    main.app.middleware("http")(web.security_headers)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)  # used by the lifespan -> db.init_db(tmp_path)
    monkeypatch.setattr(main.app.state, "config", cfg)  # used by web.py
    monkeypatch.setattr(web, "LOGIN_FAIL_DELAY_S", 0)
    monkeypatch.setattr(web, "_last_login_alert", {})
    monkeypatch.setattr(web, "_recent_failures", web.deque(maxlen=web.LOGIN_GLOBAL_ALERT_AT))
    monkeypatch.setattr(web, "_one_time", {})
    monkeypatch.setattr(bot, "schedule_announce", lambda ids: None)
    web._login_failures.clear()
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    web._login_failures.clear()


def login(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"


def save_credential(client, name, value, password=PASSWORD):
    return client.post(f"/credentials/save/{name}", data={"value": value, "password": password})


def enable_bot(monkeypatch):
    """Pretend the bot is enabled (IMMOBOT_DISABLE_BOT off) for this test."""
    monkeypatch.setattr(main.app.state, "config", dataclasses.replace(main.app.state.config, disable_bot=False))


def hook(client, payload, token=TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post("/hook/fredy", json=payload, headers=headers)


def fredy_listing(fredy_id, **extra):
    return {
        "id": fredy_id, "title": f"Wohnung {fredy_id}", "address": "Musterstr. 12", "price": "780 €", "size": "54 m²",
        "description": "Schöne Wohnung", "imageUrl": "https://img.example/1.jpg", "url": "https://portal.example/expose/1",
        "fredyUrl": "https://fredy.example/listings/1", **extra,
    }


def add_listing(**extra):
    data = {"fredy_id": "x1", "provider": "immowelt", "title": "2 Zi", "price": "780 €", "url": "https://portal.example/1", **extra}
    return db.insert_listing(data)[0]


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace llm.create_and_store_draft; records (listing_id, kind) and stores a draft like the real one."""
    calls = []

    async def create(listing_id, kind, instruction=None):
        calls.append((listing_id, kind))
        db.add_draft(listing_id, kind, f"Guten Tag, Entwurf {len(calls)}", "claude-haiku-5-5", hint="Codewort Linde",
                     input_tokens=1200, output_tokens=300, cost_usd=0.0123)
        return db.list_drafts(listing_id)[-1]

    monkeypatch.setattr(llm, "create_and_store_draft", create)
    return calls


# --- public endpoints & auth ------------------------------------------------

def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True}


def test_security_headers(client):
    r = client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "same-origin"  # no-referrer makes browsers send Origin: null
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


@pytest.mark.parametrize("method,path", [
    ("get", "/"), ("get", "/listing/1"), ("get", "/settings"), ("get", "/status"),
    ("post", "/settings"), ("post", "/settings/test"), ("post", "/settings/reset/profile"),
    ("post", "/listing/1/status"), ("post", "/listing/1/draft"),
    ("get", "/credentials"), ("post", "/credentials/save/anthropic_api_key"), ("post", "/credentials/clear/anthropic_api_key"),
    ("post", "/credentials/regenerate-webhook-token"), ("post", "/credentials/pair"), ("post", "/credentials/test/anthropic"),
])
def test_pages_require_login(client, fake_llm, method, path):
    data = {"value": DASHBOARD_SECRETS["anthropic_api_key"], "password": PASSWORD} if method == "post" else None
    r = getattr(client, method)(path, data=data) if data else getattr(client, method)(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert fake_llm == []
    assert db.list_credential_events() == [] and vault.source("anthropic_api_key") == "env"
    assert not vault.pairing_pending()


def test_wrong_password_rejected(client):
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401 and "Falsches Passwort" in r.text
    assert client.get("/").status_code == 303


def test_login_and_logout(client):
    login(client)
    assert client.get("/").status_code == 200
    assert client.get("/login").headers["location"] == "/"
    r = client.post("/logout")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 303


def test_login_rate_limit(client):
    for _ in range(web.LOGIN_MAX_FAILURES):
        assert client.post("/login", data={"password": "wrong"}).status_code == 401
    r = client.post("/login", data={"password": PASSWORD})  # even the right password is refused now
    assert r.status_code == 429 and "Zu viele Versuche" in r.text
    assert client.get("/").status_code == 303


def test_foreign_origin_rejected(client):
    login(client)
    lid = add_listing()
    r = client.post(f"/listing/{lid}/status", data={"status": "sent"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert db.get_listing(lid)["status"] == "new"
    r = client.post(f"/listing/{lid}/status", data={"status": "sent"}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    assert db.get_listing(lid)["status"] == "sent"


# --- webhook --------------------------------------------------------------------

def test_webhook_requires_token(client):
    payload = {"jobId": "j", "provider": "immowelt", "listings": [fredy_listing("a")]}
    assert hook(client, payload, token=None).status_code == 401
    assert hook(client, payload, token="wrong").status_code == 401
    assert hook(client, payload, token=TOKEN[:-1]).status_code == 401
    assert db.list_listings() == []


def test_webhook_inserts_dedupes_and_announces_new(client, monkeypatch):
    announced = []
    monkeypatch.setattr(bot, "schedule_announce", announced.append)
    monkeypatch.setattr(main.app.state, "config", dataclasses.replace(main.app.state.config, disable_bot=False))

    payload = {"jobId": "job1", "timestamp": 1, "provider": "immowelt",
               "listings": [fredy_listing("a"), fredy_listing("b"), {"title": "ohne id"}]}
    r = hook(client, payload)
    assert r.status_code == 200 and r.json() == {"received": 2, "new": 2}
    ids = {row["fredy_id"]: row["id"] for row in db.list_listings()}
    assert announced == [[ids["a"], ids["b"]]]

    row = db.get_listing(ids["a"])
    assert row["job_id"] == "job1" and row["provider"] == "immowelt" and row["image_url"] == "https://img.example/1.jpg"
    assert row["fredy_url"] == "https://fredy.example/listings/1" and row["size"] == "54 m²"
    assert db.get_meta("last_webhook_at")

    payload["listings"] = [fredy_listing("a"), fredy_listing("c")]
    assert hook(client, payload).json() == {"received": 2, "new": 1}
    new_id = next(row["id"] for row in db.list_listings() if row["fredy_id"] == "c")
    assert announced[-1] == [new_id]


def test_webhook_no_announce_when_bot_disabled(client, monkeypatch):
    announced = []
    monkeypatch.setattr(bot, "schedule_announce", announced.append)
    assert hook(client, {"jobId": "j", "provider": "p", "listings": [fredy_listing("a")]}).json()["new"] == 1
    assert announced == []


def test_webhook_ignores_price_change(client):
    r = hook(client, {"event": "priceChange", "jobId": "j", "listings": [fredy_listing("a")]})
    assert r.status_code == 200 and r.json() == {"ignored": True}
    assert db.list_listings() == []


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'{"listings": "nope"}', b"{}"])
def test_webhook_rejects_malformed(client, body):
    r = client.post("/hook/fredy", content=body, headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    assert r.status_code == 400


# --- listings -------------------------------------------------------------------

def test_listings_page_and_filter(client):
    login(client)
    a = add_listing(fredy_id="a", title="Altbau Nord")
    add_listing(fredy_id="b", title="Neubau Süd")
    db.set_listing_status(a, "sent")
    db.add_draft(a, "initial", "Hallo", "claude-opus-5-5", cost_usd=0.042)

    r = client.get("/")
    assert r.status_code == 200 and "Altbau Nord" in r.text and "Neubau Süd" in r.text
    assert "3,8 ct" in r.text and "USD" not in r.text  # 0,042 $ shown in euros (1 € = 1,10 $)
    r = client.get("/?status=sent")
    assert "Altbau Nord" in r.text and "Neubau Süd" not in r.text
    assert client.get("/?status=bogus").status_code == 200  # unknown filter = all


def test_listing_detail_escapes_and_shows_drafts(client):
    login(client)
    lid = add_listing(title="<script>alert(1)</script>", description="Zeile 1\n<b>fett</b>",
                      image_url="https://img.example/1.jpg", fredy_url="https://fredy.example/l/1")
    db.add_draft(lid, "custom", "Text <i>x</i>", "claude-sonnet-5-5", instruction="mit Hund", hint="Codewort",
                 input_tokens=111, output_tokens=22, cost_usd=0.5)
    r = client.get(f"/listing/{lid}")
    assert r.status_code == 200
    assert "<script>alert(1)" not in r.text and "&lt;script&gt;alert(1)" in r.text
    assert "<b>fett</b>" not in r.text and "<i>x</i>" not in r.text
    assert 'src="https://img.example/1.jpg"' in r.text and 'referrerpolicy="no-referrer"' in r.text
    assert 'href="https://fredy.example/l/1"' in r.text and "Inserat öffnen" in r.text
    for text in ["Anpassung", "mit Hund", "Codewort", "Claude Sonnet 5.5", "111", "45,5 ct (≈ 0,500 $)", "Kopieren"]:
        assert text in r.text
    assert client.get("/listing/9999").status_code == 404


def test_javascript_urls_not_rendered(client):
    login(client)
    lid = add_listing(url="javascript:alert(1)", image_url="javascript:alert(2)", fredy_url="JavaScript:alert(3)")
    r = client.get(f"/listing/{lid}")
    assert r.status_code == 200
    assert "javascript:" not in r.text.lower()
    assert "<img" not in r.text and "Inserat öffnen" not in r.text and "In Fredy öffnen" not in r.text


def test_only_safe_urls_with_stored_pictures(client):
    """With stored ImmoScout24 pictures the pages show images, but only from the local media route; Fredy's
    unsafe URLs (data:, javascript:, credentials in the URL) never become an image or a link."""
    login(client)
    lid = add_listing(provider="immoscout", url=IS24_URL, image_url="data:image/png;base64,AAAA",
                      fredy_url="https://user:pw@evil.example/x")
    add_listing(fredy_id="js", title="JS", image_url="javascript:alert(2)", fredy_url="JavaScript:alert(3)")
    store_listing_pictures(lid, ["Balkon", "Küche"])
    for path in [f"/listing/{lid}", "/"]:
        page = client.get(path).text
        main = page.split("<main", 1)[1]
        assert "javascript:" not in page.lower() and "data:image/png" not in page and "evil.example" not in page
        srcs = re.findall(r'<img[^>]*\ssrc="([^"]*)"', main)
        assert srcs and all(re.fullmatch(rf"/listing/{lid}/media/[12]", src) for src in srcs), srcs
        hrefs = re.findall(r'<a[^>]*\shref="([^"]*)"', main)
        allowed = ("/", "#", "https://www.immobilienscout24.de/expose/")
        assert all(h.startswith(allowed) and not h.startswith("//") for h in hrefs), hrefs


def test_listings_overview_thumbnails(client):
    login(client)
    add_listing(fredy_id="ok", title="Mit Bild", image_url="https://img.example/thumb.jpg")
    add_listing(fredy_id="bad", title="Böses Bild", image_url="javascript:alert(1)")
    add_listing(fredy_id="none", title="Ohne Bild")
    r = client.get("/")
    imgs = re.findall(r"<img[^>]*>", r.text)
    assert len(imgs) == 1  # only the plain https image; the others get the placeholder
    tag = imgs[0]
    assert 'src="https://img.example/thumb.jpg"' in tag and 'referrerpolicy="no-referrer"' in tag
    assert 'loading="lazy"' in tag and 'alt=""' in tag and "data-fallback" in tag
    assert "javascript:" not in r.text.lower() and r.text.count('class="thumb-ph"') == 3


def test_overview_thumbnail_prefers_stored_picture(client):
    login(client)
    lid = add_listing(provider="immoscout", url=IS24_URL, image_url="https://img.example/fredy.jpg")
    store_listing_pictures(lid, ["Grundriss", "Wohnzimmer"])  # IS24 puts floor plans first
    imgs = re.findall(r"<img[^>]*>", client.get("/").text)
    assert len(imgs) == 1 and f'src="/listing/{lid}/media/2"' in imgs[0] and "data-fallback" in imgs[0]
    assert db.thumb_positions() == {lid: 2}
    store_listing_pictures(lid, ["Grundriss"])  # only a floor plan: better than nothing
    assert db.thumb_positions() == {lid: 1}


def test_details_sections():
    text = ("Kaltmiete: 1.100 €\nZimmer: 3\n\nAdresse: Musterstraße 1, 12345 Musterstadt\n\n## Kosten\nNebenkosten: 300 €\n\n"
            "## Objektbeschreibung\nHelle Wohnung.\n\nZweiter Absatz: mit Doppelpunkt.\n\n## Sonstiges\nBitte melden.\n\n"
            "Anbieter: Frau Erika Mustermann, Hausverwaltung")
    sections = web.details_sections(text)
    assert sections[0] == {"title": None, "text": None, "facts": [
        ("Kaltmiete", "1.100 €"), ("Zimmer", "3"), ("Adresse", "Musterstraße 1, 12345 Musterstadt"),
        ("Anbieter", "Frau Erika Mustermann, Hausverwaltung")]}
    assert sections[1] == {"title": "Kosten", "facts": [("Nebenkosten", "300 €")], "text": None}
    assert sections[2] == {"title": "Objektbeschreibung", "facts": None,
                           "text": "Helle Wohnung.\n\nZweiter Absatz: mit Doppelpunkt."}
    assert sections[3] == {"title": "Sonstiges", "facts": None, "text": "Bitte melden."}
    assert web.details_sections(None) == [] and web.details_sections("Nur Text") == [
        {"title": None, "facts": None, "text": "Nur Text"}]


def test_static_assets_served_without_login(client):
    for path, kind in [("/static/app.css", "text/css"), ("/static/app.js", "javascript")]:
        r = client.get(path)
        assert r.status_code == 200 and kind in r.headers["content-type"]
        assert r.headers["cache-control"] == "public, max-age=86400"  # only /static is cacheable (and the pictures)
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]  # security headers apply here too
        assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/static/../app/web.py").status_code == 404
    missing = client.get("/static/nope.css")
    assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
    page = client.get("/login")
    assert page.headers["cache-control"] == "no-store"
    version = web.templates.env.globals["STATIC_VERSION"]  # content hash: a deploy changes the URL
    assert re.fullmatch(r"[0-9a-f]{12}", version)
    assert f'href="/static/app.css?v={version}"' in page.text and f'src="/static/app.js?v={version}"' in page.text

def test_listing_status_change(client):
    login(client)
    lid = add_listing()
    r = client.post(f"/listing/{lid}/status", data={"status": "dismissed"})
    assert r.status_code == 303 and db.get_listing(lid)["status"] == "dismissed"
    assert client.post(f"/listing/{lid}/status", data={"status": "bogus"}).status_code == 400
    assert db.get_listing(lid)["status"] == "dismissed"
    assert client.post("/listing/9999/status", data={"status": "sent"}).status_code == 404


def test_create_draft_initial_then_regenerate(client, fake_llm):
    login(client)
    lid = add_listing()
    r = client.post(f"/listing/{lid}/draft")
    assert r.status_code == 303 and r.headers["location"].startswith(f"/listing/{lid}#d")
    client.post(f"/listing/{lid}/draft")
    assert fake_llm == [(lid, "initial"), (lid, "regenerate")]
    assert "Entwurf 2" in client.get(f"/listing/{lid}").text


def test_create_draft_shows_llm_error(client, monkeypatch):
    async def fail(listing_id, kind, instruction=None):
        raise llm.BudgetExceeded("Monatsbudget erreicht")

    monkeypatch.setattr(llm, "create_and_store_draft", fail)
    login(client)
    lid = add_listing()
    r = client.post(f"/listing/{lid}/draft")
    assert r.status_code == 200 and "Monatsbudget erreicht" in r.text


# --- settings -------------------------------------------------------------------

def valid_settings(**overrides):
    form = {
        "system_prompt": "Sei nett.", "profile": "Erika, 34", "wishes": "Balkon", "style": "locker",
        "target_words": "120", "model": "claude-haiku-5-5", "effort": "medium", "monthly_budget_eur": "7,5",
        "send_image": "1", "followup_delay_s": "10", "paused": "1", "is24_fetch": "1",
    }
    form.update(overrides)
    return form


def test_settings_page_renders_all_fields(client):
    login(client)
    r = client.get("/settings")
    assert r.status_code == 200
    for key in settings.DEFAULT_SETTINGS:
        if key == "model":  # one model select per provider panel
            assert all(f'name="model_{provider}"' in r.text for provider in settings.PROVIDERS)
        else:
            assert f'name="{key}"' in r.text
    assert "Opus 5.5 – 4,00 $ / 20,00 $" in r.text and "Sonnet 5.5 – 3,30 € / 16,50 €" in r.text


def test_settings_save(client):
    login(client)
    r = client.post("/settings", data=valid_settings())
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    stored = db.get_settings()
    assert stored["profile"] == "Erika, 34" and stored["target_words"] == "120" and stored["model"] == "claude-haiku-5-5"
    assert stored["monthly_budget_eur"] == "7.5" and stored["send_image"] == "1" and stored["paused"] == "1"
    assert "Gespeichert" in client.get("/settings?saved=1").text


def test_settings_checkbox_off(client):
    login(client)
    client.post("/settings", data=valid_settings())
    form = valid_settings()
    del form["send_image"], form["paused"]
    assert client.post("/settings", data=form).status_code == 303
    assert db.get_settings()["send_image"] == "0" and db.get_settings()["paused"] == "0"


@pytest.mark.parametrize("field,value", [
    ("target_words", "5"), ("target_words", "abc"), ("followup_delay_s", "61"), ("model", "gpt-4"),
    ("effort", "max"), ("monthly_budget_eur", "-1"), ("monthly_budget_eur", "nan"), ("monthly_budget_eur", "x"),
    ("system_prompt", "   "),
])
def test_settings_validation(client, field, value):
    login(client)
    before = db.get_settings()
    r = client.post("/settings", data=valid_settings(**{field: value}))
    assert r.status_code == 400 and "Nicht gespeichert" in r.text
    assert db.get_settings() == before


def test_settings_unknown_keys_ignored(client):
    login(client)
    form = valid_settings(anthropic_api_key="sk-evil", evil="1")
    assert client.post("/settings", data=form).status_code == 303
    with db.connect() as conn:
        keys = {row["key"] for row in conn.execute("SELECT key FROM settings")}
    assert keys <= set(settings.DEFAULT_SETTINGS)


def test_settings_reset(client):
    login(client)
    client.post("/settings", data=valid_settings())
    r = client.post("/settings/reset/profile")
    assert r.status_code == 303
    assert db.get_settings()["profile"] == settings.DEFAULT_PROFILE
    assert db.get_settings()["wishes"] == "Balkon"
    assert client.post("/settings/reset/model").status_code == 404


def test_settings_test_draft(client, fake_llm):
    login(client)
    r = client.post("/settings/test")
    assert r.status_code == 200 and "Noch kein Inserat" in r.text and fake_llm == []

    add_listing(fredy_id="old", title="Alt")
    newest = add_listing(fredy_id="new", title="Neueste Wohnung")
    r = client.post("/settings/test")
    assert fake_llm == [(newest, "test")]
    for text in ["Test-Entwurf", "Neueste Wohnung", "Guten Tag, Entwurf 1", "Codewort Linde", "1,1 ct (≈ 0,012 $)", "Claude Haiku 5.5"]:
        assert text in r.text


# --- status & secrets -------------------------------------------------------------

def test_status_page(client):
    login(client)
    r = client.get("/status")
    assert r.status_code == 200
    assert "Anthropic-API-Key" in r.text and "/hook/fredy" in r.text and settings.APP_VERSION in r.text
    assert "gesetzt (Env-Variable)" in r.text and "Noch keine Änderungen" in r.text


def test_status_page_bot_states_and_sources(client, monkeypatch):
    login(client)
    enable_bot(monkeypatch)
    vault.init(TEST_SECRETS["SESSION_SECRET"], {})  # nothing in env
    r = client.get("/status")
    assert "nicht konfiguriert" in r.text
    assert r.text.count("nicht gesetzt") == 6 and "Kein Anthropic-API-Key hinterlegt" in r.text  # + AI warning
    save_credential(client, "anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"])
    save_credential(client, "telegram_bot_token", DASHBOARD_SECRETS["telegram_bot_token"])
    monkeypatch.setattr(bot, "status", lambda: {"running": True, "last_update_at": None, "last_error": None})
    r = client.get("/status")
    assert "nicht konfiguriert" not in r.text and "noch kein Chat verbunden" in r.text
    assert "Key: <code>sk-ant-…-777</code> · gesetzt am" in r.text  # hint + date, never the value
    assert "Key: <code>987654:…xxxx</code> · gesetzt am" in r.text
    assert "gespeichert" in r.text  # audit log shown


def test_no_secret_in_any_response(client, fake_llm, monkeypatch):
    secrets = list(TEST_SECRETS.values()) + [DASHBOARD_SECRETS[name] for name in vault.SECRET_NAMES]
    # Bot "enabled" (schedule_announce is a no-op from the fixture), so /status renders bot.status().
    monkeypatch.setattr(main.app.state, "config", dataclasses.replace(main.app.state.config, disable_bot=False))
    # Pretend the bot and the LLM put secrets into their error messages: they must be redacted.
    monkeypatch.setattr(bot, "status", lambda: {
        "running": True, "last_update_at": "2026-10-09T10:00:00+00:00",
        "last_error": f"POST https://api.telegram.org/bot{TEST_SECRETS['TELEGRAM_BOT_TOKEN']}/getUpdates failed",
    })
    responses = [client.get("/login"), client.get("/"), client.get("/healthz")]
    responses.append(client.post("/login", data={"password": "wrong"}))
    responses.append(hook(client, {"listings": []}, token="wrong"))
    responses.append(hook(client, {"jobId": "j", "provider": "immowelt", "listings": [fredy_listing("a")]}))
    responses.append(client.post("/login", data={"password": PASSWORD}))
    lid = db.list_listings()[0]["id"]
    responses += [client.post(f"/listing/{lid}/draft"), client.post("/settings/test")]
    for path in ["/", "/?status=new", f"/listing/{lid}", "/settings", "/settings?saved=1", "/login"]:
        responses.append(client.get(path))
    status = client.get("/status")
    assert "läuft" in status.text and "***" in status.text  # bot status rendered, token redacted
    responses.append(status)
    responses.append(client.post("/settings", data=valid_settings(target_words="1")))

    async def leaky(listing_id, kind, instruction=None):
        raise llm.LLMError(f"Fehler mit Key {TEST_SECRETS['ANTHROPIC_API_KEY']}")

    monkeypatch.setattr(llm, "create_and_store_draft", leaky)
    failed = client.post(f"/listing/{lid}/draft")
    assert "Fehler mit Key ***" in failed.text  # LLM error shown, key redacted
    responses += [failed, client.post("/settings/test")]

    # Credentials entered in the dashboard are never shown again - not on any page, not on error paths.
    for name, value in DASHBOARD_SECRETS.items():
        assert save_credential(client, name, value).status_code == 303
    monkeypatch.setattr(bot, "status", lambda: {
        "running": True, "last_update_at": None,
        "last_error": f"getUpdates failed for bot{DASHBOARD_SECRETS['telegram_bot_token']} / {TEST_SECRETS['TELEGRAM_BOT_TOKEN']}",
    })
    for path in ["/credentials", "/credentials?saved=anthropic_api_key", "/status", "/settings", "/", "/?status=new",
                 f"/listing/{lid}"]:
        responses.append(client.get(path))
    rejected_value = "INVALID-" + DASHBOARD_SECRETS["telegram_bot_token"]
    secrets.append(rejected_value)
    responses.append(save_credential(client, "telegram_bot_token", rejected_value))  # validation error
    responses.append(save_credential(client, "anthropic_api_key", "sk-ant-NEW", password="wrong"))  # step-up error
    responses.append(client.post(f"/listing/{lid}/draft"))  # LLM error path again, now with dashboard key

    async def leaky_telegram():
        raise bot.CheckFailed(f"kaputt {DASHBOARD_SECRETS['telegram_bot_token']} {TEST_SECRETS['TELEGRAM_BOT_TOKEN']}")

    async def leaky_anthropic(provider):
        raise llm.LLMError(f"kaputt {DASHBOARD_SECRETS['anthropic_api_key']} {TEST_SECRETS['ANTHROPIC_API_KEY']}")

    monkeypatch.setattr(bot, "check_connection", leaky_telegram)
    monkeypatch.setattr(llm, "check_api_key", leaky_anthropic)
    for target in ("telegram", "anthropic"):
        tested = client.post(f"/credentials/test/{target}")
        assert "kaputt ***" in tested.text  # error shown, secrets redacted
        responses.append(tested)

    redirect = client.post("/credentials/regenerate-webhook-token", data={"password": PASSWORD})
    new_token = vault.get("fredy_webhook_token")
    assert redirect.status_code == 303 and new_token not in redirect.headers["location"]
    responses.append(redirect)
    regenerated = client.get(redirect.headers["location"])
    assert regenerated.status_code == 200 and new_token in regenerated.text  # the one intended display
    for secret in secrets:
        assert secret not in regenerated.text
    secrets.append(new_token)
    responses += [client.get("/credentials"), client.get("/status"), client.get(redirect.headers["location"])]
    responses.append(client.post("/credentials/clear/anthropic_api_key", data={"password": PASSWORD}))
    responses += [client.get("/credentials?cleared=anthropic_api_key"), client.post("/logout"), client.get("/login")]

    for r in responses:
        assert r.status_code < 500, r.request.url
        exposed = r.text + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for secret in secrets:
            assert secret not in exposed, f"secret leaked in {r.request.method} {r.request.url}"


def test_browser_style_origins_accepted_for_login(client):
    # Real browsers send the page's own origin (or "null" under strict privacy settings) on form posts.
    r = client.post("/login", data={"password": PASSWORD}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303
    client.cookies.clear()
    r = client.post("/login", data={"password": PASSWORD}, headers={"Origin": "null"})
    assert r.status_code == 303


def test_login_limit_is_per_client_ip(client):
    for _ in range(web.LOGIN_MAX_FAILURES):  # TestClient's peer address is "testclient"
        client.post("/login", data={"password": "wrong"})
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429
    # A stranger's failures don't lock out a different client.
    web._login_failures["testclient"] = web.deque()
    web._login_failures["203.0.113.9"] = web.deque([web.time.monotonic()] * web.LOGIN_MAX_FAILURES)
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303


def test_logout_revokes_copied_session_cookie(client):
    login(client)
    stolen = dict(client.cookies)
    assert client.get("/").status_code == 200
    client.post("/logout")
    client.cookies.clear()
    client.cookies.update(stolen)
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_unexpected_draft_error_shows_generic_message(client, monkeypatch):
    login(client)
    hook(client, {"jobId": "j", "provider": "immoscout", "listings": [{"id": "boom1", "title": "T"}]})
    lid = db.list_listings()[0]["id"]

    async def explode(*args, **kwargs):
        raise RuntimeError("internal detail that must not be shown")

    monkeypatch.setattr(llm, "create_and_store_draft", explode)
    r = client.post(f"/listing/{lid}/draft")
    assert r.status_code == 200
    assert "Unerwarteter Fehler" in r.text and "internal detail" not in r.text


# --- credentials ("Zugänge") ------------------------------------------------------

@pytest.fixture
def notices(monkeypatch):
    """Record bot.notify() calls instead of sending Telegram messages."""
    sent = []

    async def notify(text, chat):
        sent.append((text, chat))

    monkeypatch.setattr(bot, "notify", notify)
    return sent


def events():
    return [(e["name"], e["action"], e["client_ip"]) for e in db.list_credential_events()]


def test_credentials_page_shows_status_never_values(client):
    login(client)
    r = client.get("/credentials")
    assert r.status_code == 200
    assert r.text.count("aus Env-Variable – im Dashboard überschreiben und danach in Coolify löschen") == 4
    assert "Aktuelle Chat-ID: <code>42</code>" in r.text  # the chat id is not secret
    assert "SESSION_SECRET" in r.text and 'href="/credentials" aria-current="page"' in r.text

    save_credential(client, "anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"])
    r = client.get("/credentials")
    assert "gesetzt über Dashboard am" in r.text
    value_inputs = re.findall(r"<input[^>]*name=\"value\"[^>]*>", r.text)
    assert len(value_inputs) == 6
    for tag in value_inputs:
        assert " value=" not in tag  # never prefilled
        # No second password field: password managers would offer to save the API key as the site password.
        assert 'type="text"' in tag and 'autocomplete="off"' in tag and 'spellcheck="false"' in tag
    assert sum('class="masked"' in tag for tag in value_inputs) == 4  # the secrets, masked by CSS
    password_inputs = re.findall(r"<input[^>]*type=\"password\"[^>]*>", r.text)
    assert len(password_inputs) == 6 and all('name="password"' in tag and "current-password" in tag for tag in password_inputs)
    assert DASHBOARD_SECRETS["anthropic_api_key"] not in r.text


def test_save_credential_with_audit_and_notice(client, notices):
    login(client)
    r = save_credential(client, "anthropic_api_key", "  " + DASHBOARD_SECRETS["anthropic_api_key"] + "  ")
    assert r.status_code == 303 and r.headers["location"] == "/credentials?saved=anthropic_api_key"
    assert vault.get("anthropic_api_key") == DASHBOARD_SECRETS["anthropic_api_key"]
    assert vault.source("anthropic_api_key") == "dashboard"
    assert events() == [("anthropic_api_key", "set", "testclient")]
    ((text, chat),) = notices
    assert chat == 42 and text.startswith("🔐 Zugang geändert: Anthropic-API-Key – gespeichert (Dashboard, ")
    assert DASHBOARD_SECRETS["anthropic_api_key"] not in text
    assert "„Anthropic-API-Key“ gespeichert." in client.get(r.headers["location"]).text


def test_save_credential_validation_error(client, notices):
    login(client)
    r = save_credential(client, "telegram_bot_token", "not-a-token")
    assert r.status_code == 400 and "Telegram-Bot-Token: Sieht nicht wie ein Bot-Token aus" in r.text
    assert "not-a-token" not in r.text
    assert vault.source("telegram_bot_token") == "env" and events() == [] and notices == []


def test_unknown_credential_and_test_target(client):
    login(client)
    assert save_credential(client, "dashboard_password", "x" * 40).status_code == 404
    assert client.post("/credentials/clear/session_secret", data={"password": PASSWORD}).status_code == 404
    assert client.post("/credentials/test/openai").status_code == 404


def test_credential_changes_need_dashboard_password(client, notices):
    login(client)
    for action in [lambda pw: save_credential(client, "anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"], pw),
                   lambda pw: client.post("/credentials/clear/fredy_webhook_token", data={"password": pw}),
                   lambda pw: client.post("/credentials/regenerate-webhook-token", data={"password": pw}),
                   lambda pw: client.post("/credentials/pair", data={"password": pw})]:
        for wrong in ["", "nope"]:
            web._login_failures.clear()
            r = action(wrong)
            assert r.status_code == 403 and "Dashboard-Passwort falsch" in r.text
            assert len(web._login_failures["testclient"]) == 1  # counted like a failed login
    assert events() == [] and notices == [] and not vault.pairing_pending()
    assert vault.source("anthropic_api_key") == "env" and vault.get("fredy_webhook_token") == TOKEN


def test_step_up_shares_the_login_limit(client):
    login(client)
    for _ in range(web.LOGIN_MAX_FAILURES):
        assert save_credential(client, "anthropic_api_key", "sk-ant-x", password="wrong").status_code == 403
    r = save_credential(client, "anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"])  # right password, refused
    assert r.status_code == 429 and "Zu viele Fehlversuche" in r.text
    assert vault.source("anthropic_api_key") == "env"
    client.cookies.clear()
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429  # login is blocked too


def test_clear_credential_falls_back_to_env(client, notices):
    login(client)
    save_credential(client, "fredy_webhook_token", DASHBOARD_SECRETS["fredy_webhook_token"])
    r = client.post("/credentials/clear/fredy_webhook_token", data={"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/credentials?cleared=fredy_webhook_token"
    assert vault.get("fredy_webhook_token") == TOKEN and vault.source("fredy_webhook_token") == "env"
    assert [e[:2] for e in events()] == [("fredy_webhook_token", "clear"), ("fredy_webhook_token", "set")]
    assert "entfernt" in notices[-1][0]


def test_bot_credential_changes_dont_block_the_request(client, monkeypatch, notices):
    """A new bot token restarts the bot in the background; a chat change needs no restart, only the catch-up."""
    login(client)
    enable_bot(monkeypatch)
    reloads, catch_ups = [], []
    monkeypatch.setattr(bot, "schedule_reload", lambda: reloads.append(vault.get("telegram_bot_token")))
    monkeypatch.setattr(bot, "schedule_announce", catch_ups.append)

    async def must_not_block():
        raise AssertionError("the request must not wait for a bot restart")

    monkeypatch.setattr(bot, "reload", must_not_block)
    waiting = add_listing(fredy_id="waiting")  # never announced, e.g. because no chat was connected
    save_credential(client, "anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"])
    save_credential(client, "fredy_webhook_token", DASHBOARD_SECRETS["fredy_webhook_token"])
    save_credential(client, "telegram_allowed_user_ids", "1001")
    assert reloads == [] and catch_ups == []
    assert save_credential(client, "telegram_bot_token", DASHBOARD_SECRETS["telegram_bot_token"]).status_code == 303
    assert reloads == [DASHBOARD_SECRETS["telegram_bot_token"]]  # scheduled with the new token in place
    save_credential(client, "telegram_chat_id", "-100555")
    assert reloads == [DASHBOARD_SECRETS["telegram_bot_token"]] and catch_ups == [[waiting]]
    client.post("/credentials/clear/telegram_chat_id", data={"password": PASSWORD})
    client.post("/credentials/clear/telegram_bot_token", data={"password": PASSWORD})
    assert len(reloads) == 2 and reloads[-1] == TEST_SECRETS["TELEGRAM_BOT_TOKEN"]  # env fallback again
    assert len(catch_ups) == 2
    # The notice about a chat change goes to the chat configured before it.
    assert [chat for text, chat in notices if "Chat-ID" in text] == [42, -100555]


def test_no_reload_when_bot_disabled(client, monkeypatch):
    login(client)

    def fail():
        raise AssertionError("bot is disabled")

    monkeypatch.setattr(bot, "schedule_reload", fail)
    monkeypatch.setattr(bot, "schedule_announce", lambda ids: fail())
    assert save_credential(client, "telegram_chat_id", "7").status_code == 303
    assert save_credential(client, "telegram_bot_token", DASHBOARD_SECRETS["telegram_bot_token"]).status_code == 303


def test_regenerated_webhook_token_is_shown_once(client, notices):
    login(client)
    r = client.post("/credentials/regenerate-webhook-token", data={"password": PASSWORD})
    token = vault.get("fredy_webhook_token")
    assert r.status_code == 303 and token != TOKEN and len(token) >= 43
    location = r.headers["location"]
    assert location.startswith("/credentials?token=") and token not in location  # post/redirect/get
    assert all(token not in v for k, v in r.headers.items())  # not in the redirect, not in the cookie
    page = client.get(location)
    assert f'<pre class="draft" id="new-token-value">{token}</pre>' in page.text and "HTTP-Adapter" in page.text
    assert page.headers["cache-control"] == "no-store"
    again = client.get(location)  # F5 / back: shown only once, and nothing is rotated
    assert token not in again.text and "schon angezeigt" in again.text
    assert vault.get("fredy_webhook_token") == token
    assert token not in client.get("/credentials").text and token not in client.get("/status").text
    assert [e[:2] for e in events()] == [("fredy_webhook_token", "regenerate")]
    assert "neu erzeugt" in notices[0][0] and token not in notices[0][0]
    # Fredy must use the new token now.
    payload = {"jobId": "j", "provider": "immowelt", "listings": [fredy_listing("a")]}
    assert hook(client, payload, token=TOKEN).status_code == 401
    assert hook(client, payload, token=token).status_code == 200


def test_one_time_token_expires_and_needs_login(client, monkeypatch):
    login(client)
    location = client.post("/credentials/regenerate-webhook-token", data={"password": PASSWORD}).headers["location"]
    token = vault.get("fredy_webhook_token")
    client.cookies.clear()
    r = client.get(location)  # the one-time id alone is useless without a login
    assert r.status_code == 303 and r.headers["location"] == "/login"
    login(client)
    now = web.time.monotonic()
    monkeypatch.setattr(web.time, "monotonic", lambda: now + web.ONE_TIME_TTL_S + 1)
    page = client.get(location)
    assert token not in page.text and "schon angezeigt" in page.text
    assert web._one_time == {}


def test_webhook_uses_dashboard_token_over_env(client):
    login(client)
    payload = {"jobId": "j", "provider": "immowelt", "listings": [fredy_listing("a")]}
    save_credential(client, "fredy_webhook_token", DASHBOARD_SECRETS["fredy_webhook_token"])
    assert hook(client, payload, token=TOKEN).status_code == 401  # old env token no longer accepted
    assert hook(client, payload, token=DASHBOARD_SECRETS["fredy_webhook_token"]).status_code == 200


def test_webhook_unavailable_without_token(client):
    vault.init(TEST_SECRETS["SESSION_SECRET"], {})
    payload = {"jobId": "j", "provider": "immowelt", "listings": [fredy_listing("a")]}
    for token in [None, "", TOKEN, "anything"]:
        r = hook(client, payload, token=token)
        assert r.status_code == 503 and r.json() == {"error": "unavailable"}
    assert db.list_listings() == []


def test_connection_tests(client, monkeypatch):
    login(client)

    async def anthropic_ok(provider):
        return "Anthropic-API-Key funktioniert."

    async def telegram_fails():
        raise bot.CheckFailed("Telegram lehnt den Bot-Token ab.")

    monkeypatch.setattr(llm, "check_api_key", anthropic_ok)
    monkeypatch.setattr(bot, "check_connection", telegram_fails)
    r = client.post("/credentials/test/anthropic")  # no step-up needed
    assert r.status_code == 200 and "✓ Anthropic-API-Key funktioniert." in r.text
    assert events() == []
    r = client.post("/credentials/test/telegram")
    assert r.status_code == 200 and "✗ Telegram lehnt den Bot-Token ab." in r.text
    assert events() == [("telegram_bot_token", "test_failed", "testclient")]


def test_connection_test_unexpected_error_is_generic(client, monkeypatch, caplog):
    login(client)

    async def explode(provider):
        raise RuntimeError(f"boom {TEST_SECRETS['ANTHROPIC_API_KEY']}")

    monkeypatch.setattr(llm, "check_api_key", explode)
    r = client.post("/credentials/test/anthropic")
    assert "Unerwarteter Fehler" in r.text and "boom" not in r.text
    assert TEST_SECRETS["ANTHROPIC_API_KEY"] not in caplog.text
    assert events() == [("anthropic_api_key", "test_failed", "testclient")]


def test_pairing_start(client, monkeypatch, notices):
    login(client)
    r = client.post("/credentials/pair", data={"password": PASSWORD})
    assert r.status_code == 409 and "Bot läuft nicht" in r.text  # bot disabled in tests
    enable_bot(monkeypatch)
    monkeypatch.setattr(bot, "username", lambda: None)  # enabled, but not running
    assert client.post("/credentials/pair", data={"password": PASSWORD}).status_code == 409
    assert not vault.pairing_pending() and events() == []

    monkeypatch.setattr(bot, "username", lambda: "immo_test_bot")
    r = client.post("/credentials/pair", data={"password": PASSWORD})
    assert r.status_code == 200
    # Always the exact command with @bot: in groups with privacy mode a bare "/pair" isn't seen by the bot.
    code = re.search(r"<code id=\"pair-command\">/pair@immo_test_bot ([0-9a-f]{8})</code>", r.text).group(1)
    stored = json.loads(db.get_meta(vault.PAIRING_META_KEY))
    pairing_key = vault._derive(TEST_SECRETS["SESSION_SECRET"], vault.PAIRING_KDF_INFO)
    assert stored["hmac"] == hmac.new(pairing_key, code.encode(), "sha256").hexdigest() and code not in json.dumps(stored)
    assert stored["bot_username"] == "immo_test_bot"
    assert events() == [("telegram_chat_id", "pair_start", "testclient")]
    assert "Pairing gestartet" in notices[0][0]
    page = client.get("/credentials").text
    assert code not in page and "Pairing-Code für @immo_test_bot ist noch bis" in page
    assert vault.check_pairing_code(code)


def test_credentials_page_shows_last_pairing(client):
    login(client)
    detail = "Chat „<i>Wohnung</i>“ (group, -100777) · User Erika (@erika_m, 1001)"
    db.add_credential_event("telegram_chat_id", "pair", "Telegram", detail)
    page = client.get("/credentials").text
    escaped = "Chat „&lt;i&gt;Wohnung&lt;/i&gt;“ (group, -100777) · User Erika (@erika_m, 1001)"
    assert "Zuletzt per Pairing verbunden am" in page and page.count(escaped) == 2  # chat box + audit log
    assert "<i>Wohnung</i>" not in page  # names from Telegram are escaped


# --- Telegram user allowlist in the dashboard ----------------------------------------

def test_allowlist_warning_and_value(client, notices):
    login(client)
    warning = "Jedes Mitglied des Chats kann Buttons drücken – Pairing erneut ausführen oder User-ID eintragen"
    assert warning in client.get("/credentials").text and warning in client.get("/status").text
    r = save_credential(client, "telegram_allowed_user_ids", " 1001, 2002,1001 ")
    assert r.status_code == 303
    assert vault.get("telegram_allowed_user_ids") == "1001,2002"
    page = client.get("/credentials").text
    assert warning not in page and "Aktuell erlaubt: <code>1001,2002</code>" in page
    status = client.get("/status").text
    assert warning not in status and "1001, 2002" in status
    assert "Erlaubte Telegram-User-IDs – gespeichert" in notices[-1][0]
    assert save_credential(client, "telegram_allowed_user_ids", "-5").status_code == 400
    assert save_credential(client, "telegram_allowed_user_ids", "abc").status_code == 400


def test_undecryptable_allowlist_is_explained(client):
    login(client)
    save_credential(client, "telegram_allowed_user_ids", "1001")
    vault.init("rotated-session-secret-" + "z" * 40, main.app.state.config.env_credentials())
    login(client)  # SESSION_SECRET "changed" only for the vault here; the session itself is still valid
    for path in ["/credentials", "/status"]:
        page = client.get(path).text
        assert "nicht entschlüsselbar (SESSION_SECRET geändert?) – niemand darf Buttons drücken" in page, path
        assert "Jedes Mitglied des Chats" not in page, path


def test_allowlist_change_needs_step_up_and_no_bot_restart(client, monkeypatch):
    login(client)
    enable_bot(monkeypatch)

    def reload():
        raise AssertionError("the allowlist is read live, no restart needed")

    monkeypatch.setattr(bot, "schedule_reload", reload)
    assert save_credential(client, "telegram_allowed_user_ids", "1001", password="wrong").status_code == 403
    assert vault.get("telegram_allowed_user_ids") is None
    assert save_credential(client, "telegram_allowed_user_ids", "1001").status_code == 303


# --- session timeouts -----------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    """Controls web._now(); returns a dict whose "t" can be moved forward."""
    state = {"t": 1_800_000_000.0}
    monkeypatch.setattr(web, "_now", lambda: state["t"])
    return state


def set_cookie_headers(response):
    return [v for k, v in response.headers.multi_items() if k.lower() == "set-cookie"]


def test_session_idle_timeout(client, clock):
    login(client)
    clock["t"] += web.SESSION_IDLE_S - 60
    assert client.get("/").status_code == 200  # refreshes seen_at
    clock["t"] += web.SESSION_IDLE_S - 60
    assert client.get("/").status_code == 200  # still within 24 h of the last request
    clock["t"] += web.SESSION_IDLE_S + 1
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_session_absolute_lifetime(client, clock):
    login(client)
    for _ in range(7):  # active every 23 h (161 h in total) ...
        clock["t"] += 23 * 3600
        assert client.get("/").status_code == 200
    clock["t"] += 23 * 3600  # ... but never longer than 7 days (168 h) after login
    assert client.get("/").status_code == 303


def signed_session_cookie(session: dict) -> str:
    """A session cookie exactly as Starlette's SessionMiddleware writes it."""
    import base64

    import itsdangerous

    signer = itsdangerous.TimestampSigner(main.app.state.config.session_secret)
    return signer.sign(base64.b64encode(json.dumps(session).encode())).decode()


def test_seen_at_rewritten_at_most_every_5_minutes(client, clock):
    login(client)
    clock["t"] += 60
    assert set_cookie_headers(client.get("/")) == []  # no cookie rewrite for every request
    clock["t"] += web.SESSION_SEEN_REFRESH_S
    assert len(set_cookie_headers(client.get("/"))) == 1
    assert set_cookie_headers(client.get("/")) == []


def test_session_without_timestamps_is_logged_out(client):
    """Cookies issued before session timeouts existed have no login_at/seen_at: log in again."""
    config = main.app.state.config
    stamp = hmac.new(config.session_secret.encode(), f"0:{config.dashboard_password}".encode(), "sha256").hexdigest()
    client.cookies.set("immobot_session", signed_session_cookie({"auth": True, "stamp": stamp}))
    r = client.get("/")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    now = int(web._now())  # control: the same cookie with timestamps is accepted
    client.cookies.set("immobot_session", signed_session_cookie({"auth": True, "stamp": stamp, "login_at": now, "seen_at": now}))
    assert client.get("/").status_code == 200


def test_session_cookie_max_age_is_7_days():
    middleware = next(m for m in main.app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    assert middleware.kwargs["max_age"] == web.SESSION_MAX_AGE_S == 7 * 24 * 3600


# --- failed-login alerts ----------------------------------------------------------------

def test_failed_logins_alert_at_threshold_and_block(client, notices):
    for _ in range(2):
        client.post("/login", data={"password": "geheim-falsch-1"})
    assert notices == [] and db.list_credential_events() == []
    client.post("/login", data={"password": "geheim-falsch-1"})
    ((text, chat),) = notices
    assert chat == 42
    assert text.startswith("⚠️ 3 fehlgeschlagene Dashboard-Logins von testclient (") and "geheim-falsch" not in text
    assert events() == [("dashboard", "login_failed", "testclient")]
    for _ in range(2):  # 5th failure: blocked - its own kind of warning, so it is sent too
        client.post("/login", data={"password": "geheim-falsch-1"})
    assert len(notices) == 2 and "5 fehlgeschlagene Dashboard-Logins von testclient – für 5 Minuten gesperrt" in notices[1][0]
    assert "geheim-falsch" not in notices[1][0]
    assert events() == [("dashboard", "login_failed", "testclient")] * 2
    assert client.post("/login", data={"password": PASSWORD}).status_code == 429


def test_login_alerts_throttled_per_kind(client, notices, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(web.time, "monotonic", lambda: now["t"])

    def fail(ip, times=1):
        for _ in range(times):
            asyncio.run(web._count_failure(ip))

    fail("203.0.113.1", 3)
    fail("203.0.113.2", 3)  # another IP reaches the threshold within 10 minutes: audit only
    fail("203.0.113.1", 2)  # the first IP gets blocked: a different kind, so it is sent
    assert [t.split(" von ")[1].split(" ")[0] for t, _ in notices] == ["203.0.113.1", "203.0.113.1"]
    assert "gesperrt" in notices[1][0]
    assert [e[2] for e in events()].count("203.0.113.2") == 1
    now["t"] += web.LOGIN_ALERT_INTERVAL_S
    fail("203.0.113.3", 3)  # after the quiet period the threshold warning comes again
    assert len(notices) == 3 and "203.0.113.3" in notices[2][0]


def test_global_failure_budget_alerts_once_without_lockout(client, notices, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(web.time, "monotonic", lambda: now["t"])
    for i in range(web.LOGIN_GLOBAL_ALERT_AT):  # 20 IPs with one or two failures each: no per-IP warning
        now["t"] += 10
        asyncio.run(web._count_failure(f"198.51.100.{i}"))
    (text, chat), = notices
    assert text.startswith("⚠️ Viele fehlgeschlagene Logins von verschiedenen IPs (20 in 10 Minuten") and chat == 42
    assert events() == [("dashboard", "login_failed_many", None)]
    asyncio.run(web._count_failure("198.51.100.99"))  # still over the budget: no second warning within 10 minutes
    assert len(notices) == 1 and len(events()) == 1
    assert client.post("/login", data={"password": PASSWORD}).status_code == 303  # the owner is not locked out


def test_global_budget_needs_20_failures_within_10_minutes(client, notices, monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(web.time, "monotonic", lambda: now["t"])
    for i in range(web.LOGIN_GLOBAL_ALERT_AT * 2):  # one failure every 32 s: 20 failures span 608 s > 600 s
        now["t"] += 32
        asyncio.run(web._count_failure(f"198.51.100.{i}"))
    assert notices == []


@pytest.mark.parametrize("client_ip,key", [
    ("2001:db8:1:2:aaaa::1", "2001:db8:1:2::/64"),
    ("2001:db8:1:2:ffff:ffff:ffff:ffff", "2001:db8:1:2::/64"),
    ("2001:db8:1:3::1", "2001:db8:1:3::/64"),
    ("203.0.113.9", "203.0.113.9"),
    ("::ffff:203.0.113.9", "::ffff:203.0.113.9"),
    ("testclient", "testclient"),
])
def test_limiter_key_groups_ipv6_by_64(client_ip, key):
    assert web._limiter_key(client_ip) == key


def test_ipv6_clients_in_one_64_share_the_limit(client):
    for i in range(web.LOGIN_MAX_FAILURES):  # an attacker rotating addresses inside their /64
        asyncio.run(web._count_failure(f"2001:db8:1:2::{i + 1:x}"))
    assert web._too_many_failures("2001:db8:1:2::abcd")
    assert not web._too_many_failures("2001:db8:1:3::1")


def test_limiter_evicts_stalest_instead_of_skipping(client, monkeypatch):
    monkeypatch.setattr(web, "LOGIN_TRACKED_MAX", 3)
    for ip in ["10.0.0.1", "10.0.0.2", "10.0.0.3"]:
        asyncio.run(web._count_failure(ip))
    asyncio.run(web._count_failure("10.0.0.1"))  # 10.0.0.1 failed again: now the most recent
    asyncio.run(web._count_failure("10.0.0.4"))  # over the cap: the stalest (10.0.0.2) goes, the new one is recorded
    assert list(web._login_failures) == ["10.0.0.3", "10.0.0.1", "10.0.0.4"]
    assert len(web._login_failures["10.0.0.1"]) == 2


# --- logout ---------------------------------------------------------------------------

@pytest.mark.parametrize("headers", [{}, {"Origin": "null"}])
def test_logout_by_stranger_does_not_end_owner_sessions(client, headers):
    login(client)
    owner_cookies = dict(client.cookies)
    client.cookies.clear()
    r = client.post("/logout", headers=headers)  # e.g. curl -X POST, no cookie
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert db.get_meta("session_generation") is None
    client.cookies.update(owner_cookies)
    assert client.get("/").status_code == 200


def test_step_up_failures_trigger_alert(client, notices):
    login(client)
    for _ in range(web.LOGIN_ALERT_AT):
        save_credential(client, "anthropic_api_key", "sk-ant-x", password="falsches-pw")
    assert len(notices) == 1 and "3 fehlgeschlagene Dashboard-Logins" in notices[0][0]
    assert "falsches-pw" not in notices[0][0]
    page = client.get("/credentials").text
    assert "Dashboard-Login" in page and "wiederholt falsches Passwort" in page


# --- proxy headers (Dockerfile) -----------------------------------------------------------

def test_dockerfile_trusts_only_private_proxies():
    from pathlib import Path

    from uvicorn.middleware.proxy_headers import _TrustedHosts

    dockerfile = (Path(__file__).parent.parent / "Dockerfile").read_text()
    cmd = json.loads(dockerfile.split("\nCMD ", 1)[1].strip())
    allowed = cmd[cmd.index("--forwarded-allow-ips") + 1]
    assert allowed == "127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    trusted = _TrustedHosts(allowed)
    # The proxy appends the real client; a client-supplied X-Forwarded-For entry can't override it.
    assert trusted.get_trusted_client_address("6.6.6.6, 203.0.113.9")[0] == "203.0.113.9"
    assert "172.18.0.2" in trusted and "203.0.113.9" not in trusted


# --- AI provider (Einstellungen → KI-Anbieter) ------------------------------------------

DGPT_KEY = "dgpt_qz7Xv9Mk3Jw8Lp0r4Tn6Yb2WXYZ"  # no common words: every 6-character piece is distinctive


@pytest.fixture
def model_refreshes(monkeypatch):
    """Record llm.refresh_models() calls instead of calling a provider."""
    calls = []

    async def refresh(provider):
        calls.append(provider)
        return []

    monkeypatch.setattr(llm, "refresh_models", refresh)
    return calls


def cache_models(provider, models, error=None):
    db.set_meta(f"models_cache:{provider}", json.dumps({"fetched_at": db.now_iso(), "models": models, "error": error}))


def model_options(html, provider="anthropic"):
    select = re.search(rf'<select name="model_{provider}"[^>]*>(.*?)</select>', html, re.S).group(1)
    return re.findall(r'<option value="([^"]+)"', select)


def provider_panel(html, provider):
    """The HTML of one provider's panel on /settings."""
    return re.search(rf'<section class="provider-panel[^"]*" data-provider-panel="{provider}".*?</section>', html, re.S).group(0)


def save_provider(client, provider, model=None):
    """Choose a provider (and optionally a model in its panel) and save, like the settings form does."""
    form = valid_settings(provider=provider)
    if model:
        form[f"model_{provider}"] = model
    return client.post("/settings", data=form)


def test_settings_shows_provider_section(client):
    login(client)
    r = client.get("/settings")
    page = r.text
    assert 'id="ki"' in page and "KI-Anbieter" in page
    assert '<select id="provider" name="provider" form="settings-form" data-provider-select data-saved="anthropic">' in page
    assert '<option value="anthropic" selected>Anthropic</option>' in page and ">DeutschlandGPT</option>" in page
    # Both panels are rendered (app.js shows the chosen one; without JS both are visible).
    assert 'class="provider-panel is-active" data-provider-panel="anthropic"' in page
    assert 'class="provider-panel" data-provider-panel="deutschlandgpt"' in page
    anthropic, dgpt = provider_panel(page, "anthropic"), provider_panel(page, "deutschlandgpt")
    assert "Key: <code>sk-ant-…1234</code> · aus Env-Variable" in anthropic  # conftest's env key, as a hint only
    assert "So bekommst du den Anthropic-API-Key" in anthropic and "So bekommst du den DeutschlandGPT-API-Key" in dgpt
    assert "Noch kein DeutschlandGPT-API-Key hinterlegt" in dgpt and "Noch kein" not in anthropic  # warning in its panel
    for panel, provider in [(anthropic, "anthropic"), (dgpt, "deutschlandgpt")]:
        assert f'<input type="hidden" name="provider" value="{provider}">' in panel  # key form for that provider
        assert "Key speichern &amp; Modelle laden" in panel and 'autocomplete="current-password" required' in panel
        assert 'class="masked" autocomplete="off"' in panel and f'form="refresh-models-{provider}"' in panel
    assert "Preise in $ pro 1 Mio. Tokens" in anthropic and "Preise in € pro 1 Mio. Tokens" in dgpt
    assert "Modellliste nicht geprüft" in anthropic
    assert model_options(page, "anthropic") == list(settings.PRICES["anthropic"])  # unchecked: every priced model
    assert model_options(page, "deutschlandgpt") == list(settings.PRICES["deutschlandgpt"])
    assert "Anbieter &amp; Modell speichern" in page and ">Wechseln<" not in page
    assert "data-provider-unsaved hidden" in page
    assert "Wechselkurs: 1 € = x $" in page and 'name="eur_usd_rate" value="1.10"' in page
    assert "Monatsbudget in €" in page and 'name="monthly_budget_eur" value="15"' in page


def test_switch_provider_with_the_settings_form(client, model_refreshes):
    login(client)
    client.post("/settings", data=valid_settings(model="claude-haiku-5-5"))
    r = save_provider(client, "deutschlandgpt")  # no model chosen in its panel: its default model
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    stored = db.get_settings()
    assert stored["provider"] == "deutschlandgpt" and stored["model"] == "claude-sonnet-5.5"
    assert model_refreshes == []  # no DeutschlandGPT key yet: nothing to fetch
    page = client.get("/settings").text
    assert 'data-saved="deutschlandgpt"' in page and 'class="provider-panel is-active" data-provider-panel="deutschlandgpt"' in page
    assert "Noch kein DeutschlandGPT-API-Key hinterlegt" in provider_panel(page, "deutschlandgpt")
    assert "3,30 € / 16,50 €" in page
    assert save_provider(client, "deutschlandgpt", "claude-opus-5.5").status_code == 303
    assert db.get_settings()["model"] == "claude-opus-5.5"
    assert save_provider(client, "anthropic", "claude-sonnet-5-5").status_code == 303  # its own panel's select
    assert (db.get_settings()["provider"], db.get_settings()["model"]) == ("anthropic", "claude-sonnet-5-5")
    assert save_provider(client, "openai").status_code == 400
    assert client.post("/settings/provider", data={"provider": "deutschlandgpt"}).status_code in (404, 405)  # no 2nd step


def test_switch_provider_fetches_models_once_when_a_key_exists(client, model_refreshes):
    login(client)
    vault.set("deutschlandgpt_api_key", DGPT_KEY)
    save_provider(client, "deutschlandgpt")
    assert model_refreshes == ["deutschlandgpt"]
    cache_models("deutschlandgpt", ["claude-sonnet-5.5"])
    cache_models("anthropic", ["claude-opus-5-5"])
    save_provider(client, "anthropic")
    save_provider(client, "deutschlandgpt")
    client.post("/settings", data=valid_settings(provider="deutschlandgpt"))  # same provider again: nothing
    assert model_refreshes == ["deutschlandgpt"]  # cached: "Modelle neu laden" refreshes on demand


def test_settings_form_validates_model_per_provider(client):
    login(client)
    r = client.post("/settings", data=valid_settings(model="claude-sonnet-5.5"))  # a DeutschlandGPT id on Anthropic
    assert r.status_code == 400 and "Unbekanntes Modell" in r.text
    r = client.post("/settings", data=valid_settings(provider="deutschlandgpt", model="claude-haiku-5-5"))
    assert r.status_code == 303  # provider changed in the same form: its default model
    assert db.get_settings()["model"] == "claude-sonnet-5.5"
    assert client.post("/settings", data=valid_settings(provider="deutschlandgpt", model="claude-opus-5.5")).status_code == 303
    assert db.get_settings()["model"] == "claude-opus-5.5"
    assert client.post("/settings", data=valid_settings(provider="bogus")).status_code == 400


@pytest.mark.parametrize("rate,ok", [("1,10", True), ("0.95", True), ("0", False), ("abc", False), ("11", False)])
def test_eur_usd_rate_validation(client, rate, ok):
    login(client)
    r = client.post("/settings", data=valid_settings(eur_usd_rate=rate))
    assert (r.status_code == 303) is ok
    if ok:
        assert float(db.get_settings()["eur_usd_rate"]) == float(rate.replace(",", "."))


def test_model_select_is_live_list_intersected_with_prices(client):
    login(client)
    save_provider(client, "deutschlandgpt")
    cache_models("deutschlandgpt", ["claude-sonnet-5.5", "claude-haiku-5.5", "gpt-9-turbo", "claude-opus-4.5", "mistral-x"])
    page = client.get("/settings").text
    dgpt = provider_panel(page, "deutschlandgpt")
    assert model_options(page, "deutschlandgpt") == ["claude-sonnet-5.5", "claude-haiku-5.5"]
    assert "Modellliste von DeutschlandGPT geladen am" in dgpt and "nicht geprüft" not in dgpt
    assert "3 weitere Modelle verfügbar, aber nicht unterstützt (kein Preis hinterlegt)" in dgpt
    assert "gpt-9-turbo, claude-opus-4.5, mistral-x" in dgpt
    db.update_settings({"model": "claude-opus-4.8"})  # saved choice not in the live list: still selectable
    assert model_options(client.get("/settings").text, "deutschlandgpt")[-1] == "claude-opus-4.8"


def test_failed_model_list_shows_reason_and_all_priced_models(client):
    login(client)
    cache_models("anthropic", [], error="Anthropic-API-Key ungültig oder ohne Berechtigung.")
    page = client.get("/settings").text
    assert "Modellliste nicht geprüft: Anthropic-API-Key ungültig oder ohne Berechtigung." in provider_panel(page, "anthropic")
    assert model_options(page, "anthropic") == list(settings.PRICES["anthropic"])


def test_save_provider_key_needs_step_up_and_loads_models(client, notices, model_refreshes):
    login(client)  # Anthropic stays the saved provider: a panel's key form works for its own provider
    form = {"provider": "deutschlandgpt", "value": DGPT_KEY}
    r = client.post("/settings/provider-key", data={**form, "password": "wrong"})
    assert r.status_code == 403 and "Dashboard-Passwort falsch" in r.text and 'id="ki"' in r.text
    assert '<option value="deutschlandgpt" selected>' in r.text  # the error page shows that provider's panel
    assert vault.get("deutschlandgpt_api_key") is None and model_refreshes == []
    r = client.post("/settings/provider-key", data={**form, "value": "too short", "password": PASSWORD})
    assert r.status_code == 400 and "DeutschlandGPT-API-Key:" in r.text
    assert client.post("/settings/provider-key", data={**form, "provider": "openai", "password": PASSWORD}).status_code == 400
    r = client.post("/settings/provider-key", data={**form, "value": f"  {DGPT_KEY} ", "password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=key&show=deutschlandgpt#ki"
    assert vault.get("deutschlandgpt_api_key") == DGPT_KEY and model_refreshes == ["deutschlandgpt"]
    assert db.get_settings()["provider"] == "anthropic"  # saving a key doesn't switch the provider
    assert events() == [("deutschlandgpt_api_key", "set", "testclient")]  # same audit + notice as on /credentials
    assert "DeutschlandGPT-API-Key – gespeichert" in notices[-1][0] and DGPT_KEY not in notices[-1][0]
    page = client.get("/settings?saved=key&show=deutschlandgpt").text
    assert "API-Key gespeichert." in page and '<option value="deutschlandgpt" selected>' in page
    assert 'class="provider-panel is-active" data-provider-panel="deutschlandgpt"' in page
    assert "Noch nicht gespeichert" in page and "data-provider-unsaved hidden" not in page  # select ≠ saved provider
    dgpt = provider_panel(page, "deutschlandgpt")
    assert "Key: <code>dgpt…WXYZ</code> · gesetzt am" in dgpt and "Noch kein DeutschlandGPT-API-Key" not in dgpt
    assert '<option value="bogus"' not in client.get("/settings?show=bogus").text  # unknown: the saved provider


def test_refresh_models_needs_login_but_no_step_up(client, model_refreshes):
    assert client.post("/settings/models/refresh", data={"provider": "anthropic"}).headers["location"] == "/login"
    login(client)
    r = client.post("/settings/models/refresh", data={"provider": "anthropic"})
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=models&show=anthropic#ki"
    r = client.post("/settings/models/refresh", data={"provider": "deutschlandgpt"})
    assert r.headers["location"] == "/settings?saved=models&show=deutschlandgpt#ki"
    assert model_refreshes == ["anthropic", "deutschlandgpt"]
    assert client.post("/settings/models/refresh", data={"provider": "x"}).status_code == 400
    assert client.post("/settings/models/refresh", data={"provider": "anthropic"},
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_no_key_warning_on_settings_and_status(client):
    login(client)
    vault.init(TEST_SECRETS["SESSION_SECRET"], {})
    assert "Kein Anthropic-API-Key hinterlegt – Entwürfe schlagen fehl." in client.get("/status").text
    assert "Noch kein Anthropic-API-Key hinterlegt" in provider_panel(client.get("/settings").text, "anthropic")
    vault.set("anthropic_api_key", DASHBOARD_SECRETS["anthropic_api_key"])
    assert "Kein Anthropic-API-Key hinterlegt" not in client.get("/status").text
    assert "Noch kein Anthropic-API-Key" not in client.get("/settings").text
    db.update_settings({"provider": "deutschlandgpt"})
    status = client.get("/status").text
    assert "Kein DeutschlandGPT-API-Key hinterlegt" in status and "DeutschlandGPT · Claude Sonnet 5.5" in status


def test_key_hint_is_the_only_part_of_a_key_that_appears(client):
    login(client)
    vault.set("deutschlandgpt_api_key", DGPT_KEY)
    vault.set("anthropic_api_key", "sk-ant-api03-Hq8Zt5Rw2Nv7Kp4Xm9Jc6-A1B2")
    db.update_settings({"provider": "deutschlandgpt"})
    pages = [client.get(p).text for p in ["/credentials", "/settings", "/status"]]
    for value in [DGPT_KEY, "sk-ant-api03-Hq8Zt5Rw2Nv7Kp4Xm9Jc6-A1B2"]:
        hint = vault.hint("deutschlandgpt_api_key" if value == DGPT_KEY else "anthropic_api_key", value)
        assert all(hint in page for page in pages[:1])  # /credentials lists every key with its hint
        for page in pages:
            for i in range(len(value) - 5):  # no 6-character piece of the value outside the hint
                chunk = value[i:i + 6]
                if chunk not in hint:
                    assert chunk not in page, chunk
    assert "Key: <code>dgpt…WXYZ</code>" in pages[1] and "Key: <code>dgpt…WXYZ</code>" in pages[2]


def test_connection_test_for_deutschlandgpt(client, monkeypatch):
    login(client)
    seen = []

    async def check(provider):
        seen.append(provider)
        raise llm.LLMError("DeutschlandGPT-Guthaben oder Key-Limit aufgebraucht – im DeutschlandGPT-Dashboard aufladen.")

    monkeypatch.setattr(llm, "check_api_key", check)
    r = client.post("/credentials/test/deutschlandgpt")
    assert seen == ["deutschlandgpt"] and "✗ DeutschlandGPT-Guthaben" in r.text
    assert events() == [("deutschlandgpt_api_key", "test_failed", "testclient")]
    assert "DeutschlandGPT testen" in client.get("/credentials").text


def test_listing_shows_euro_costs_and_dollars_for_anthropic(client):
    login(client)
    lid = add_listing()
    db.add_draft(lid, "initial", "Hallo", "claude-sonnet-5.5", cost_usd=0.0231, provider="deutschlandgpt", currency="EUR",
                 cost_native=0.021)
    db.add_draft(lid, "shorter", "Kurz", "claude-opus-5-5", cost_usd=0.042)
    page = client.get(f"/listing/{lid}").text
    # Model · provider and the cost are separate chips under each draft; euros first, dollars only for Anthropic.
    assert ">Claude Sonnet 5.5 · DeutschlandGPT</span>" in page and ">2,1 ct</span>" in page
    assert ">Claude Opus 5.5 · Anthropic</span>" in page and ">3,8 ct (≈ 0,042 $)</span>" in page
    assert "5,9 ct" in client.get("/").text  # listing total in euros: 2,1 + 3,8 ct
    db.update_settings({"eur_usd_rate": "1.40"})  # 1 € = 1,40 $
    assert ">3,0 ct (≈ 0,042 $)</span>" in client.get(f"/listing/{lid}").text


def test_stat_tiles_in_euros(client):
    login(client)
    lid = add_listing()
    db.add_draft(lid, "initial", "A", "claude-opus-5-5", cost_usd=2.2)  # 2,00 € at 1,10
    db.add_draft(lid, "initial", "B", "claude-sonnet-5.5", cost_usd=1.1, provider="deutschlandgpt", currency="EUR",
                 cost_native=1.0)
    for path in ["/", "/status"]:
        page = client.get(path).text
        assert page.count(">3,00 €</span>") == 2, path  # this month and in total
        assert "USD" not in page, path


# --- ImmoScout24 gallery & media route --------------------------------------------------

IS24_URL = "https://www.immobilienscout24.de/expose/123456789"


def store_listing_pictures(listing_id, captions):
    import io

    from PIL import Image

    from app import media

    folder = media.media_root() / str(listing_id)
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for position, caption in enumerate(captions, start=1):
        out = io.BytesIO()
        Image.new("RGB", (30, 20), (200, 100, position)).save(out, format="JPEG")
        (folder / f"{position}.jpg").write_bytes(out.getvalue())
        rows.append({"position": position, "caption": caption, "source_url": "https://pictures.immobilienscout24.de/x",
                     "file": f"{position}.jpg"})
    db.replace_media(listing_id, rows)
    db.set_media_state(listing_id, db.now_iso(), None)


def test_listing_page_shows_gallery_and_details(client):
    login(client)
    lid = add_listing(provider="immoscout", url=IS24_URL, description="Kurzer Fredy-Text")
    page = client.get(f"/listing/{lid}").text
    assert 'Bilder <span class="count">0</span>' in page and "Bilder &amp; Details laden" in page
    assert "Kurzer Fredy-Text" in page
    store_listing_pictures(lid, ["Grundriss", "<b>Balkon</b>"])
    db.set_listing_details(lid, "Zimmer: 3\n## Objektbeschreibung\nVoller Text", "Frau Erika Mustermann")
    page = client.get(f"/listing/{lid}").text
    assert 'Bilder <span class="count">2</span>' in page and "Bilder &amp; Details laden" not in page
    assert f'<img src="/listing/{lid}/media/1"' in page and f'href="/listing/{lid}/media/2"' in page
    assert "1 · Grundriss" in page and "&lt;b&gt;Balkon&lt;/b&gt;" in page and "<b>Balkon</b>" not in page
    # The big picture at the top skips the floor plan (IS24 sorts floor plans first).
    assert re.search(rf'class="hero" href="/listing/{lid}/media/2"[^>]*>\s*<img src="/listing/{lid}/media/2"', page)
    # Details as a card: contact person and key facts as <dl class="facts">, then the titled sections.
    assert "<dt>Ansprechperson</dt><dd>Frau Erika Mustermann</dd>" in page and "<dt>Zimmer</dt><dd>3</dd>" in page
    assert "<h3>Objektbeschreibung</h3>" in page and "Voller Text" in page
    assert "Kurzer Fredy-Text" not in page  # the full exposé text replaces Fredy's
    db.set_media_state(lid, db.now_iso(), "Bilder-Server lehnt ab (HTTP 429) – abgebrochen.")
    page = client.get(f"/listing/{lid}").text
    assert "⚠ Bilder-Server lehnt ab (HTTP 429)" in page and "Erneut versuchen" in page


def test_listing_page_without_is24_has_no_gallery(client):
    login(client)
    lid = add_listing()  # immowelt
    page = client.get(f"/listing/{lid}").text
    assert 'id="bilder"' not in page and "Bilder &amp; Details laden" not in page


def test_media_route(client):
    lid = add_listing(provider="immoscout", url=IS24_URL)
    store_listing_pictures(lid, ["Balkon"])
    r = client.get(f"/listing/{lid}/media/1")
    assert r.status_code == 303 and r.headers["location"] == "/login"  # login required
    login(client)
    r = client.get(f"/listing/{lid}/media/1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert r.headers["cache-control"] == "private, max-age=86400"  # the one exception to no-store
    assert r.headers["x-content-type-options"] == "nosniff" and "frame-ancestors" in r.headers["content-security-policy"]
    assert client.get(f"/listing/{lid}").headers["cache-control"] == "no-store"
    for path in [f"/listing/{lid}/media/2", f"/listing/{lid}/media/0", f"/listing/{lid}/media/-1", "/listing/999/media/1"]:
        assert client.get(path).status_code == 404, path
    for path in [f"/listing/{lid}/media/abc", f"/listing/{lid}/media/..%2F..%2Fimmobot.db", f"/listing/{lid}/media/1.jpg"]:
        assert client.get(path).status_code in (404, 422), path
    db.replace_media(lid, [{"position": 1, "caption": "", "source_url": "x", "file": "../../immobot.db"}])
    assert client.get(f"/listing/{lid}/media/1").status_code == 404  # never outside the listing's folder


def test_load_media_button(client, monkeypatch):
    calls = []

    async def ensure_media(listing_id, retry=False):
        calls.append((listing_id, retry))
        return []

    monkeypatch.setattr(web.media, "ensure_media", ensure_media)
    lid = add_listing(provider="immoscout", url=IS24_URL)
    assert client.post(f"/listing/{lid}/media").headers["location"] == "/login"
    login(client)
    assert client.post(f"/listing/{lid}/media", headers={"Origin": "https://evil.example"}).status_code == 403
    r = client.post(f"/listing/{lid}/media")
    assert r.status_code == 303 and r.headers["location"] == f"/listing/{lid}#bilder"
    assert calls == [(lid, True)]
    assert client.post("/listing/999/media").status_code == 404


def test_public_url_remembered_from_trusted_requests(client):
    client.get("/login", headers={"host": "immobot.example.de"})  # not logged in: ignored
    assert db.get_meta("public_base_url") is None
    login(client)
    client.get("/", headers={"host": "immobot.example.de"})
    assert db.get_meta("public_base_url") == "http://immobot.example.de"  # TestClient speaks http
    assert bot.dashboard_url(3) == "http://immobot.example.de/listing/3"
    client.get("/", headers={"host": "localhost:8000"})  # unusable for Telegram: kept the old one
    assert db.get_meta("public_base_url") == "http://immobot.example.de"
    page = client.get("/settings").text
    assert "<code>http://immobot.example.de</code>" in page and "gemerkt aus deinen Dashboard-Aufrufen" in page
    assert "IMMOBOT_PUBLIC_URL" in page


def test_public_url_from_webhook(client):
    hook(client, {"listings": []}, token="wrong")
    assert db.get_meta("public_base_url") is None
    client.post("/hook/fredy", json={"listings": []}, headers={"Authorization": f"Bearer {TOKEN}", "host": "bot.example.de"})
    assert db.get_meta("public_base_url") == "http://bot.example.de"


def test_settings_llm_max_images(client):
    login(client)
    page = client.get("/settings").text
    assert 'name="llm_max_images" min="0" max="20" step="1" value="10"' in page
    assert "Fotos für Claude pro Entwurf" in page and "Höchstens so viele Bilder" not in page
    assert ("Wie viele Inserat-Fotos Claude beim Schreiben mitbekommt – mehr Fotos = genauer, aber je Foto ca. 1.000 Tokens "
            "(≈ 0,1–4 ct je nach Modell). Das Telegram-Album zeigt unabhängig davon bis zu 10 Fotos (Telegram-Limit).") in page
    assert client.post("/settings", data=valid_settings(llm_max_images="4")).status_code == 303
    assert db.get_settings()["llm_max_images"] == "4"
    for bad in ["21", "-1", "x"]:
        assert client.post("/settings", data=valid_settings(llm_max_images=bad)).status_code == 400


def test_status_shows_last_picture_error(client):
    login(client)
    assert "Letzter Bilder-Fehler: keiner" in client.get("/status").text
    db.set_meta("media_last_error", "2026-10-12T10:00:00+00:00 Inserat 7 (ImmoScout24): Exposé nicht abrufbar (HTTP 404).")
    assert "Inserat 7 (ImmoScout24): Exposé nicht abrufbar (HTTP 404)." in client.get("/status").text
