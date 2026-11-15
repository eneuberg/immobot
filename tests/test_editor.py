"""The draft editor (app/editor.py): the way in from Telegram (/e/…), access to /editor/{id}, login ?next=, every
action, the background rule proposals, "Als gesendet markieren" and the listing page's editor parts."""

import dataclasses
import hashlib
import hmac
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import bot, db, drafting, editor, editor_access, learning, llm, main, media, vault, web
from app.textdiff import word_diff

from .conftest import TEST_SECRETS

PASSWORD = TEST_SECRETS["DASHBOARD_PASSWORD"]
BOT_TOKEN = TEST_SECRETS["TELEGRAM_BOT_TOKEN"]  # the vault's env fallback in these tests
USER = 1001
DAY = 24 * 3600


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)
    monkeypatch.setattr(main.app.state, "config", cfg)
    monkeypatch.setattr(web, "LOGIN_FAIL_DELAY_S", 0)
    monkeypatch.setattr(editor, "_rule_jobs", {})
    monkeypatch.setattr(drafting, "_jobs", {})
    monkeypatch.setattr(drafting, "_editor_jobs", {})
    monkeypatch.setattr(drafting, "_ask_durations", drafting.deque(maxlen=drafting.SAMPLES))
    monkeypatch.setattr(bot, "_locks", {})
    monkeypatch.setattr(learning, "_busy_sessions", set())
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh", refreshed.append)

    async def no_media(listing_id, retry=False, want_images=True):
        return []

    monkeypatch.setattr(media, "ensure_media", no_media)
    web._login_failures.clear()
    with TestClient(main.app, follow_redirects=False) as c:
        vault.set("telegram_allowed_user_ids", f"{USER}")
        c.refreshed = refreshed
        yield c
    web._login_failures.clear()


@pytest.fixture
def lid(client):
    return db.insert_listing({"fredy_id": "e1", "provider": "immoscout", "title": "Helle 2-Zimmer-Wohnung",
                              "price": "780 €", "size": "54 m²", "address": "Musterstr. 12",
                              "url": "https://portal.example/expose/1"})[0]


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def generate(listing, settings, kind, previous_text=None, instruction=None, pictures=None, rules=None,
                       examples=None):
        calls.append({"kind": kind, "previous_text": previous_text, "instruction": instruction})
        return llm.DraftResult(text=f"Fassung von Claude {len(calls)}", hint="Codewort Linde", model="claude-opus-5-5",
                               input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0,
                               cost_usd=0.011)

    monkeypatch.setattr(llm, "generate_draft", generate)
    return calls


def login(client, next_path=None):
    data = {"password": PASSWORD, **({"next": next_path} if next_path is not None else {})}
    return client.post("/login", data=data)


def login_params(user_id=USER, auth_date=None, token=BOT_TOKEN, **extra):
    """Login data as Telegram appends them to a login_url, signed the way Telegram does."""
    auth_date = int(time.time()) - 5 if auth_date is None else auth_date
    fields = {"id": str(user_id), "first_name": "Erika", "username": "erika_tg", "auth_date": str(auth_date), **extra}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(token.encode()).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), "sha256").hexdigest()
    return fields


def from_telegram(client, listing_id, path=None, **params):
    """Tap "📝 Editor": the signed link plus Telegram's login data."""
    return client.get(path or editor_access.make_editor_path(listing_id), params=login_params(**params))


def editor_cookie(client, listing_id):
    r = from_telegram(client, listing_id)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{listing_id}"
    return client.cookies.get(editor_access.EDITOR_COOKIE)


def form_fields(page: str) -> dict:
    fields = {"token": re.search(r'name="token" value="([0-9a-f]{32})"', page).group(1)}
    session = re.search(r'name="session_id" value="(\d+)"', page)
    if session:
        fields["session_id"] = session.group(1)
    return fields


def open_editor(client, listing_id):
    r = client.get(f"/editor/{listing_id}")
    assert r.status_code == 200
    return r, form_fields(r.text)


def post(client, listing_id, action, fields, **data):
    return client.post(f"/editor/{listing_id}/{action}", data={**fields, **data})


def assert_editor_headers(r):
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-robots-tag"] == "noindex"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"  # the dashboard's security headers stay


# --- the way in from Telegram -----------------------------------------------------------------------

def test_telegram_link_starts_a_browser_session(client, lid):
    db.add_draft(lid, "initial", "Guten Tag, Entwurf", "claude-opus-5-5")
    r = from_telegram(client, lid)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}"  # login data leave the address bar
    assert_editor_headers(r)
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(editor_access.EDITOR_COOKIE + "=")
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie and "Path=/" in cookie and "Secure" not in cookie
    max_age = int(re.search(r"Max-Age=(\d+)", cookie).group(1))
    assert DAY - 60 <= max_age <= DAY
    value = client.cookies.get(editor_access.EDITOR_COOKIE)
    assert editor_access.get_editor_browser_session(value)["telegram_user_id"] == USER

    page = client.get(f"/editor/{lid}")
    assert page.status_code == 200 and "Guten Tag, Entwurf" in page.text
    assert_editor_headers(page)
    assert "Entwurfs-Editor" in page.text  # minimal header: no way into the dashboard
    for link in ('href="/settings"', 'href="/status"', 'href="/"', "Abmelden", "🔗 Dashboard", f'href="/listing/{lid}"'):
        assert link not in page.text
    assert f'data-go="{editor_access.make_open_path(lid)}"' in page.text  # "Kopieren & Inserat öffnen", via /go


def test_cookie_is_secure_like_the_session_cookie(client, lid, monkeypatch):
    monkeypatch.setattr(main.app.state, "config", dataclasses.replace(main.app.state.config, cookie_secure=True))
    r = from_telegram(client, lid)
    assert r.status_code == 303 and "Secure" in r.headers["set-cookie"]


def test_link_error_pages(client, lid):
    secret_marks = ["erika_tg", BOT_TOKEN]

    def check(r, status, words, login_link=True):
        assert r.status_code == status
        assert_editor_headers(r)
        for word in words:
            assert word in r.text
        assert ("/login?next=/editor/%d" % lid in r.text) == login_link
        assert editor_access.EDITOR_COOKIE not in r.headers.get("set-cookie", "")
        assert "Einstellungen" not in r.text.split("<main")[0]  # no dashboard navigation
        for mark in secret_marks:
            assert mark not in r.text

    path = editor_access.make_editor_path(lid)
    check(client.get(path[:-3] + "AAA", params=login_params()), 400, ["Link ungültig"])
    check(client.get(editor_access.make_editor_path(lid, now=time.time() - 8 * DAY), params=login_params()), 410,
          ["Link abgelaufen", "7 Tage"])
    check(client.get(path, params={**login_params(), "username": "mallory"}), 400, ["Anmeldung ungültig"])
    check(from_telegram(client, lid, path, auth_date=int(time.time()) - 3600), 410, ["10 Minuten"])
    check(from_telegram(client, lid, path, user_id=2002), 403, ["Kein Zugriff"])
    used_at = int(time.time()) - 7
    params = login_params(auth_date=used_at)
    assert client.get(path, params=params).status_code == 303
    client.cookies.clear()
    check(client.get(path, params=params), 410, ["Link schon benutzt", "kopierte oder weitergeleitete"])
    vault.clear("telegram_allowed_user_ids")
    # another auth_date than above in any case (time.time() may have moved on a second): else "schon benutzt"
    check(from_telegram(client, lid, path, auth_date=used_at - 1), 403,
          ["Erst erlaubte User-IDs eintragen", "Erlaubte Telegram-User-IDs"])
    vault.set("telegram_allowed_user_ids", str(USER))
    old = editor_access.make_editor_path(lid, now=time.time() - DAY - 600)  # first used more than a day ago
    _, _, _, exp, sig = old.split("/")
    first = time.time() - DAY - 300
    assert editor_access.start_editor_browser_session(lid, exp, sig, login_params(auth_date=int(first)), now=first)[0]
    check(from_telegram(client, lid, old), 410, ["24 Stunden"])
    check(client.get(f"/e/abc/{exp}/{sig}", params=login_params()), 400, ["Link ungültig"], login_link=False)
    assert set(editor.LINK_ERRORS) == set(editor_access.ERROR_CODES)


