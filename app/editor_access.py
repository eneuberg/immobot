"""Access to the draft editor from Telegram, without the dashboard password (PLAN-LERNEN.md section 2).

The "📝 Editor" button is a Telegram login_url button (bot.editor_button) pointing at a signed link:
  /e/{listing_id}/{exp}/{sig}  sig = base64url HMAC-SHA256 (no padding) of "editor:{listing_id}:{exp}", keyed with
                               vault.editor_link_key() (HKDF from SESSION_SECRET); exp = creation + LINK_VALID_S.
When tapped, Telegram appends the user's signed login data (id, first_name, ..., auth_date, hash).
start_editor_browser_session() checks, in this order:
  (a) link signature and expiry            -> "bad_link" / "link_expired"
  (b) Telegram login hash (bot token)      -> "bad_login"
  (c) auth_date at most 10 minutes old, at most 60 s in the future -> "login_stale"
  (d) login hash never used before         -> "login_reused" (remembered for a day)
  (e) Telegram user on "Erlaubte Telegram-User-IDs" (required here) -> "allowlist_missing" / "not_allowed"
  (f) within 24 h of the link's first successful use -> "window_over"
and then creates an editor browser session: a random cookie value (only its SHA-256 is stored) for one listing,
valid until the link's first use + 24 h. check_editor_browser_session() checks that cookie on /editor/{id}.
A copied or forwarded URL doesn't work: its login hash is single-use and only 10 minutes fresh.
"""

import base64
import hashlib
import hmac
import re
import secrets
import time
from datetime import datetime, timezone

from . import db, vault

LINK_VALID_S = 7 * 24 * 3600
SESSION_WINDOW_S = 24 * 3600  # editor sessions over one link: until its first use + 1 day
LOGIN_MAX_AGE_S = 600
LOGIN_FUTURE_SKEW_S = 60
USED_HASH_KEEP_S = 24 * 3600
EDITOR_COOKIE = "immobot_editor"  # HttpOnly, Secure like the session cookie, SameSite=Lax
ERROR_CODES = ("bad_link", "link_expired", "bad_login", "login_stale", "login_reused", "allowlist_missing",
               "not_allowed", "window_over")

_EXP = re.compile(r"[0-9]{1,12}")
_HASH = re.compile(r"[0-9a-f]{64}")
_COOKIE = re.compile(r"[A-Za-z0-9_-]{20,100}")


def _iso(ts: float) -> str:
    """Unix time -> ISO string in the format of db.now_iso() (so stored times compare as strings)."""
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat(timespec="seconds")


def _ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# --- signed link ------------------------------------------------------------------------

def _signature(listing_id: int, exp: int) -> str:
    message = f"editor:{int(listing_id)}:{int(exp)}".encode("ascii")
    digest = hmac.new(vault.editor_link_key(), message, "sha256").digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def make_editor_path(listing_id: int, now: float | None = None) -> str:
    """'/e/12/1760000000/AbC…' - the token is valid for LINK_VALID_S from `now` (default: the current time)."""
    exp = int(time.time() if now is None else now) + LINK_VALID_S
    return f"/e/{int(listing_id)}/{exp}/{_signature(listing_id, exp)}"


def _link_state(listing_id, exp, sig, now: float) -> str:
    """"ok", "expired" (correctly signed but past exp) or "bad" (anything else). Values may come raw from the URL."""
    try:
        listing_id = int(listing_id)
    except (TypeError, ValueError):
        return "bad"
    exp = str(exp)
    if listing_id <= 0 or not _EXP.fullmatch(exp) or not isinstance(sig, str) or len(sig) > 100:
        return "bad"
    if not hmac.compare_digest(sig.encode("utf-8"), _signature(listing_id, int(exp)).encode("ascii")):
        return "bad"
    return "ok" if int(exp) > now else "expired"


def verify_editor_link(listing_id, exp, sig, now: float | None = None) -> bool:
    """True if `sig` signs this listing and expiry and the link hasn't expired."""
    return _link_state(listing_id, exp, sig, time.time() if now is None else now) == "ok"


# --- Telegram login data -------------------------------------------------------------------

