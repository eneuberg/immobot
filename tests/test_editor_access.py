"""Editor access from Telegram: signed link, Telegram login data, one-time hashes, link window, browser sessions."""

import hashlib
import hmac

import pytest

from app import bot, db, editor_access
from tests.conftest import TEST_SECRETS

NOW = 1_800_000_000  # a fixed "now" (2027) for every check
DAY = 24 * 3600
BOT_TOKEN = "123456789:" + "T" * 35
USER = 1001


def login_params(user_id=USER, auth_date=NOW - 5, token=BOT_TOKEN, **extra):
    """Login data as Telegram appends them to a login_url, signed like Telegram does."""
    fields = {"id": str(user_id), "first_name": "Erika", "username": "erika", "auth_date": str(auth_date), **extra}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(token.encode()).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), "sha256").hexdigest()
    return fields


def link(listing_id, created=NOW - 3600):
    _, _, lid, exp, sig = editor_access.make_editor_path(listing_id, now=created).split("/")
    return int(lid), exp, sig


@pytest.fixture
def lid(fresh_vault):
    fresh_vault.set("telegram_bot_token", BOT_TOKEN)
    fresh_vault.set("telegram_allowed_user_ids", f"{USER},1002")
    return db.insert_listing({"fredy_id": "e1", "title": "Wohnung"})[0]


# --- signed link -----------------------------------------------------------------------------

def test_link_sign_and_verify(fresh_vault):
    path = editor_access.make_editor_path(7, now=NOW)
    _, e, listing, exp, sig = path.split("/")
    assert (e, listing, exp) == ("e", "7", str(NOW + editor_access.LINK_VALID_S))
    assert len(sig) == 43 and "=" not in sig  # base64url SHA-256 without padding
    assert editor_access.verify_editor_link(7, exp, sig, now=NOW)
    assert editor_access.verify_editor_link("7", int(exp), sig, now=NOW + editor_access.LINK_VALID_S - 1)


def test_link_expiry_and_tampering(fresh_vault):
    _, _, _, exp, sig = editor_access.make_editor_path(7, now=NOW).split("/")
    assert not editor_access.verify_editor_link(7, exp, sig, now=int(exp))  # expired
    assert not editor_access.verify_editor_link(8, exp, sig, now=NOW)  # another listing
    assert not editor_access.verify_editor_link(7, str(int(exp) + 1), sig, now=NOW)  # longer validity
    assert not editor_access.verify_editor_link(7, exp, sig[:-1] + ("A" if sig[-1] != "A" else "B"), now=NOW)
    for bad in [("x", exp, sig), (7, "abc", sig), (7, "-1", sig), (7, exp, "ä" * 43), (7, exp, None),
                (7, exp, "A" * 500), (0, exp, sig)]:
        assert not editor_access.verify_editor_link(*bad, now=NOW)
    fresh_vault.init("another-session-secret-" + "y" * 40, {})  # SESSION_SECRET changed: old links are invalid
    assert not editor_access.verify_editor_link(7, exp, sig, now=NOW)
    fresh_vault.init(TEST_SECRETS["SESSION_SECRET"], {})
    assert editor_access.verify_editor_link(7, exp, sig, now=NOW)


# --- Telegram login data -----------------------------------------------------------------------

def test_telegram_login_hash():
    assert editor_access.verify_telegram_login(login_params(), BOT_TOKEN, now=NOW) == USER
    assert editor_access.verify_telegram_login(login_params(photo_url="https://t.me/i/x.jpg"), BOT_TOKEN, now=NOW) == USER
    forged = {**login_params(), "id": "666"}
    assert editor_access.verify_telegram_login(forged, BOT_TOKEN, now=NOW) is None
    assert editor_access.verify_telegram_login(login_params(token="1:" + "X" * 35), BOT_TOKEN, now=NOW) is None
    assert editor_access.verify_telegram_login(login_params(), "", now=NOW) is None
    without_hash = login_params()
    del without_hash["hash"]
    assert editor_access.verify_telegram_login(without_hash, BOT_TOKEN, now=NOW) is None
    assert editor_access.verify_telegram_login({**login_params(), "hash": "zz"}, BOT_TOKEN, now=NOW) is None
    assert editor_access.verify_telegram_login({}, BOT_TOKEN, now=NOW) is None


def test_telegram_login_freshness():
    check = editor_access.verify_telegram_login
    assert check(login_params(auth_date=NOW - 600), BOT_TOKEN, now=NOW) == USER
    assert check(login_params(auth_date=NOW - 601), BOT_TOKEN, now=NOW) is None
    assert check(login_params(auth_date=NOW + 60), BOT_TOKEN, now=NOW) == USER  # small clock skew
    assert check(login_params(auth_date=NOW + 61), BOT_TOKEN, now=NOW) is None
    assert check(login_params(auth_date=NOW - 100), BOT_TOKEN, now=NOW, max_age_s=50) is None


# --- browser sessions ---------------------------------------------------------------------------

