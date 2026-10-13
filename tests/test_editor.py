"""The draft editor (app/editor.py): the way in from Telegram (/e/…), access to /editor/{id}, login ?next=, every
action, the background rule proposals, "Als gesendet markieren" and the listing page's editor parts."""

import dataclasses
import hashlib
import hmac
import re
import time

import pytest
from fastapi.testclient import TestClient

from app import bot, db, editor, editor_access, learning, llm, main, media, vault, web
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
    assert 'href="https://portal.example/expose/1"' in page.text  # "📨 Inserat öffnen"


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
    assert client.post(f"/listing/{lid}/status", data={"status": "sent"}).headers["location"] == "/login"

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
    assert "Noch kein Entwurf" in client.get(f"/editor/{other}").text
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
    assert "„Fertig“ speichert die Endversion." in r.text  # orientation on a fresh round
    r = post(client, lid, "revise", fields, text="Original von Hand", critique="  ")
    assert r.status_code == 400 and "was Claude ändern soll" in r.text
    assert db.latest_session(lid)["current_text"] == "Original von Hand"  # hand edits saved anyway
    r = post(client, lid, "revise", fields, text="Original von Hand", critique="Kürzer bitte")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=revised#text"
    assert fake_generate == [{"kind": "custom", "previous_text": "Original von Hand", "instruction": "Kürzer bitte"}]
    page = client.get(f"/editor/{lid}").text
    assert "Fassung von Claude 1</textarea>" in page and "Fassung 2" in page
    assert "„Kürzer bitte“" in page and "Claudes Entwurf" in page  # history
    assert "Codewort Linde" in page  # Claude's hint for this text
    assert "„Fertig“ speichert die Endversion." not in page  # not once you are working on it
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
    assert "Endversion von Hand</pre>" in page and "Nenne kein Einzugsdatum.</textarea>" in page
    assert "Datum zweimal gestrichen." in page and "Bisher:" in page and "Alte Regel" in page
    assert "Claude sucht nach Regeln" not in page

    new_rule, update = db.list_rules("proposed", session_id=session["id"])
    r = post(client, lid, f"rules/{new_rule['id']}", fields, decision="accept", text="Nenne nie ein Einzugsdatum.")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=accepted#regeln"
    rule = db.get_rule(new_rule["id"])
    assert (rule["status"], rule["text"], rule["proposed_text"]) == ("active", "Nenne nie ein Einzugsdatum.",
                                                                     "Nenne kein Einzugsdatum.")
    assert post(client, lid, f"rules/{update['id']}", fields, decision="reject").status_code == 303
    assert db.get_rule(update["id"])["status"] == "rejected"
    page = client.get(f"/editor/{lid}").text
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
    assert "„Den Balkon nicht erwähnen“" in page and "nicht an Claude geschickt" in page  # in the Verlauf
    assert "Fassung" not in page.split('class="ed-history"')[1]  # no numbered Fassung for it


def test_finished_session_without_proposals_offers_to_ask(client, lid, fake_rules):
    """After a restart the background job is gone: a finished session with nothing proposed for its final version
    offers "Regeln vorschlagen lassen" instead of claiming there are none."""
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    session_id = int(fields["session_id"])
    learning.finish(session_id, "Geändert")  # finished, but no rule round (like a job lost in a restart)
    page = client.get(f"/editor/{lid}").text
    assert "Für diese Endversion hat Claude noch keine Regeln vorgeschlagen." in page
    assert f'action="/editor/{lid}/rules/retry"' in page and "Regeln vorschlagen lassen" in page
    r = post(client, lid, "rules/retry", fields)
    assert r.status_code == 303 and wait_for_rules(client, lid) == "done" and fake_rules == [session_id]
    page = client.get(f"/editor/{lid}").text
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
    page = client.get(f"/editor/{lid}").text
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
    page = client.get(f"/editor/{lid}").text
    assert "Keine Regelvorschläge: Monatsbudget von 15 € erreicht." in page and "Nochmal versuchen" in page

    async def boom(session_id):
        raise RuntimeError("secret detail")

    monkeypatch.setattr(learning, "propose_rules", boom)
    r = post(client, lid, "rules/retry", fields)
    assert r.status_code == 303 and wait_for_rules(client, lid) == "error"
    page = client.get(f"/editor/{lid}").text
    assert "Unerwarteter Fehler" in page and "secret detail" not in page