def _telegram_login(params: dict, bot_token: str, now: float, max_age_s: int) -> tuple[int | None, str | None]:
    """(user id, None) or (None, "bad_login" / "login_stale")."""
    fields = {str(key): str(value) for key, value in (params or {}).items()}
    given = fields.pop("hash", "")
    if not bot_token or not _HASH.fullmatch(given) or not fields.get("id", "").isdigit():
        return None, "bad_login"
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), "sha256").hexdigest()
    if not hmac.compare_digest(given.encode("ascii"), expected.encode("ascii")):
        return None, "bad_login"
    auth_date = fields.get("auth_date", "")
    if not _EXP.fullmatch(auth_date):
        return None, "bad_login"
    if now - int(auth_date) > max_age_s or int(auth_date) - now > LOGIN_FUTURE_SKEW_S:
        return None, "login_stale"
    return int(fields["id"]), None


def verify_telegram_login(params: dict, bot_token: str, now: float | None = None,
                          max_age_s: int = LOGIN_MAX_AGE_S) -> int | None:
    """The Telegram user id if `params` (the query parameters Telegram appended: id, first_name, last_name,
    username, photo_url, auth_date, hash) carry a valid login hash for this bot and auth_date is fresh; else None.
    hash = hex HMAC-SHA256 of the data check string (all fields but hash, sorted, "key=value", "\\n"-joined),
    keyed with SHA-256(bot token). Doesn't mark the hash as used (start_editor_browser_session does)."""
    return _telegram_login(params, bot_token, time.time() if now is None else now, max_age_s)[0]


# --- editor browser sessions -------------------------------------------------------------------

def start_editor_browser_session(listing_id, exp, sig, telegram_params: dict,
                                 now: float | None = None) -> tuple[str | None, str | None]:
    """GET /e/{listing_id}/{exp}/{sig}?<Telegram login data>: run checks (a)-(f) (module docstring) and start an
    editor browser session. Returns (cookie value, None), or (None, error code from ERROR_CODES). Set the cookie
    as EDITOR_COOKIE and redirect to /editor/{listing_id}."""
    now = time.time() if now is None else now
    state = _link_state(listing_id, exp, sig, now)
    if state != "ok":
        return None, "link_expired" if state == "expired" else "bad_link"
    listing_id = int(listing_id)
    if db.get_listing(listing_id) is None:
        return None, "bad_link"

    user_id, error = _telegram_login(telegram_params, vault.get("telegram_bot_token") or "", now, LOGIN_MAX_AGE_S)
    if error:
        return None, error
    # Single use: remembered whatever happens next, so a captured URL can't be tried again.
    if not db.use_login_hash(_sha256(str(telegram_params["hash"])), _iso(now), _iso(now - USED_HASH_KEEP_S)):
        return None, "login_reused"

    allowed = vault.allowed_user_ids()
    if not allowed:  # empty, or unreadable (SESSION_SECRET changed): the list is required for this way in
        return None, "allowlist_missing"
    if user_id not in allowed:
        return None, "not_allowed"

    link_key = _sha256(f"{listing_id}:{int(exp)}:{sig}")
    first_used = db.editor_link_first_use(link_key, listing_id, _iso(now), _iso(now - LINK_VALID_S - SESSION_WINDOW_S))
    expires = _ts(first_used) + SESSION_WINDOW_S
    if now >= expires:
        return None, "window_over"

    cookie = secrets.token_urlsafe(32)
    db.add_editor_access_session(_sha256(cookie), listing_id, user_id, _iso(now), _iso(expires),
                                 purge_before=_iso(now - LINK_VALID_S))
    return cookie, None


def get_editor_browser_session(cookie_value: str | None) -> dict | None:
    """The stored session (listing_id, telegram_user_id, created_at, expires_at) for a cookie value, or None."""
    if not isinstance(cookie_value, str) or not _COOKIE.fullmatch(cookie_value):
        return None
    return db.get_editor_access_session(_sha256(cookie_value))


def check_editor_browser_session(cookie_value: str | None, listing_id, now: float | None = None) -> str:
    """"ok" (valid for this listing), "expired" (was valid for it: "Sitzung abgelaufen – bitte anmelden") or
    "none" (no cookie, unknown, or for another listing: redirect to /login?next=…). Expired sessions are kept for
    LINK_VALID_S so the friendly page shows."""
    session = get_editor_browser_session(cookie_value)
    try:
        listing_id = int(listing_id)
    except (TypeError, ValueError):
        return "none"
    if session is None or session["listing_id"] != listing_id:
        return "none"
    return "ok" if (time.time() if now is None else now) < _ts(session["expires_at"]) else "expired"