def test_start_and_check_session(lid):
    cookie, error = editor_access.start_editor_browser_session(*link(lid), login_params(), now=NOW)
    assert error is None and cookie
    with db.connect() as conn:  # only the cookie's hash is stored
        (row,) = [dict(r) for r in conn.execute("SELECT * FROM editor_access_sessions")]
    assert cookie not in str(row) and row["token_hash"] == hashlib.sha256(cookie.encode()).hexdigest()
    assert (row["listing_id"], row["telegram_user_id"]) == (lid, USER)
    assert row["expires_at"] == editor_access._iso(NOW + DAY)
    assert editor_access.check_editor_browser_session(cookie, lid, now=NOW + DAY - 1) == "ok"
    assert editor_access.check_editor_browser_session(cookie, str(lid), now=NOW + DAY) == "expired"
    assert editor_access.check_editor_browser_session(cookie, lid + 1, now=NOW) == "none"  # only this listing
    for other in [None, "", "x" * 10, cookie[:-1] + ("a" if cookie[-1] != "a" else "b"), "ä" * 43]:
        assert editor_access.check_editor_browser_session(other, lid, now=NOW) == "none"
    assert editor_access.get_editor_browser_session(cookie)["telegram_user_id"] == USER


def test_start_session_errors(lid, fresh_vault):
    start = editor_access.start_editor_browser_session
    listing, exp, sig = link(lid)
    assert start(listing, exp, sig[:-2] + "AA", login_params(), now=NOW) == (None, "bad_link")
    assert start(listing + 1, exp, sig, login_params(), now=NOW) == (None, "bad_link")
    assert start(*link(999), login_params(), now=NOW) == (None, "bad_link")  # signed, but no such listing
    assert start(*link(lid, created=NOW - 8 * DAY), login_params(), now=NOW) == (None, "link_expired")
    assert start(listing, exp, sig, {**login_params(), "username": "mallory"}, now=NOW) == (None, "bad_login")
    assert start(listing, exp, sig, login_params(auth_date=NOW - 3600), now=NOW) == (None, "login_stale")
    assert start(listing, exp, sig, login_params(user_id=2002), now=NOW) == (None, "not_allowed")
    params = login_params(auth_date=NOW - 1)
    assert start(listing, exp, sig, params, now=NOW)[1] is None
    assert start(listing, exp, sig, params, now=NOW) == (None, "login_reused")  # a copied URL doesn't work
    fresh_vault.clear("telegram_allowed_user_ids")
    assert start(listing, exp, sig, login_params(auth_date=NOW - 2), now=NOW) == (None, "allowlist_missing")
    fresh_vault.clear("telegram_bot_token")
    assert start(listing, exp, sig, login_params(auth_date=NOW - 3), now=NOW) == (None, "bad_login")
    assert set(editor_access.ERROR_CODES) == {"bad_link", "link_expired", "bad_login", "login_stale", "login_reused",
                                              "allowlist_missing", "not_allowed", "window_over"}


def test_link_window_of_one_day(lid):
    start = editor_access.start_editor_browser_session
    the_link = link(lid)
    first, _ = start(*the_link, login_params(), now=NOW)
    later = NOW + DAY - 60  # a new tap in Telegram within the day: a new session, same end
    second, error = start(*the_link, login_params(auth_date=later - 5), now=later)
    assert error is None and second != first
    assert editor_access.get_editor_browser_session(second)["expires_at"] == editor_access._iso(NOW + DAY)
    after = NOW + DAY
    assert start(*the_link, login_params(auth_date=after - 5), now=after) == (None, "window_over")
    assert editor_access.check_editor_browser_session(first, lid, now=after) == "expired"
    fresh_link = link(lid, created=after - 10)  # a new draft message brings a new link with its own day
    assert start(*fresh_link, login_params(auth_date=after - 4), now=after)[1] is None


def test_used_hashes_are_purged_after_a_day(lid):
    params = login_params()
    assert db.use_login_hash("h1", editor_access._iso(NOW), editor_access._iso(NOW - DAY))
    assert not db.use_login_hash("h1", editor_access._iso(NOW + 10), editor_access._iso(NOW + 10 - DAY))
    assert db.use_login_hash("h1", editor_access._iso(NOW + DAY + 1), editor_access._iso(NOW + 1))
    assert params["hash"]  # the real hashes are stored as SHA-256 only
    editor_access.start_editor_browser_session(*link(lid), params, now=NOW)
    with db.connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT hash_sha256 FROM used_login_hashes")]
    assert hashlib.sha256(params["hash"].encode()).hexdigest() in stored and params["hash"] not in stored


# --- Telegram button ------------------------------------------------------------------------------

def test_editor_button(fresh_vault, monkeypatch):
    assert bot.editor_url(5) is None and bot.editor_button(5) is None  # public address unknown
    monkeypatch.setattr(bot, "_public_base", "https://immobot.example.com")
    button = bot.editor_button(5)
    assert button.text == "📝 Bearbeiten" and button.url is None and button.callback_data is None
    url = button.login_url.url
    assert url.startswith("https://immobot.example.com/e/5/")
    _, _, listing, exp, sig = url.removeprefix("https://immobot.example.com").split("/")
    assert editor_access.verify_editor_link(listing, exp, sig)
    assert bot.editor_url(5).startswith("https://immobot.example.com/e/5/")


def test_allowed_user_ids_live_in_the_vault(fresh_vault):
    assert fresh_vault.allowed_user_ids() == set() == bot.allowed_user_ids()
    fresh_vault.set("telegram_allowed_user_ids", "12, 34")
    assert fresh_vault.allowed_user_ids() == {12, 34} == bot.allowed_user_ids()