def test_waiting_page_while_rules_run(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    _, fields = open_editor(client, lid)
    session_id = int(fields["session_id"])
    learning.finish(session_id, "Ende")
    editor._rule_jobs[session_id] = {"state": "running", "error": None}
    page = client.get(f"/editor/{lid}").text
    assert "Claude sucht nach Regeln …" in page and f'data-rules-poll="/editor/{lid}/rules/status"' in page
    assert f'<noscript><meta http-equiv="refresh" content="5; url=/editor/{lid}"></noscript>' in page  # no "#…"
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
    assert "✅ Als gesendet markieren" in client.get(f"/editor/{lid}").text
    r = post(client, lid, "sent", {"token": fields["token"]})
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=sent#ende"
    assert db.get_listing(lid)["status"] == "sent" and client.refreshed == [lid, lid]  # Telegram: Endversion, then "gesendet"
    page = client.get(f"/editor/{lid}").text
    assert "als gesendet markiert" in page and "✅ Als gesendet markieren" not in page


def test_new_round_from_a_newer_draft(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    editor_cookie(client, lid)
    _, fields = open_editor(client, lid)
    post(client, lid, "save", fields, text="Original, von Hand")
    assert post(client, lid, "new-round", fields).status_code == 400  # nothing newer yet
    newer = db.add_draft(lid, "shorter", "Kürzer aus Telegram", "claude-opus-5-5")
    page = client.get(f"/editor/{lid}").text
    assert "Es gibt einen neueren Entwurf" in page and "Kürzer" in page and "data-confirm=" in page
    r = post(client, lid, "new-round", fields)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=round#text"
    old = db.get_session(int(fields["session_id"]))
    new = db.latest_session(lid)
    assert old["status"] == "superseded" and (new["status"], new["base_draft_id"]) == ("open", newer)
    page = client.get(f"/editor/{lid}").text
    assert "Kürzer aus Telegram</textarea>" in page and "Es gibt einen neueren Entwurf" not in page
    r = post(client, lid, "save", fields, text="Aus dem alten Tab")  # a form of the old round
    assert r.status_code == 409 and "nicht mehr aktuell" in r.text and "Aus dem alten Tab</pre>" in r.text
    assert db.latest_session(lid)["current_text"] == "Kürzer aus Telegram"


def test_create_first_draft_in_the_editor(client, lid, monkeypatch):
    calls = []

    async def create(listing_id, kind, instruction=None, *, source="dashboard", record_feedback=True):
        calls.append((listing_id, kind, source, record_feedback))
        db.add_draft(listing_id, kind, "Erster Entwurf", "claude-opus-5-5")
        return db.latest_draft(listing_id)

    monkeypatch.setattr(llm, "create_and_store_draft", create)
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert "Noch kein Entwurf" in r.text and "session_id" not in fields
    r = post(client, lid, "draft", fields)
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=draft#text"
    assert calls == [(lid, "initial", "editor", False)] and client.refreshed == [lid]
    assert "Erster Entwurf</textarea>" in client.get(f"/editor/{lid}").text
    post(client, lid, "draft", fields)  # with a draft the button is gone and the action does nothing
    assert len(calls) == 1


def test_listing_page_links_the_editor_and_shows_the_final_version(client, lid):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    login(client)
    page = client.get(f"/listing/{lid}").text
    assert f'href="/editor/{lid}"' in page and "Im Editor bearbeiten" in page and "Endversion" not in page
    session = learning.open_or_create_session(lid)
    learning.finish(session["id"], "Meine Endversion")
    page = client.get(f"/listing/{lid}").text
    assert "Endversion" in page and "Meine Endversion</pre>" in page and f'data-copy="final-{session["id"]}"' in page
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
    hint = '<strong class="md-label">Hinweis von Claude:</strong> <p>Bitte <strong>Codewort</strong> nennen</p>'
    assert hint in page.text
    assert ">Text mit **Sternen**</textarea>" in page.text
    post(client, lid, "finish", fields, text="Mit *Stern*")
    session = db.latest_session(lid)
    assert wait_for_rules(client, lid) == "done"
    db.add_rule("Schreib **so**.", reason="Du hast *zweimal* gekürzt.", session_id=session["id"])
    page = client.get(f"/editor/{lid}").text
    assert '<div class="ed-reason md"><p>Du hast <em>zweimal</em> gekürzt.</p></div>' in page
    assert ">Mit *Stern*</pre>" in page and ">Schreib **so**.</textarea>" in page


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