# --- access ------------------------------------------------------------------------------------------

def test_editor_access_matrix(client, lid):
    other = db.insert_listing({"fredy_id": "e2", "title": "Andere"})[0]
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    r = client.get(f"/editor/{lid}")  # nobody
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/editor/{lid}"
    assert_editor_headers(r)
    assert client.get(f"/editor/{lid}/rules/status").status_code == 403

    editor_cookie(client, lid)  # editor session for this listing only
    assert client.get(f"/editor/{lid}").status_code == 200
    assert client.get(f"/editor/{lid}/rules/status").json() == {"state": "idle"}
    r = client.get(f"/editor/{other}")
    assert r.status_code == 303 and r.headers["location"] == f"/login?next=/editor/{other}"
    for path in ["/", f"/listing/{lid}", "/settings", "/status", f"/listing/{lid}/media/1"]:
        assert client.get(path).headers["location"] == "/login"
    assert client.post(f"/listing/{lid}/sent").headers["location"] == "/login"

    with db.connect() as conn:  # the day is over
        conn.execute("UPDATE editor_access_sessions SET expires_at = ?", (db.now_iso().replace("20", "19", 1),))
    r = client.get(f"/editor/{lid}")
    assert r.status_code == 401 and "Sitzung abgelaufen – bitte anmelden" in r.text
    assert f'href="/login?next=/editor/{lid}"' in r.text
    fields = {"token": "x" * 32, "session_id": "1"}
    assert post(client, lid, "save", fields, text="x").status_code == 401

    client.cookies.clear()
    assert login(client).status_code == 303  # dashboard login: every listing
    for listing_id in (lid, other):
        r = client.get(f"/editor/{listing_id}")
        assert r.status_code == 200 and f'href="/listing/{listing_id}"' in r.text and 'href="/settings"' in r.text
        assert_editor_headers(r)
    assert "Noch kein Anschreiben" in client.get(f"/editor/{other}").text
    for path in ["/editor/abc", "/editor/0", "/editor/99999"]:
        r = client.get(path)
        assert r.status_code == 404 and "Inserat nicht gefunden" in r.text and "abc" not in r.text


def test_login_next(client, lid):
    r = client.get("/login", params={"next": f"/editor/{lid}"})
    assert f'name="next" value="/editor/{lid}"' in r.text
    r = login(client, f"/editor/{lid}")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}"
    assert client.get("/login", params={"next": f"/editor/{lid}"}).headers["location"] == f"/editor/{lid}"
    assert client.get("/login", params={"next": "//evil.example.com"}).headers["location"] == "/"
    client.post("/logout")
    r = client.post("/login", data={"password": "falsch", "next": f"/editor/{lid}"})
    assert r.status_code == 401 and f'name="next" value="/editor/{lid}"' in r.text  # kept for the next try


@pytest.mark.parametrize("value", ["//evil.example.com", "https://evil.example.com/x", "/\\evil.example.com",
                                   "\\\\evil.example.com", "javascript:alert(1)", "editor/5", "/editor/5\n",
                                   "/ editor", "/" + "a" * 400, "", None])
def test_login_rejects_foreign_next(client, value):
    assert web.safe_next(value) is None
    data = {"password": PASSWORD, **({"next": value} if value is not None else {})}
    r = client.post("/login", data=data)
    assert r.status_code == 303 and r.headers["location"] == "/"
    r = client.get("/login", params={"next": value} if value is not None else None)
    assert 'name="next"' not in r.text and "evil" not in r.text


def test_safe_next_accepts_local_paths():
    for path in ["/", "/editor/12", "/editor/12?m=saved", "/listing/3#entwuerfe"]:
        assert web.safe_next(path) == path


# --- actions --------------------------------------------------------------------------------------------

def test_save_needs_token_and_origin(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "save", {**fields, "token": "0" * 32}, text="Fremd")
    assert r.status_code == 403 and "Seite veraltet" in r.text
    r = client.post(f"/editor/{lid}/save", data={**fields, "text": "Fremd"}, headers={"Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert db.latest_session(lid)["current_text"] == "Original"
    r = post(client, lid, "save", fields, text="Hand\r\nbearbeitet")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=saved#text"
    assert_editor_headers(r)
    assert db.latest_session(lid)["current_text"] == "Hand\nbearbeitet"
    page = client.get(r.headers["location"])
    assert "Gespeichert." in page.text and "Hand\nbearbeitet</textarea>" in page.text
    assert "<ins>" in page.text  # the diff to Claude's original
    client.cookies.clear()
    login(client)  # the token belongs to the way in: a dashboard login gets its own
    assert post(client, lid, "save", fields, text="x").status_code == 403
    _, dashboard_fields = open_editor(client, lid)
    assert dashboard_fields["token"] != fields["token"]
    assert post(client, lid, "save", dashboard_fields, text="Vom Dashboard").status_code == 303


def test_revise_with_critique_and_history(client, lid, fake_generate):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert "Kopieren &amp; Inserat öffnen" in r.text and "Fassung 1" not in r.text  # one action; no badge yet
    r = post(client, lid, "revise", fields, text="Original von Hand", critique="  ")
    assert r.status_code == 400 and "was Claude ändern soll" in r.text
    assert db.latest_session(lid)["current_text"] == "Original von Hand"  # hand edits saved anyway
    r = post(client, lid, "revise", fields, text="Original von Hand", critique="Kürzer bitte")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=revised#text"
    assert fake_generate == [{"kind": "custom", "previous_text": "Original von Hand", "instruction": "Kürzer bitte"}]
    page = client.get(f"/editor/{lid}").text
    assert "Fassung von Claude 1</textarea>" in page and "Fassung 2" in page
    assert "<q>Kürzer bitte</q>" in page and "erster Entwurf" in page  # Verlauf
    assert "Codewort Linde" in page  # Claude's hint for this text
    assert "Es gibt einen neueren Entwurf" not in page  # its own revision isn't a newer draft
    assert "Claude-Aufrufen" not in page  # the daily count only shows when the limit comes near
    editor.CALLS_SHOWN_FROM, shown_from = 1, editor.CALLS_SHOWN_FROM
    try:
        assert "Heute schon 1 von 30 Claude-Aufrufen" in client.get(f"/editor/{lid}").text
    finally:
        editor.CALLS_SHOWN_FROM = shown_from

    db.update_settings({"monthly_budget_eur": "0.001"})
    r = post(client, lid, "revise", fields, text="Fassung von Claude 1", critique="Förmlicher")
    assert r.status_code == 400 and "Monatsbudget" in r.text and "Förmlicher</textarea>" in r.text
    assert len(fake_generate) == 1


def test_quick_buttons_shorter_and_formal(client, lid, fake_generate):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert "/quick" not in r.text  # the editor has no quick buttons any more; the route stays (Telegram's kinds)

    r = post(client, lid, "quick", fields, text="Original von Hand", critique="", kind="shorter")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=shorter#text"
    assert fake_generate == [{"kind": "shorter", "previous_text": "Original von Hand", "instruction": None}]
    page = client.get(r.headers["location"]).text
    assert "Claude hat den Text gekürzt." in page and "Fassung von Claude 1</textarea>" in page and "Fassung 2" in page
    assert "überarbeitet: Kürzer" in page  # in the Verlauf
    (feedback,) = db.list_feedback()
    assert (feedback["source"], feedback["kind"], feedback["text"]) == ("editor", "quick", "shorter")

    # a critique typed but not sent stays in its field (by a one-time id, never in the URL)
    r = post(client, lid, "quick", fields, text="Fassung von Claude 1", critique="Balkon erwähnen", kind="formal")
    location = r.headers["location"]
    assert r.status_code == 303 and location.startswith(f"/editor/{lid}?m=formal&k=") and "Balkon" not in location
    page = client.get(location).text
    assert "förmlicher formuliert" in page and "Balkon erwähnen</textarea>" in page
    assert "Balkon erwähnen</textarea>" not in client.get(location).text  # shown once
    assert fake_generate[-1]["kind"] == "formal" and len(fake_generate) == 2

    r = post(client, lid, "quick", fields, text="x", kind="custom")
    assert r.status_code == 400 and "Unbekannte Aktion" in r.text and len(fake_generate) == 2
    db.update_settings({"monthly_budget_eur": "0.001"})
    r = post(client, lid, "quick", fields, text="Fassung von Claude 2", critique="Noch da", kind="shorter")
    assert r.status_code == 400 and "Monatsbudget" in r.text and "Noch da</textarea>" in r.text
    assert client.refreshed == []  # like "Überarbeiten lassen": Telegram isn't touched


def test_quick_button_regenerate_starts_a_new_round(client, lid, fake_generate):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "quick", fields, text="Original, von Hand", kind="regenerate")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=regenerate#text"
    assert fake_generate == [{"kind": "regenerate", "previous_text": "Original, von Hand", "instruction": None}]
    old, new = db.get_session(int(fields["session_id"])), db.latest_session(lid)
    assert old["status"] == "superseded" and new["status"] == "open" and new["original_text"] == "Fassung von Claude 1"
    page = client.get(r.headers["location"]).text
    assert "ganz neu geschrieben" in page and "Fassung von Claude 1</textarea>" in page and "Fassung 1" in page
    assert "Es gibt einen neueren Entwurf" not in page and "Keine Änderungen seit der letzten Fassung" in page
    assert post(client, lid, "quick", fields, text="Alter Tab", kind="shorter").status_code == 409  # old round's form
    assert len(fake_generate) == 1


def test_daily_limit_shows_up_nicely(client, lid, fake_generate, monkeypatch):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    monkeypatch.setattr(learning, "EDITOR_DAILY_LIMIT", 0)
    login(client)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "revise", fields, text="Original", critique="Kürzer")
    assert r.status_code == 400 and "Tageslimit erreicht" in r.text and fake_generate == []


