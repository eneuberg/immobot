import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import bot, db, llm, main, settings, web

from .conftest import TEST_SECRETS

PASSWORD = TEST_SECRETS["DASHBOARD_PASSWORD"]
TOKEN = TEST_SECRETS["FREDY_WEBHOOK_TOKEN"]

# main.py may not register the middleware yet; it has to be added before the app first starts.
if not any(m.kwargs.get("dispatch") is web.security_headers for m in main.app.user_middleware):
    main.app.middleware("http")(web.security_headers)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)  # used by the lifespan -> db.init_db(tmp_path)
    monkeypatch.setattr(main.app.state, "config", cfg)  # used by web.py
    monkeypatch.setattr(web, "LOGIN_FAIL_DELAY_S", 0)
    monkeypatch.setattr(bot, "schedule_announce", lambda ids: None)
    web._login_failures.clear()
    with TestClient(main.app, follow_redirects=False) as c:
        yield c
    web._login_failures.clear()


def login(client):
    r = client.post("/login", data={"password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/"


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


@pytest.mark.parametrize("method,path", [
    ("get", "/"), ("get", "/listing/1"), ("get", "/settings"), ("get", "/status"),
    ("post", "/settings"), ("post", "/settings/test"), ("post", "/settings/reset/profile"),
    ("post", "/listing/1/status"), ("post", "/listing/1/draft"),
])
def test_pages_require_login(client, fake_llm, method, path):
    r = getattr(client, method)(path)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert fake_llm == []


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
    assert "4,2 ct" in r.text and "USD" in r.text
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
    for text in ["Anpassung", "mit Hund", "Codewort", "Claude Sonnet 5.5", "111", "50,0 ct", "Kopieren"]:
        assert text in r.text
    assert client.get("/listing/9999").status_code == 404


def test_javascript_urls_not_rendered(client):
    login(client)
    lid = add_listing(url="javascript:alert(1)", image_url="javascript:alert(2)", fredy_url="JavaScript:alert(3)")
    r = client.get(f"/listing/{lid}")
    assert r.status_code == 200
    assert "javascript:" not in r.text.lower()
    assert "<img" not in r.text and "Inserat öffnen" not in r.text and "In Fredy öffnen" not in r.text


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
        "target_words": "120", "model": "claude-haiku-5-5", "effort": "medium", "monthly_budget_usd": "7,5",
        "send_image": "1", "followup_delay_s": "10", "paused": "1",
    }
    form.update(overrides)
    return form


def test_settings_page_renders_all_fields(client):
    login(client)
    r = client.get("/settings")
    assert r.status_code == 200
    for key in settings.DEFAULT_SETTINGS:
        assert f'name="{key}"' in r.text
    assert "Claude Opus 5.5" in r.text and "4,00 $ / 20,00 $" in r.text


def test_settings_save(client):
    login(client)
    r = client.post("/settings", data=valid_settings())
    assert r.status_code == 303 and r.headers["location"] == "/settings?saved=1"
    stored = db.get_settings()
    assert stored["profile"] == "Erika, 34" and stored["target_words"] == "120" and stored["model"] == "claude-haiku-5-5"
    assert stored["monthly_budget_usd"] == "7.5" and stored["send_image"] == "1" and stored["paused"] == "1"
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
    ("effort", "max"), ("monthly_budget_usd", "-1"), ("monthly_budget_usd", "nan"), ("monthly_budget_usd", "x"),
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
    for text in ["Test-Entwurf", "Neueste Wohnung", "Guten Tag, Entwurf 1", "Codewort Linde", "1,2 ct", "Claude Haiku 5.5"]:
        assert text in r.text


# --- status & secrets -------------------------------------------------------------

def test_status_page(client):
    login(client)
    r = client.get("/status")
    assert r.status_code == 200
    assert "Anthropic API-Key gesetzt" in r.text and "/hook/fredy" in r.text and settings.APP_VERSION in r.text


def test_no_secret_in_any_response(client, fake_llm, monkeypatch):
    secrets = list(TEST_SECRETS.values())
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
    responses += [failed, client.post("/settings/test"), client.post("/logout")]
    responses.append(client.get("/login"))

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