def wait_for_rules(client, listing_id):
    for _ in range(200):
        state = client.get(f"/editor/{listing_id}/rules/status").json()["state"]
        if state != "running":
            return state
        time.sleep(0.01)
    raise AssertionError("rule proposals still running")


@pytest.fixture
def fake_rules(monkeypatch):
    """learning.propose_rules replaced: proposes one new rule and one update of an active rule."""
    calls = []

    async def propose(session_id):
        calls.append(session_id)
        active = db.add_rule("Alte Regel", status="active")
        db.add_rule("Nenne kein Einzugsdatum.", reason="Datum zweimal gestrichen.", session_id=session_id)
        db.add_rule("Alte Regel, genauer", action="update", replaces_rule_id=active, session_id=session_id)
        return db.list_rules("proposed", session_id=session_id)

    monkeypatch.setattr(learning, "propose_rules", propose)
    return calls


def test_finish_proposes_rules_in_the_background(client, lid, fake_rules):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "finish", fields, text=" Endversion von Hand \n")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=done#ende"
    session = db.latest_session(lid)
    assert (session["status"], session["final_text"]) == ("done", "Endversion von Hand")
    assert wait_for_rules(client, lid) == "done" and fake_rules == [session["id"]]
    page = client.get(f"/editor/{lid}").text
    assert "Endversion von Hand</pre>" in page and "Nenne kein Einzugsdatum." not in page  # proposals: own page
    assert f'href="/editor/{lid}/lernen"' in page and "Claude hat 2 Vorschläge aus deinen Änderungen" in page
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Nenne kein Einzugsdatum.</textarea>" in page
    assert "Datum zweimal gestrichen." in page and "Bisher:" in page and "Alte Regel" in page
    assert "Claude sucht nach Regeln" not in page and "Hast du die Nachricht abgeschickt?" not in page

    new_rule, update = db.list_rules("proposed", session_id=session["id"])
    r = post(client, lid, f"rules/{new_rule['id']}", fields, decision="accept", text="Nenne nie ein Einzugsdatum.")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}/lernen?m=accepted#regeln"
    rule = db.get_rule(new_rule["id"])
    assert (rule["status"], rule["text"], rule["proposed_text"]) == ("active", "Nenne nie ein Einzugsdatum.",
                                                                     "Nenne kein Einzugsdatum.")
    assert post(client, lid, f"rules/{update['id']}", fields, decision="reject").status_code == 303
    assert db.get_rule(update["id"])["status"] == "rejected"
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Nenne nie ein Einzugsdatum." in page and "verworfen" in page  # decided ones listed
    r = post(client, lid, f"rules/{update['id']}", fields, decision="accept")
    assert r.status_code == 400 and "schon entschieden" in r.text


def test_unsent_critique_on_fertig_is_kept(client, lid, fake_rules):
    """Critique typed, "Überarbeiten lassen" not pressed, then "Fertig": not dropped, but kept as a note for
    learning, and the page says so."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "finish", fields, text="Endversion", critique=" Den Balkon nicht erwähnen ")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=done_note#ende"
    session = db.latest_session(lid)
    (note,) = db.list_feedback(session_id=session["id"])
    assert (note["kind"], note["text"]) == ("note", "Den Balkon nicht erwähnen")
    assert wait_for_rules(client, lid) == "done"
    page = client.get(r.headers["location"]).text
    assert "hat Claude nicht mehr umgesetzt" in page and "als Rückmeldung gespeichert" in page
    assert "„Den Balkon nicht erwähnen“" in page and "nicht an Claude geschickt" in page  # in the Verlauf, as a note
    assert "Den Balkon nicht erwähnen" in page.split('id="verlauf"')[1].split('class="vl-item is-note"')[1]


def test_finished_session_without_proposals_offers_to_ask(client, lid, fake_rules):
    """After a restart the background job is gone: a finished session with nothing proposed for its final version
    offers "Regeln vorschlagen lassen" instead of claiming there are none."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    session_id = int(fields["session_id"])
    learning.finish(session_id, "Geändert")  # finished, but no rule round (like a job lost in a restart)
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Für diesen Text hat Claude noch keine Regeln vorgeschlagen." in page
    assert f'action="/editor/{lid}/rules/retry"' in page and "Regeln vorschlagen lassen" in page
    r = post(client, lid, "rules/retry", fields)
    assert r.status_code == 303 and wait_for_rules(client, lid) == "done" and fake_rules == [session_id]
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Nenne kein Einzugsdatum.</textarea>" in page and "Regeln vorschlagen lassen" not in page


@pytest.mark.parametrize("final,basis,shown", [
    ("Original", None, "Du hast Claudes Entwurf unverändert übernommen"),
    ("Geändert", "same", "Claude hat in dieser Runde keine allgemeine Regel gefunden."),
])
def test_finished_session_says_why_there_are_no_proposals(client, lid, final, basis, shown):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    session = learning.finish(int(fields["session_id"]), final)
    if basis:  # a round was made and found nothing
        db.update_session(session["id"], rules_basis=learning._rules_basis(session, []))
    page = client.get(f"/editor/{lid}/lernen").text
    assert shown in page and "Regeln vorschlagen lassen" not in page


def test_rules_of_other_sessions_are_out_of_reach(client, lid):
    other = db.insert_listing({"fredy_id": "e2", "title": "Andere"})[0]
    db.add_draft(other, "initial", "Anderer Entwurf", "claude-opus-5-5")
    foreign_session = learning.open_or_create_session(other)
    foreign = db.add_rule("Fremde Regel", session_id=foreign_session["id"])
    loose = db.add_rule("Regel ohne Sitzung")
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    learning.finish(int(fields["session_id"]), "Ende")
    for rule_id in (foreign, loose, 99999, "abc"):
        r = post(client, lid, f"rules/{rule_id}", fields, decision="accept")
        assert r.status_code == 404 and "gibt es hier nicht" in r.text
    assert db.get_rule(foreign)["status"] == db.get_rule(loose)["status"] == "proposed"
    r = post(client, other, f"rules/{foreign}", fields, decision="accept")  # the other listing: no access at all
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert db.get_rule(foreign)["status"] == "proposed"


def test_rule_job_error_and_retry(client, lid, monkeypatch):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    calls = []

    async def failing(session_id):
        calls.append(session_id)
        raise llm.BudgetExceeded("Monatsbudget von 15 € erreicht.")

    monkeypatch.setattr(learning, "propose_rules", failing)
    login(client)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Ende")
    assert wait_for_rules(client, lid) == "error"
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Keine Regelvorschläge: Monatsbudget von 15 € erreicht." in page and "Nochmal versuchen" in page

    async def boom(session_id):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(learning, "propose_rules", boom)
    r = post(client, lid, "rules/retry", fields)
    assert r.status_code == 303 and wait_for_rules(client, lid) == "error"
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Unerwarteter Fehler" in page and "secret detail" not in page


def test_waiting_page_while_rules_run(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    session_id = int(fields["session_id"])
    learning.finish(session_id, "Ende")
    editor._rule_jobs[session_id] = {"state": "running", "error": None}
    page = client.get(f"/editor/{lid}").text
    assert "Claude lernt aus deinen Änderungen – ansehen" in page and "data-rules-poll" not in page  # just a link
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Claude sucht nach Regeln …" in page and f'data-rules-poll="/editor/{lid}/rules/status"' in page
    assert f'<noscript><meta http-equiv="refresh" content="5; url=/editor/{lid}/lernen"></noscript>' in page  # no "#…"
    assert client.get(f"/editor/{lid}/rules/status").json() == {"state": "running"}
    r = post(client, lid, "reopen", fields)
    assert r.status_code == 409 and db.get_session(session_id)["status"] == "done"


def test_finish_without_rules_and_reopen(client, lid, fake_rules):
    db.update_settings({"use_rules": "0"})
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    assert post(client, lid, "finish", fields, text="   ").status_code == 400  # empty: stays open
    post(client, lid, "finish", fields, text="Ende")
    assert fake_rules == [] and editor._rule_jobs == {}
    page = client.get(f"/editor/{lid}").text
    assert "Regeln für künftige Entwürfe" not in page and "Weiter bearbeiten" in page
    r = post(client, lid, "reopen", fields)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=reopened#text"
    session = db.latest_session(lid)
    assert session["status"] == "open" and session["final_text"] == "Ende"
    assert "Ende</textarea>" in client.get(f"/editor/{lid}").text


def test_mark_sent(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Ende")
    page = client.get(f"/editor/{lid}").text
    assert "<strong>Abgeschickt?</strong>" in page and "✅ Ja, abgeschickt" in page
    assert f'<aside class="ask-sheet" id="editor-{lid}-ask" data-ask-return="editor-{lid}" data-opened\n' in page  # the sheet, shown
    sheet = page.split('class="ask-sheet"')[1].split("</aside>")[0]
    assert 'class="secondary outline js-only" data-sheet-close>Noch nicht</button>' in sheet
    assert "<dialog" not in page.replace('<dialog class="ed-sheet"', "") and "data-ask-fold" in sheet
    assert "ask-later" not in sheet and "uninteressant" not in sheet  # two buttons, only what she'd not do anyway
    assert page.count(f'action="/editor/{lid}/sent"') == 1  # asked once
    r = post(client, lid, "sent", {"token": fields["token"]})
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=sent#ende"
    assert db.get_listing(lid)["status"] == "sent" and client.refreshed == [lid, lid]  # Telegram: Endversion, then "gesendet"
    page = client.get(f"/editor/{lid}").text
    assert "✓ Gesendet" in page and "<strong>Abgeschickt?</strong>" not in page
    # pressed by mistake: "zurücknehmen" -> back to "Entwurf"
    assert f'action="/editor/{lid}/unsent"' in page
    r = post(client, lid, "unsent", {"token": fields["token"]})
    assert r.headers["location"] == f"/editor/{lid}?m=unsent#ende" and db.get_listing(lid)["status"] == "drafted"
    assert "<strong>Abgeschickt?</strong>" in client.get(f"/editor/{lid}").text


def test_mark_dismissed_after_finish(client, lid):
    """Not sent after all: "Doch uninteressant" in the card sorts the listing out (by hand; the sheet only asks
    "Abgeschickt?"); the banner then offers "↩️ Zurückholen"."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Ende")
    page = client.get(f"/editor/{lid}").text
    assert page.count(f'action="/editor/{lid}/dismiss"') == 1 and "❌ Doch uninteressant" in page
    assert "Doch uninteressant" not in page.split('class="ask-sheet"')[1]
    r = post(client, lid, "dismiss", {"token": fields["token"]})
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=dismissed#ende"
    listing = db.get_listing(lid)
    assert listing["discard_reason"] == "uninteressant" and listing["discard_auto"] == 0
    assert client.refreshed[-1] == lid
    page = client.get(f"/editor/{lid}").text
    assert "<strong>Abgeschickt?</strong>" not in page and "Von dir als uninteressant aussortiert" in page
    assert '<span class="dc-kind">Aussortiert</span><span class="dc-reason">Uninteressant</span>' in page
    assert f'action="/editor/{lid}/restore"' in page
    assert post(client, lid, "sent", {"token": fields["token"]}).headers["location"] == f"/editor/{lid}?m=locked"
    assert db.get_listing(lid)["status"] != "sent"  # locked while aussortiert
    r = post(client, lid, "restore", {"token": fields["token"]})
    assert r.headers["location"] == f"/editor/{lid}?m=restored" and db.get_listing(lid)["discard_reason"] is None
    assert "<strong>Abgeschickt?</strong>" in client.get(f"/editor/{lid}").text


def test_editor_banner_for_offline_listings(client, lid):
    """Offline (or veraltet): a notice at the top, no Zurückholen, no "abgeschickt?" - the editor stays usable."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    listing = db.get_listing(lid)
    db.mark_offline([listing["fredy_id"]])
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    page = r.text
    assert "Inserat ist nicht mehr online – von Fredy gemeldet am" in page and "Du kannst hier trotzdem weiterschreiben" in page
    assert f'action="/editor/{lid}/restore"' not in page and "data-ask-return" not in page
    post(client, lid, "finish", fields, text="Ende")
    page = client.get(f"/editor/{lid}").text
    assert "Ende" in page and "Hast du die Nachricht abgeschickt?" not in page
    post(client, lid, "restore", {"token": fields["token"]})
    assert db.get_listing(lid)["discard_reason"] == "offline"  # final


def test_editor_counts_as_seen(client, lid):
    """Opening the editor (from Telegram or the dashboard) and every action in it mark the listing seen; a refused
    request doesn't."""
    assert client.get(f"/editor/{lid}").status_code == 303  # no access: login
    editor_cookie(client, lid)  # the Telegram link only redirects to the editor
    assert db.get_listing(lid)["seen_at"] is None
    _, fields = open_editor(client, lid)
    assert db.get_listing(lid)["seen_at"]
    with db.connect() as conn:
        conn.execute("UPDATE listings SET seen_at = NULL")
    assert post(client, lid, "dismiss", {"token": "0" * 32}).status_code == 403  # stale token: nothing happens
    assert db.get_listing(lid)["seen_at"] is None
    post(client, lid, "dismiss", {"token": fields["token"]})
    assert db.get_listing(lid)["discard_reason"] == "uninteressant" and db.get_listing(lid)["seen_at"]


def test_asks_after_opening_the_listing_while_writing(client, lid):
    """Before "Fertig", "Uninteressant?" waits on the page as a slim bar (no dialog, hidden until app.js brings it
    up); once the ad was opened (here, on the dashboard or in Telegram - the /go link) it is armed for every return.
    Its "Aussortieren" (muted, not the green primary) dismisses; ✕ keeps the listing."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    page = r.text
    assert f'data-go="{editor_access.make_open_path(lid)}"' in page  # "Kopieren & Inserat öffnen"
    assert f'<aside class="ask-sheet ask-bar" id="editor-{lid}-ask" data-ask-return="editor-{lid}" hidden' in page  # not armed
    bar = page.split('class="ask-sheet ask-bar"')[1].split("</aside>")[0]
    assert "<strong>Uninteressant?</strong>" in bar and f'action="/editor/{lid}/dismiss"' in bar
    assert '<button type="submit" class="ask-bar-act is-dismiss">Aussortieren</button>' in bar
    assert 'data-sheet-close aria-label="Schließen"' in bar and bar.count("<button") == 4  # + fold and its pill
    assert "data-ask-fold" in bar and "data-ask-unfold" in bar
    assert f'name="token" value="{fields["token"]}"' in bar
    assert "Ja, aussortieren" not in bar and ">Nein<" not in bar and "ask-later" not in bar
    assert "<dialog" not in page.replace('<dialog class="ed-sheet"', "")  # not modal (the drawer is the only dialog)
    db.mark_opened(lid)
    _, fields = open_editor(client, lid)
    assert f'data-ask-return="editor-{lid}" data-opened hidden' in client.get(f"/editor/{lid}").text
    r = post(client, lid, "dismiss", {"token": fields["token"]})
    assert r.status_code == 303 and db.get_listing(lid)["discard_reason"] == "uninteressant"
    assert "data-ask-return" not in client.get(f"/editor/{lid}").text  # nothing left to decide


def test_editor_dismiss_offers_undo(client, lid):
    """After "Aussortieren": "Aussortiert · Rückgängig" (a toast with JS, a flash without); "Rückgängig" posts the
    restore form with the form token and brings the listing back."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "dismiss", {"token": fields["token"]})
    page = client.get(r.headers["location"]).text
    toast = page.split('class="flash ok undo-toast"')[1].split("</div>")[0]
    assert ' role="status" data-toast>' in toast and '<span class="undo-toast-text">Aussortiert</span>' in toast
    assert f'<form method="post" action="/editor/{lid}/restore" class="flash-dismiss">' in toast
    assert f'name="token" value="{fields["token"]}"' in toast
    assert '<button type="submit" class="linkish">Rückgängig</button>' in toast
    assert "Aussortiert: uninteressant." not in page  # the toast instead of the message
    assert "data-toast" not in client.get(f"/editor/{lid}").text  # only right after dismissing
    restore = form_fields(toast)
    r = client.post(f"/editor/{lid}/restore", data=restore)
    assert r.headers["location"] == f"/editor/{lid}?m=restored" and db.get_listing(lid)["discard_reason"] is None
    page = client.get(r.headers["location"]).text
    assert "Zurückgeholt – das Inserat ist wieder in der Liste." in page and "<strong>Uninteressant?</strong>" in page


def test_no_after_open_question_once_finished(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Ende")
    page = client.get(f"/editor/{lid}").text
    assert "Uninteressant?" not in page and page.count("data-ask-return") == 1  # "Abgeschickt?" instead


def test_new_round_from_a_newer_draft(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "save", fields, text="Original, von Hand")
    assert post(client, lid, "new-round", fields).status_code == 400  # nothing newer yet
    newer = db.add_draft(lid, "shorter", "Kürzer aus Telegram", "claude-opus-5-5")
    page = client.get(f"/editor/{lid}").text
    assert "Es gibt ein neueres Anschreiben" in page and "Kürzer" in page and "data-confirm=" in page
    r = post(client, lid, "new-round", fields)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=round#text"
    old = db.get_session(int(fields["session_id"]))
    new = db.latest_session(lid)
    assert old["status"] == "superseded" and (new["status"], new["base_draft_id"]) == ("open", newer)
    page = client.get(f"/editor/{lid}").text
    assert "Kürzer aus Telegram</textarea>" in page and "Es gibt ein neueres Anschreiben" not in page
    r = post(client, lid, "save", fields, text="Aus dem alten Tab")  # a form of the old round
    assert r.status_code == 409 and "nicht mehr aktuell" in r.text and "Aus dem alten Tab</pre>" in r.text
    assert db.latest_session(lid)["current_text"] == "Kürzer aus Telegram"


def fake_create(monkeypatch, delay=0.0, error=None):
    """llm.create_and_store_draft replaced: records its calls, waits `delay` s, raises `error` or stores a draft."""
    import asyncio
    calls = []

    async def create(listing_id, kind, instruction=None, *, source="dashboard", record_feedback=True):
        calls.append((listing_id, kind, source, record_feedback))
        await asyncio.sleep(delay)
        if error is not None:
            raise error
        db.add_draft(listing_id, kind, "Erster Entwurf", "claude-opus-5-5", duration_ms=1234)
        return db.latest_draft(listing_id)

    monkeypatch.setattr(llm, "create_and_store_draft", create)
    return calls


def draft_status(client, listing_id) -> dict:
    r = client.get(f"/editor/{listing_id}/draft/status")
    assert r.status_code == 200
    return r.json()


def wait_for_draft(client, listing_id, timeout=5.0) -> dict:
    """Poll the status like editor.js until the background job is no longer running."""
    end = time.monotonic() + timeout
    while True:
        status = draft_status(client, listing_id)
        if status["state"] != "running" or time.monotonic() > end:
            return status
        time.sleep(0.02)


def test_editor_without_draft_shows_the_bar_and_a_get_starts_nothing(client, lid, monkeypatch):
    """Opening the editor (here from Telegram) never calls Claude: the page has the progress bar, editor.js posts
    the start; without JS the button "Anschreiben schreiben lassen" does."""
    calls = fake_create(monkeypatch)
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert 'data-draft-job="idle"' in r.text and 'role="progressbar"' in r.text and "session_id" not in fields
    assert f'data-status-url="/editor/{lid}/draft/status"' in r.text and 'data-expected-ms="40000"' in r.text
    assert f'action="/editor/{lid}/draft"' in r.text and "Anschreiben schreiben lassen</button>" in r.text
    assert 'aria-live="polite"' in r.text
    client.get(f"/editor/{lid}")
    status = draft_status(client, lid)
    assert status["state"] == "idle" and status["expected_ms"] == 40000 and status["slow_after_ms"] == 15000
    assert calls == []


def test_start_draft_in_the_background_then_the_editor_shows_it(client, lid, monkeypatch):
    calls = fake_create(monkeypatch)
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = post(client, lid, "draft", fields)  # without JS: back to the page with the bar
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}#text"
    assert wait_for_draft(client, lid)["state"] == "done"
    assert calls == [(lid, "initial", "editor", False)] and client.refreshed == [lid]
    page = client.get(f"/editor/{lid}?m=draft").text
    assert "Erster Entwurf</textarea>" in page and "Claude hat ein Anschreiben geschrieben." in page
    assert "data-draft-job" not in page and drafting._jobs == {}  # shown: the job is forgotten
    post(client, lid, "draft", fields)  # with a draft the action does nothing
    assert len(calls) == 1


def test_start_draft_is_idempotent_and_answers_json(client, lid, monkeypatch):
    """A double tap (or the listing page and Telegram at once) reuses the running job."""
    calls = fake_create(monkeypatch, delay=0.3)
    login(client)
    _, fields = open_editor(client, lid)
    first = client.post(f"/editor/{lid}/draft", data=fields, headers={"Accept": "application/json"})
    second = client.post(f"/editor/{lid}/draft", data=fields, headers={"Accept": "application/json"})
    assert first.status_code == second.status_code == 200
    assert first.json()["state"] == "running" and second.json()["state"] == "running"
    assert second.json()["elapsed_ms"] >= 0 and second.json()["expected_ms"] == drafting.DEFAULT_EXPECTED_MS
    running = client.get(f"/editor/{lid}").text  # the page while it runs: the bar, no auto start needed
    assert 'data-draft-job="running"' in running and 'http-equiv="refresh"' in running  # <noscript> reload
    assert wait_for_draft(client, lid)["state"] == "done"
    assert len(calls) == 1


def test_start_draft_needs_the_form_token(client, lid, monkeypatch):
    calls = fake_create(monkeypatch)
    login(client)
    r = client.post(f"/editor/{lid}/draft", data={"token": "0" * 32}, headers={"Accept": "application/json"})
    assert r.status_code == 403 and r.json()["state"] == "denied"
    assert client.post(f"/editor/{lid}/draft", data={}).status_code == 403
    client.cookies.clear()
    assert client.get(f"/editor/{lid}/draft/status").status_code == 403
    assert calls == [] and drafting._jobs == {}


def test_draft_error_stops_and_offers_a_retry(client, lid, monkeypatch):
    calls = fake_create(monkeypatch, error=llm.LLMError("Monatsbudget aufgebraucht."))
    login(client)
    _, fields = open_editor(client, lid)
    client.post(f"/editor/{lid}/draft", data=fields, headers={"Accept": "application/json"})
    status = wait_for_draft(client, lid)
    assert status["state"] == "error" and status["error"] == "Monatsbudget aufgebraucht."
    page = client.get(f"/editor/{lid}").text  # shows the error; nothing starts again by itself
    assert 'data-draft-job="error"' in page and "Monatsbudget aufgebraucht." in page and "Nochmal versuchen</button>" in page
    assert len(calls) == 1
    fake_create(monkeypatch)  # "Nochmal versuchen" works
    post(client, lid, "draft", fields)
    assert wait_for_draft(client, lid)["state"] == "done" and db.latest_draft(lid)["text"] == "Erster Entwurf"


def test_unexpected_draft_error_is_generic(client, lid, monkeypatch):
    fake_create(monkeypatch, error=RuntimeError("secret detail"))
    login(client)
    _, fields = open_editor(client, lid)
    post(client, lid, "draft", fields)
    status = wait_for_draft(client, lid)
    assert status["state"] == "error" and status["error"] == drafting.GENERIC_ERROR and "secret" not in str(status)


def test_old_error_is_forgotten(client, lid, monkeypatch):
    drafting._jobs[lid] = {"state": "error", "error": "alt", "started": 0.0,
                           "ended": time.monotonic() - drafting.ERROR_KEEP_S - 1}
    login(client)
    assert draft_status(client, lid)["state"] == "idle"


def test_listing_page_starts_the_draft_and_opens_the_editor(client, lid, monkeypatch):
    """"✍️ Anschreiben" on the listing page: personal words stored, the job started, the editor opens at once."""
    calls = fake_create(monkeypatch, delay=0.2)
    login(client)
    page = client.get(f"/listing/{lid}").text
    assert f'action="/listing/{lid}/draft"' in page and "Anschreiben</button>" in page
    r = client.post(f"/listing/{lid}/draft", data={"personal": "1", "own_words": "kenne die Ecke", "effort_level": "3"})
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}#text"
    assert db.get_listing(lid)["own_words"] == "kenne die Ecke" and db.get_listing(lid)["effort_level"] == 3
    assert "Claude schreibt … – ansehen" in client.get(f"/listing/{lid}").text  # while it runs
    assert wait_for_draft(client, lid)["state"] == "done" and len(calls) == 1
    r = client.post(f"/listing/{lid}/draft")  # with a draft: only opens the editor
    assert r.headers["location"] == f"/editor/{lid}#text" and len(calls) == 1
    assert "Anschreiben öffnen" in client.get(f"/listing/{lid}").text


def test_listing_page_links_the_editor_and_shows_the_final_version(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    page = client.get(f"/listing/{lid}").text
    assert f'href="/editor/{lid}"' in page and "Anschreiben öffnen" in page and "Fertiges Anschreiben" not in page
    session = learning.open_or_create_session(lid)
    learning.finish(session["id"], "Meine Endversion")
    page = client.get(f"/listing/{lid}").text
    assert "Fertiges Anschreiben öffnen" in page and "Meine Endversion</pre>" in page
    assert f'data-copy="final-{session["id"]}"' in page
    assert client.get(f"/listing/{lid}").headers["referrer-policy"] == "same-origin"  # only the editor is no-referrer


def test_diff_shows_changes_to_claudes_original(client, lid):
    db.add_draft(lid, "initial", "Guten Tag, ich bin Erika.", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    post(client, lid, "save", fields, text="Hallo, ich bin Erika & Max.")
    page = client.get(f"/editor/{lid}").text
    assert "<del>Guten Tag</del><ins>Hallo</ins>" in page and "&amp; Max" in page
    assert 'data-original="Guten Tag, ich bin Erika."' in page
    assert word_diff("a", "a") == [("equal", "a")]


def test_hint_and_rule_reasons_render_markdown_texts_stay_raw(client, lid, fake_rules):
    db.add_draft(lid, "initial", "Text mit **Sternen**", "claude-opus-5-5", hint="Bitte **Codewort** nennen")
    login(client)
    page, fields = open_editor(client, lid)
    hint = '<div class="md ed-hints-body"><p>Bitte <strong>Codewort</strong> nennen</p></div>'
    assert hint in page.text and "Hinweis von Claude" in page.text
    assert ">Text mit **Sternen**</textarea>" in page.text
    post(client, lid, "finish", fields, text="Mit *Stern*")
    session = db.latest_session(lid)
    assert wait_for_rules(client, lid) == "done"
    db.add_rule("Schreib **so**.", reason="Du hast *zweimal* gekürzt.", session_id=session["id"])
    assert ">Mit *Stern*</pre>" in client.get(f"/editor/{lid}").text
    page = client.get(f"/editor/{lid}/lernen").text
    assert '<div class="ed-reason md"><p>Du hast <em>zweimal</em> gekürzt.</p></div>' in page
    assert ">Schreib **so**.</textarea>" in page


def test_finish_and_reopen_refresh_the_telegram_message(client, lid, fake_rules, monkeypatch):
    refreshed = []
    monkeypatch.setattr(bot, "schedule_refresh", refreshed.append)
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    assert post(client, lid, "finish", fields, text="Meine Endversion").status_code == 303
    assert refreshed == [lid]  # Telegram now shows the Endversion (bot.current_view)
    wait_for_rules(client, lid)
    _, fields = open_editor(client, lid)
    assert post(client, lid, "reopen", fields).status_code == 303
    assert refreshed == [lid, lid]


# --- autosave ("Speichern" is gone: the text saves itself) --------------------------------------------------

def autosave(client, listing_id, fields, text):
    return client.post(f"/editor/{listing_id}/save", data={**fields, "text": text},
                       headers={"Accept": "application/json"})


def test_page_without_a_save_button(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    r, _ = open_editor(client, lid)
    bar = r.text.split('<div class="ed-bar">')[1].split("</div>")[0]
    assert ">Kopieren &amp; Inserat öffnen</button>" in bar and "Speichern" not in bar  # the one action
    assert 'formaction="/editor/1/finish" data-copy-open' in bar  # it finishes: learning works as before
    head = r.text.split('<header class="ed-card-head">')[1].split("</header>")[0]
    assert 'class="linkish ed-save-nojs nojs-only"' in head and ">Zwischenspeichern</button>" in head  # without JS
    assert "Kopiert den Text, öffnet das Inserat und speichert den Text als fertig" in bar
    assert 'data-save-state role="status"' in r.text and "data-save-reload hidden" in r.text


def test_autosave_answers_in_json(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = autosave(client, lid, fields, "Von Hand\r\ngeändert")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert_editor_headers(r)
    assert db.latest_session(lid)["current_text"] == "Von Hand\ngeändert"
    r = autosave(client, lid, fields, "x" * (learning.TEXT_MAX_CHARS + 1))
    assert r.status_code == 400 and r.json() == {"ok": False, "error": "Höchstens 20000 Zeichen.", "stale": False}
    r = autosave(client, lid, {**fields, "token": "0" * 32}, "Fremd")
    assert r.status_code == 403 and r.json()["error"] == "Seite veraltet – bitte neu laden."
    r = client.post(f"/editor/{lid}/save", data={**fields, "text": "Fremd"},
                    headers={"Accept": "application/json", "Origin": "https://evil.example.com"})
    assert r.status_code == 403
    assert db.latest_session(lid)["current_text"] == "Von Hand\ngeändert"

    learning.finish(int(fields["session_id"]), "Fertig in einem anderen Tab")
    r = autosave(client, lid, fields, "Noch getippt")
    assert r.status_code == 409 and r.json() == {"ok": False, "error": editor.AUTOSAVE_STALE, "stale": True}
    r = post(client, lid, "save", fields, text="Noch getippt")  # "Neu laden" posts the form: the text stays visible
    assert r.status_code == 409 and "nicht mehr aktuell" in r.text and "Noch getippt</pre>" in r.text
    assert db.latest_session(lid)["final_text"] == "Fertig in einem anderen Tab"

    with db.connect() as conn:  # the editor session from Telegram is over
        conn.execute("UPDATE editor_access_sessions SET expires_at = ?", (db.now_iso().replace("20", "19", 1),))
    r = autosave(client, lid, fields, "Später")
    assert r.status_code == 401 and r.json()["error"].startswith("Sitzung abgelaufen")
    client.cookies.clear()
    r = autosave(client, lid, fields, "Ohne Anmeldung")  # not a redirect to the login page
    assert r.status_code == 401 and r.json()["ok"] is False


def test_autosave_of_an_old_round_is_stale(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    db.add_draft(lid, "shorter", "Kürzer", "claude-opus-5-5")
    learning.start_new_round(lid)
    r = autosave(client, lid, fields, "Aus dem alten Tab")
    assert r.status_code == 409 and r.json()["stale"] is True
    assert db.latest_session(lid)["current_text"] == "Kürzer"


# --- rules: why none, and rules of one's own --------------------------------------------------------------

def test_rules_card_explains_the_search(client, lid, fake_rules):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Geändert")
    wait_for_rules(client, lid)
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Claude schaut, was du selbst geändert hast, was du von seinen Überarbeitungen behalten hast" in page
    # Claude proposed something: "Regel selbst eintragen" is there, but collapsed
    assert '<details class="rule-add">\n  <summary>Regel selbst eintragen</summary>' in page


def test_no_rule_shows_claudes_reason_and_the_manual_form(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    session = learning.finish(int(fields["session_id"]), "Geändert")
    db.update_session(session["id"], rules_basis=learning._rules_basis(session, []),
                      rules_note="Die Änderung betraf nur den Namen der Ansprechperson.")
    page = client.get(f"/editor/{lid}/lernen").text
    assert "<strong>Claude:</strong> Die Änderung betraf nur den Namen der Ansprechperson." in page
    assert "keine allgemeine Regel gefunden" not in page
    assert '<details class="rule-add is-prominent" open>' in page and f'action="/editor/{lid}/rules/add"' in page
    assert "Ein kurzer Satz als Anweisung an Claude, in Du-Form – eine Regel pro Eintrag." in page


def test_add_own_rule_in_the_editor(client, lid):
    other = db.insert_listing({"fredy_id": "e2", "title": "Andere"})[0]
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)  # from Telegram: allowed for its own listing
    _, fields = open_editor(client, lid)
    r = post(client, lid, "rules/add", fields, text="Schreib „Viele Grüße“.")
    assert r.status_code == 409 and db.list_rules() == []  # only after "Fertig"
    session = learning.finish(int(fields["session_id"]), "Ende")
    r = post(client, lid, "rules/add", fields, text=" Schreib „Viele Grüße“\nstatt „Mit freundlichen Grüßen“. ")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}/lernen?m=rule_added#regeln"
    (rule,) = db.list_rules("active")
    assert (rule["text"], rule["session_id"], rule["proposed_text"]) == (
        "Schreib „Viele Grüße“ statt „Mit freundlichen Grüßen“.", session["id"], None)
    page = client.get(r.headers["location"]).text
    assert "Regel gespeichert – sie gilt ab dem nächsten Entwurf." in page
    assert '<details class="rule-add">' in page  # done: the form folds away
    assert ('Schreib „Viele Grüße“ statt „Mit freundlichen Grüßen“. <small class="muted">(selbst eingetragen)</small>'
            in page)

    r = post(client, lid, "rules/add", fields, text="x" * 301)
    assert r.status_code == 400 and "Höchstens 300 Zeichen." in r.text
    assert '<details class="rule-add is-prominent" open>' in r.text and "x" * 301 + "</textarea>" in r.text  # kept
    r = post(client, lid, "rules/add", {**fields, "token": "0" * 32}, text="Fremd.")
    assert r.status_code == 403
    r = post(client, other, "rules/add", fields, text="Fremd.")  # another listing: no access
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
    assert db.count_rules("active") == 1

    client.cookies.clear()
    login(client)  # the dashboard login may do it too
    _, fields = open_editor(client, lid)
    assert post(client, lid, "rules/add", fields, text="Nenne kein Datum.").status_code == 303
    assert db.count_rules("active") == 2


def test_editor_shows_pictures_and_details_collapsed(client, lid):
    import io

    from PIL import Image

    other = db.insert_listing({"fredy_id": "e2", "provider": "immoscout", "title": "Andere"})[0]
    for listing_id in (lid, other):
        folder = media.media_root() / str(listing_id)
        folder.mkdir(parents=True, exist_ok=True)
        out = io.BytesIO()
        Image.new("RGB", (30, 20), (200, 100, 1)).save(out, format="JPEG")
        (folder / "1.jpg").write_bytes(out.getvalue())
        db.replace_media(listing_id, [{"position": 1, "caption": "Balkon", "source_url": "https://x.example/1",
                                       "file": "1.jpg"}])
    db.set_listing_details(lid, "Kaltmiete: 780 €\n\n## Objektbeschreibung\nSonnig und ruhig.", "Frau Muster")
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    from_telegram(client, lid)
    page = client.get(f"/editor/{lid}").text
    assert 'Bilder <span class="count">1</span>' in page and f'src="/editor/{lid}/media/1"' in page
    assert "Beschreibung &amp; Details" in page and "Sonnig und ruhig." in page and "Frau Muster" in page
    assert '<dialog class="ed-sheet" id="inserat" aria-labelledby="ed-sheet-title">' in page  # the drawer, closed
    r = client.get(f"/editor/{lid}/media/1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.headers["cache-control"] == "no-store"
    assert client.get(f"/editor/{lid}/media/2").status_code == 404
    # the Telegram session is for this one listing only; without any session: login
    assert client.get(f"/editor/{other}/media/1").status_code in (303, 401)
    client.cookies.clear()
    assert client.get(f"/editor/{lid}/media/1").headers["location"] == f"/login?next=/editor/{lid}"


def test_editor_without_exposé_shows_fredys_description(client, lid):
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    login(client)
    with db.connect() as conn:
        conn.execute("UPDATE listings SET description = ? WHERE id = ?", ("Kurzer Fredy-Text", lid))
    page = client.get(f"/editor/{lid}").text
    assert 'id="ed-sheet-text">Beschreibung</h3>' in page and "Kurzer Fredy-Text" in page


def test_first_editor_visit_fetches_pictures(client, lid, monkeypatch):
    calls = []

    async def fetch(listing_id, retry=False, want_images=True):
        calls.append(listing_id)
        db.set_media_state(listing_id, db.now_iso(), None)
        db.set_listing_details(listing_id, "## Objektbeschreibung\nFrisch geladen.", None)
        return []

    monkeypatch.setattr(media, "ensure_media", fetch)
    monkeypatch.setattr(media, "has_gallery", lambda listing: True)
    login(client)
    client.get(f"/editor/{lid}")
    assert calls == []  # no draft yet: the page shows its progress bar at once, the draft job fetches them
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    assert "Frisch geladen." in client.get(f"/editor/{lid}").text
    client.get(f"/editor/{lid}")
    assert calls == [lid]  # once; later visits read what's stored


def test_first_editor_visit_downloads_pictures_known_by_url(client, lid, monkeypatch):
    """The exposé text came first (media.ensure_details): the pictures are still to download."""
    calls = []

    async def fetch(listing_id, retry=False, want_images=True):
        calls.append(listing_id)
        return []

    monkeypatch.setattr(media, "ensure_media", fetch)
    monkeypatch.setattr(media, "has_gallery", lambda listing: True)
    db.set_media_state(lid, db.now_iso(), None)
    db.replace_media(lid, [{"position": 1, "caption": "Balkon", "source_url": "https://x.example/1", "file": ""}])
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    login(client)
    page = client.get(f"/editor/{lid}").text
    assert calls == [lid] and "Bilder laden</button>" in page  # still missing (fake fetch): the button stays


def test_editor_load_and_reload_pictures(client, lid, monkeypatch):
    loads, reloads = [], []

    async def fetch(listing_id, retry=False, want_images=True):
        loads.append((listing_id, retry))
        return []

    async def reload(listing_id):
        reloads.append(listing_id)
        return len(reloads) == 1  # the second one is "too soon"

    monkeypatch.setattr(media, "ensure_media", fetch)
    monkeypatch.setattr(media, "reload", reload)
    monkeypatch.setattr(media, "has_gallery", lambda listing: True)
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    db.set_media_state(lid, db.now_iso(), "Exposé nicht abrufbar (HTTP 429).")
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert "Erneut versuchen</button>" in r.text and "HTTP 429" in r.text
    r = post(client, lid, "media", fields)  # still failing (fake fetch): no "geladen", the reason stays visible
    assert r.headers["location"] == f"/editor/{lid}?m=media_failed#bilder" and loads[-1] == (lid, True)
    page = client.get(r.headers["location"]).text
    assert 'id="inserat" aria-labelledby="ed-sheet-title" data-open' in page and "HTTP 429" in page and "Bilder geladen" not in page
    assert client.refreshed[-1] == lid

    db.set_media_state(lid, db.now_iso(), None)  # all there now: "Inserat neu laden"
    page = client.get(f"/editor/{lid}?m=media_loaded").text
    assert "Bilder geladen." in page and 'id="inserat" aria-labelledby="ed-sheet-title" data-open' in page and "Inserat neu laden</button>" in page
    assert post(client, lid, "media", fields, reload="1").headers["location"] == f"/editor/{lid}?m=media#bilder"
    assert post(client, lid, "media", fields, reload="1").headers["location"] == f"/editor/{lid}?m=media_soon#bilder"
    assert reloads == [lid, lid]
    assert post(client, lid, "media", {"token": "0" * 32}).status_code in (303, 400, 403)  # bad token: nothing loaded
    assert len(loads) == 1


def test_button_without_login_data_reopens_an_existing_session(client, lid):
    other = db.insert_listing({"fredy_id": "e3", "provider": "immoscout", "title": "Andere"})[0]
    path = editor_access.make_editor_path(lid)
    assert client.get(path).status_code == 400  # no session yet: "Keine Anmeldung von Telegram"
    editor_cookie(client, lid)
    r = client.get(editor_access.make_editor_path(lid))  # e.g. the new button after "Fertig", Telegram sent nothing
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}"
    assert client.get(editor_access.make_editor_path(other)).status_code == 400  # the session is for one listing
    assert client.get(path[:-3] + "AAA").status_code == 400  # a forged link never counts


def test_editor_redirects_become_json_for_editor_js(client, lid):
    """editor.js sends forms with X-Editor-Nav: replace and replaces the page, so the history keeps one entry."""
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    r = client.post(f"/editor/{lid}/save", data={**fields, "text": "Neu"}, headers={"X-Editor-Nav": "replace"})
    assert r.status_code == 200 and r.json() == {"location": f"/editor/{lid}?m=saved#text"}
    assert r.headers["cache-control"] == "no-store"
    r = client.post(f"/editor/{lid}/save", data={**fields, "text": "Neuer"})  # without the header: a normal redirect
    assert r.status_code == 303
    r = client.post(f"/editor/{lid}/save", data={"text": "x"}, headers={"X-Editor-Nav": "replace"})
    assert r.status_code != 200 or "location" not in r.text  # error pages stay pages


def test_claudes_revision_is_kept_or_undone(client, lid, fake_generate):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "revise", fields, text="Original von Hand", critique="natürlicher")
    page = client.get(f"/editor/{lid}").text
    assert "Claudes Überarbeitung</strong> – „natürlicher“" in page and "👍 Übernehmen" in page
    r = post(client, lid, "revision", fields, text="Fassung von Claude 1", decision="reject")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=revision_undone#text"
    assert db.latest_session(lid)["current_text"] == "Original von Hand"
    assert "Claudes Überarbeitung</strong>" not in client.get(f"/editor/{lid}").text
    assert post(client, lid, "revision", fields, text="x", decision="accept").status_code == 400  # nothing to decide
    post(client, lid, "revise", fields, text="Original von Hand", critique="kürzer")
    r = post(client, lid, "revision", fields, text="Fassung von Claude 2, noch von Hand", decision="accept")
    assert r.headers["location"] == f"/editor/{lid}?m=revision_kept#text"
    assert db.latest_session(lid)["current_text"] == "Fassung von Claude 2, noch von Hand"
    assert [f["decision"] for f in db.list_feedback(session_id=db.latest_session(lid)["id"])] == ["accepted", "rejected"]


def test_rule_accepted_with_scope_and_strength_and_a_fact(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    db.update_settings({"profile": "Einzug: ideal November."})
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "finish", fields, text="Ende")
    session_id = db.latest_session(lid)["id"]
    rule = db.add_rule("Gern ein Smiley.", session_id=session_id, scope="privat", strength="soft")
    fact = db.add_rule("Einzug ab sofort", action="fact", session_id=session_id, fact_old="ideal November",
                       fact_new="ab sofort")
    page = client.get(f"/editor/{lid}/lernen").text
    assert 'value="privat"' in page and "„Über mich“ ergänzen" in page and "In „Über mich“ bisher:" in page
    r = post(client, lid, f"rules/{rule}", {"token": fields["token"], "session_id": str(session_id)},
             decision="accept", text="Gern ein „:)“.", scope="lockerer Ton", strength="hard", has_opts="1")
    assert r.status_code == 303
    assert {k: db.get_rule(rule)[k] for k in ("status", "text", "scope", "strength")} == {
        "status": "active", "text": "Gern ein „:)“.", "scope": "lockerer Ton", "strength": "hard"}
    r = post(client, lid, f"rules/{fact}", {"token": fields["token"], "session_id": str(session_id)},
             decision="accept", text="ab sofort")
    assert r.headers["location"] == f"/editor/{lid}/lernen?m=fact#regeln"
    assert db.get_settings()["profile"] == "Einzug: ab sofort." and db.get_rule(fact)["status"] == "merged"


def test_editor_links_the_address_to_google_maps(client, lid):
    db.add_draft(lid, "initial", "Guten Tag", "claude-opus-5-5")
    login(client)
    r, _ = open_editor(client, lid)
    link = "https://www.google.com/maps/search/?api=1&amp;query=Musterstr.%2012%2C%20K%C3%B6ln"
    assert ('<p class="ed-facts">780 € · 54 m² · Musterstr. 12 · <a href="' + link + '" target="_blank" '
            'rel="noopener noreferrer">Karte ↗</a></p>') in r.text
    assert "<iframe" not in r.text and "data-map-src" not in r.text  # only the link here, no map


# --- one action: "Kopieren & Inserat öffnen" finishes; what Claude learns has its own page --------------------------

def test_copy_and_open_finishes_and_learns_out_of_the_way(client, lid, fake_rules):
    """editor.js copies and opens the ad, then posts /finish with copied=1: the text is final (learning starts as
    before), the page says it was copied and asks "Abgeschickt?" once; the proposals wait on /lernen."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)  # from Telegram: the learn page is reachable too
    _, fields = open_editor(client, lid)
    r = post(client, lid, "finish", fields, text="Von Hand fertig", copied="1")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=done_copied#ende"
    assert db.latest_session(lid)["final_text"] == "Von Hand fertig"
    assert wait_for_rules(client, lid) == "done" and fake_rules == [db.latest_session(lid)["id"]]
    page = client.get(r.headers["location"]).text
    assert "Kopiert. Im Inserat einfügen und abschicken" in page and page.count("data-ask-return") == 1
    assert "ed-rule" not in page and f'href="/editor/{lid}/lernen"' in page
    learn = client.get(f"/editor/{lid}/lernen")
    assert learn.status_code == 200 and "Was Claude daraus lernt" in learn.text and "ed-rule" in learn.text
    assert "data-ask-return" not in learn.text  # no sending questions there
    assert client.get(f"/editor/{lid + 1}/lernen").status_code in (303, 401, 404)  # another listing: no access


def test_learn_page_before_finishing(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    page = client.get(f"/editor/{lid}/lernen").text
    assert "Noch nicht abgeschlossen" in page and "Kopieren &amp; Inserat öffnen" in page
