"""Dashboard (login, listings, drafts, settings, credentials, status) and the Fredy webhook.

Security model (see PLAN.md section 3):
- Every page needs a logged-in session, except /healthz, /login and /hook/fredy (Bearer token).
- Secret values are never passed to templates: the settings pages and /status only show whether and where a
  credential is set (the one exception is a freshly generated webhook token, shown once on the response
  that created it). Error texts shown on a page additionally go through redact().
- Changing a credential needs the dashboard password again (step-up); wrong passwords count toward the
  login limit. Every change is audited and announced in Telegram, and so are repeated wrong passwords.
- Sessions end 7 days after login or after 24 h without a request, on logout, and when the password or
  SESSION_SECRET changes.
- Listing data from Fredy is untrusted: it is rendered only through Jinja autoescape, and links/images
  only after portals.safe_url().
- CSRF: the session cookie is SameSite=Strict; POSTs with a foreign Origin header are rejected too.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import math
import re
import secrets
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import bot, db, is24, llm, media, portals, settings, vault

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape is on for .html
STATIC_DIR = Path(__file__).parent / "static"  # app.css / app.js, served at /static (main.py)

LOCAL_TZ = settings.LOCAL_TZ

DRAFT_KIND_LABELS = {
    "initial": "Erstentwurf",
    "regenerate": "Neu erzeugt",
    "shorter": "Kürzer",
    "formal": "Förmlicher",
    "custom": "Anpassung",
    "test": "Test (Einstellungen)",
}
EFFORT_LABELS = {"low": "niedrig", "medium": "mittel", "high": "hoch"}

TEXT_SETTINGS = ["system_prompt", "profile", "wishes", "style"]  # textareas, each with a reset button
CHECKBOX_SETTINGS = ["send_image", "paused", "is24_fetch"]
TEXT_SETTING_LABELS = {"system_prompt": "System-Prompt", "profile": "Über mich", "wishes": "Wünsche", "style": "Schreibstil"}
MAX_TEXT_CHARS = 20_000
FREDY_BASE_URL_MAX = 200

# "Einstellungen" has three sub-pages. Each form posts its section, so it saves only its own checkboxes.
SETTINGS_SECTIONS = {
    "texte": {"path": "/settings", "label": "Anschreiben", "template": "settings.html"},
    "ki": {"path": "/settings/ki", "label": "KI & Kosten", "template": "settings_ki.html"},
    "telegram": {"path": "/settings/telegram", "label": "Telegram & Fredy", "template": "settings_telegram.html"},
}
SECTION_FIELDS = {
    "texte": [*TEXT_SETTINGS, "target_words"],
    "ki": ["provider", "model", "effort", "monthly_budget_eur", "eur_usd_rate", "send_image", "llm_max_images", "is24_fetch"],
    "telegram": ["followup_delay_s", "paused", "fredy_base_url"],
}
FIELD_SECTION = {field: section for section, fields in SECTION_FIELDS.items() for field in fields}
AI_CREDENTIALS = {info["credential"]: provider for provider, info in settings.PROVIDERS.items()}  # managed on "KI & Kosten"
OPTIONAL_CREDENTIALS = {"telegram_allowed_user_ids"}

GENERIC_DRAFT_ERROR = "Unerwarteter Fehler beim Generieren – Details im Log."

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 300
LOGIN_FAIL_DELAY_S = 1.0
LOGIN_ALERT_AT = 3  # failures of one IP within the window that trigger a Telegram warning (and again at the limit)
LOGIN_GLOBAL_ALERT_AT = 20  # failures across all IPs within LOGIN_GLOBAL_WINDOW_S: warning, but no global lockout
LOGIN_GLOBAL_WINDOW_S = 600
LOGIN_ALERT_INTERVAL_S = 600  # at most one warning per kind ("threshold", "blocked", "global") per 10 minutes
LOGIN_TRACKED_MAX = 10_000  # tracked IPs; beyond that the stalest is dropped
# limiter key (IP, or IPv6 /64) -> monotonic times of failed logins. Ordered: least recently failed first.
_login_failures: dict[str, deque[float]] = {}
_recent_failures: deque[float] = deque(maxlen=LOGIN_GLOBAL_ALERT_AT)  # the latest failures across all IPs
_last_login_alert: dict[str, float] = {}  # alert kind -> monotonic time it was last sent

SESSION_MAX_AGE_S = 7 * 24 * 3600  # absolute, counted from login (main.py uses it for the cookie too)
SESSION_IDLE_S = 24 * 3600
SESSION_SEEN_REFRESH_S = 300  # rewrite the cookie at most every 5 minutes to keep seen_at current


# --- helpers ------------------------------------------------------------------

def local_time(value) -> str:
    """ISO timestamp (UTC) -> '09.10.2026 21:23' in German local time."""
    if not value:
        return "–"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    elif isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return str(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def _price(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


templates.env.filters["eur"] = settings.fmt_eur
templates.env.filters["local_time"] = local_time
templates.env.filters["as_bool"] = settings.as_bool
templates.env.filters["safe_url"] = portals.safe_url  # untrusted listing URLs -> http(s) or None
templates.env.globals["STATUSES"] = settings.STATUSES
# Content hash in the /static URLs: browsers may cache the files for a day, a deploy still takes effect at once.
templates.env.globals["STATIC_VERSION"] = hashlib.sha256(
    b"".join((STATIC_DIR / name).read_bytes() for name in ("app.css", "app.js"))).hexdigest()[:12]


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def redact(request: Request, text: str) -> str:
    """Defense in depth: blank out any secret value that slipped into a message we are about to show."""
    config = request.app.state.config
    values = vault.secret_values() + [config.dashboard_password, config.session_secret]
    for value in sorted(filter(None, values), key=len, reverse=True):
        text = text.replace(value, "***")
    return text


# --- security middleware & dependencies ----------------------------------------

CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    # Not "no-referrer": with that, browsers send "Origin: null" on same-site form posts.
    # External links/images opt out per element (rel=noreferrer / referrerpolicy=no-referrer).
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",  # every response is dynamic and most are behind login
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",  # browsers ignore it over plain HTTP
}


async def security_headers(request: Request, call_next):
    """HTTP middleware; registered in main.py via app.middleware("http")(security_headers).
    A route that sets its own Cache-Control (the listing pictures) keeps it; everything else is no-store."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        if name == "Cache-Control" and name in response.headers:
            continue
        response.headers[name] = value
    return response


def _session_stamp(request: Request) -> str:
    """Binds a session to the current password, session secret and logout generation.
    Changing DASHBOARD_PASSWORD / SESSION_SECRET or logging out invalidates every existing cookie."""
    config = request.app.state.config
    generation = db.get_meta("session_generation") or "0"
    message = f"{generation}:{config.dashboard_password}".encode("utf-8")
    return hmac.new(config.session_secret.encode("utf-8"), message, "sha256").hexdigest()


def _now() -> float:
    return time.time()  # wall clock for session ages (tests replace this function)


def is_logged_in(request: Request) -> bool:
    session = request.session
    stamp = session.get("stamp")
    if session.get("auth") is not True or not isinstance(stamp, str) or not hmac.compare_digest(stamp, _session_stamp(request)):
        return False
    login_at, seen_at, now = session.get("login_at"), session.get("seen_at"), _now()
    if not isinstance(login_at, (int, float)) or not isinstance(seen_at, (int, float)):
        return False  # cookie from before session timeouts existed: log in again
    if now - login_at > SESSION_MAX_AGE_S or now - seen_at > SESSION_IDLE_S:
        return False
    if now - seen_at > SESSION_SEEN_REFRESH_S:
        session["seen_at"] = int(now)
    return True


def _remember_base_url(request: Request) -> None:
    """Scheme and host of a trusted request (logged in, or the authenticated webhook) become the address of
    the "🔗 Dashboard" buttons in Telegram, unless IMMOBOT_PUBLIC_URL fixes it."""
    bot.remember_public_url(f"{request.url.scheme}://{request.url.netloc}")


async def require_login(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    _remember_base_url(request)


async def same_origin(request: Request) -> None:
    """Cheap CSRF backstop on top of SameSite=Strict: reject form posts from another site."""
    origin = request.headers.get("origin")
    # "null" (privacy settings, sandboxed contexts) can't carry our SameSite=Strict cookie cross-site anyway.
    if origin not in (None, "null") and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Anfrage von fremder Herkunft abgelehnt")


PAGE = [Depends(require_login)]
ACTION = [Depends(same_origin), Depends(require_login)]


# --- password checks (login form and step-up for credential changes share one per-IP limit) ----

def _client_ip(request: Request) -> str:
    # The proxy sets X-Forwarded-For and uvicorn trusts only private proxy addresses (Dockerfile),
    # so this is the real client. Capped because it ends up in the audit log and Telegram warnings.
    return (request.client.host if request.client else "?")[:64]


def _limiter_key(client: str) -> str:
    """IPv6 clients are counted per /64: one connection usually owns a whole /64 and could rotate within it."""
    try:
        ip = ipaddress.ip_address(client)
    except ValueError:
        return client
    if ip.version == 6 and ip.ipv4_mapped is None:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return client


def _too_many_failures(client: str) -> bool:
    """Forget failures older than the window (all clients), then check this client's count."""
    now = time.monotonic()
    for key in list(_login_failures):
        attempts = _login_failures[key]
        while attempts and attempts[0] < now - LOGIN_WINDOW_S:
            attempts.popleft()
        if not attempts:
            del _login_failures[key]
    return len(_login_failures.get(_limiter_key(client), ())) >= LOGIN_MAX_FAILURES


async def _login_alert(kind: str, text: str) -> bool:
    """Telegram warning about failed logins, at most one per kind every LOGIN_ALERT_INTERVAL_S. True if sent."""
    now = time.monotonic()
    last = _last_login_alert.get(kind)
    if last is not None and now - last < LOGIN_ALERT_INTERVAL_S:
        return False
    _last_login_alert[kind] = now
    await bot.notify(text, bot.configured_chat_id())
    return True


async def _count_failure(client: str) -> None:
    """Record a wrong dashboard password (login or step-up), then wait a bit.

    - Per IP (IPv6: /64): at LOGIN_ALERT_AT failures and at the limit, an audit entry and a Telegram warning.
    - Across all IPs: LOGIN_GLOBAL_ALERT_AT failures within LOGIN_GLOBAL_WINDOW_S send one warning
      (no global lockout - that would lock the owner out too).
    Warnings never contain the attempted password."""
    key, now = _limiter_key(client), time.monotonic()
    attempts = _login_failures.pop(key, deque())  # re-inserted at the end: the dict stays least-recent-first
    attempts.append(now)
    _login_failures[key] = attempts
    if len(_login_failures) > LOGIN_TRACKED_MAX:
        del _login_failures[next(iter(_login_failures))]  # bound memory by dropping the stalest, never the new one
    _recent_failures.append(now)
    when = datetime.now(LOCAL_TZ).strftime("%d.%m. %H:%M")

    if len(attempts) in (LOGIN_ALERT_AT, LOGIN_MAX_FAILURES):
        db.add_credential_event("dashboard", "login_failed", key)
        if len(attempts) >= LOGIN_MAX_FAILURES:
            await _login_alert("blocked", f"⚠️ {len(attempts)} fehlgeschlagene Dashboard-Logins von {key} – "
                                          f"für {LOGIN_WINDOW_S // 60} Minuten gesperrt ({when})")
        else:
            await _login_alert("threshold", f"⚠️ {len(attempts)} fehlgeschlagene Dashboard-Logins von {key} ({when})")
    if len(_recent_failures) == LOGIN_GLOBAL_ALERT_AT and now - _recent_failures[0] <= LOGIN_GLOBAL_WINDOW_S:
        text = f"⚠️ Viele fehlgeschlagene Logins von verschiedenen IPs ({LOGIN_GLOBAL_ALERT_AT} in 10 Minuten, {when})"
        if await _login_alert("global", text):
            db.add_credential_event("dashboard", "login_failed_many", None)
    await asyncio.sleep(LOGIN_FAIL_DELAY_S)


def _password_ok(request: Request, password: str) -> bool:
    expected = request.app.state.config.dashboard_password
    return hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8"))


# --- public endpoints -----------------------------------------------------------

@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.post("/hook/fredy")
async def fredy_webhook(request: Request):
    """Receives Fredy's HTTP notification adapter call (one job run, possibly several listings)."""
    config = request.app.state.config
    token = vault.get("fredy_webhook_token")
    if not token:  # not configured yet: nothing can authenticate
        return JSONResponse({"error": "unavailable"}, status_code=503)
    expected = f"Bearer {token}".encode()
    given = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(given, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    _remember_base_url(request)

    try:
        payload = await request.json()
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "expected a JSON object"}, status_code=400)

    db.set_meta("last_webhook_at", db.now_iso())
    if payload.get("event") == "priceChange":
        return {"ignored": True}

    items = payload.get("listings")
    if not isinstance(items, list):
        return JSONResponse({"error": "listings must be a list"}, status_code=400)

    received, new_ids, with_images = 0, [], 0
    for item in items:
        if not isinstance(item, dict) or item.get("id") in (None, ""):
            continue
        listing_id, created = db.insert_listing({
            "fredy_id": item.get("id"),
            "job_id": payload.get("jobId"),
            "provider": payload.get("provider"),
            "title": item.get("title"),
            "price": item.get("price"),
            "size": item.get("size"),
            "address": item.get("address"),
            "description": item.get("description"),
            "image_url": item.get("imageUrl"),
            "url": item.get("url"),
            "fredy_url": item.get("fredyUrl"),
        })
        received += 1
        if created:
            new_ids.append(listing_id)
        # Optional gallery (Fredy PR #489 / patched Fredy). Bad entries are dropped, never the whole webhook;
        # a listing that already has pictures keeps them (no duplicates when Fredy sends it again).
        pictures = media.parse_fredy_images(item.get("images"))
        if pictures:
            with_images += 1
            media.store_fredy_images(listing_id, pictures)

    if received:  # for the status page: does this Fredy send pictures?
        db.set_meta("last_webhook_had_images", "1" if with_images else "0")
        if with_images:
            db.set_meta("fredy_images_last_at", db.now_iso())
    log.info("Fredy webhook: %d listings, %d new, %d with pictures", received, len(new_ids), with_images)
    if new_ids and not config.disable_bot:
        bot.schedule_announce(new_ids)
    return {"received": received, "new": len(new_ids)}


@router.get("/login")
async def login_page(request: Request):
    if is_logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(same_origin)])
async def login(request: Request, password: str = Form("")):
    # Limited per client IP, so a stranger hammering the login can't lock the owner out.
    client = _client_ip(request)
    if _too_many_failures(client):
        return render(request, "login.html", {"error": "Zu viele Versuche, bitte warten."}, status_code=429)
    if not _password_ok(request, password):
        log.warning("Failed dashboard login from %s", client)
        await _count_failure(client)
        return render(request, "login.html", {"error": "Falsches Passwort."}, status_code=401)

    request.session.clear()
    request.session["auth"] = True
    request.session["stamp"] = _session_stamp(request)
    request.session["login_at"] = request.session["seen_at"] = int(_now())
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(same_origin)])
async def logout(request: Request):
    # Single user: logging out ends every session, including copies of the cookie elsewhere. Only a
    # logged-in request may do that - otherwise anyone could log the owner out with one POST.
    if is_logged_in(request):
        generation = int(db.get_meta("session_generation") or "0") + 1
        db.set_meta("session_generation", str(generation))
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- listings -------------------------------------------------------------------

def _listing_or_404(listing_id: int) -> dict:
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Inserat nicht gefunden")
    return listing


def _draft_view(draft: dict) -> dict:
    kind, provider = draft.get("kind"), draft.get("provider") or settings.DEFAULT_PROVIDER
    return {
        **draft,
        "kind_label": DRAFT_KIND_LABELS.get(kind, kind or "?"),
        "model_label": settings.model_label(provider, draft.get("model")),
        "provider_label": settings.provider_label(provider),
        "cost_text": settings.draft_cost_detail(draft),  # "3,8 ct (≈ 0,042 $)" for Anthropic, else "2,1 ct"
    }


_FACT_LINE = re.compile(r"([^:\n]{1,60}): (\S.*)")


def details_sections(text: str | None) -> list[dict]:
    """The ImmoScout24 details text (is24.parse_expose: blocks with optional "## Title" lines) -> sections for
    the listing page: {"title", "facts": [(label, value)] or None, "text"}. A section whose lines all read
    "Label: value" becomes facts, anything else stays text. The trailing "Anbieter: …" joins the first facts."""
    sections = [{"title": None, "lines": []}]
    for line in (text or "").splitlines():
        if line.startswith("## ") and line[3:].strip():
            sections.append({"title": line[3:].strip(), "lines": []})
        else:
            sections[-1]["lines"].append(line)
    if len(sections) > 1:
        head, sep, tail = "\n".join(sections[-1]["lines"]).strip().rpartition("\n\n")
        if sep and tail.startswith("Anbieter: ") and "\n" not in tail:
            sections[-1]["lines"] = head.splitlines()
            sections[0]["lines"].append(tail)
    result = []
    for section in sections:
        body = "\n".join(section["lines"]).strip()
        if not body:
            continue
        matches = [_FACT_LINE.fullmatch(line.strip()) for line in body.splitlines() if line.strip()]
        if all(matches):
            result.append({"title": section["title"], "facts": [m.groups() for m in matches], "text": None})
        else:
            result.append({"title": section["title"], "facts": None, "text": body})
    return result


def _is_floorplan(picture: dict) -> bool:
    return bool(picture.get("floor_plan")) or any(word in (picture.get("caption") or "").lower()
                                                   for word in is24.FLOORPLAN_WORDS)


MEDIA_SOURCE_LABELS = {"fredy": "Bilder von Fredy", "is24": "von ImmoScout24 geladen"}


def _listing_context(listing: dict, error: str | None = None) -> dict:
    pictures = media.stored_pictures(listing["id"])
    source = media.source_of(listing)
    return {
        "active": "listings",
        "listing": listing,
        "gallery": source is not None or bool(pictures),
        "media_source": source or listing.get("media_source"),
        "media_source_label": MEDIA_SOURCE_LABELS.get(source or listing.get("media_source")),
        # Load/retry only where it can do something: a source, and nothing fetched yet or a failure.
        "can_load_media": source is not None and (not listing.get("media_fetched_at") or bool(listing.get("media_error"))),
        "media": pictures,
        # Big picture at the top: the first stored photo that isn't a floor plan (those come first).
        "hero": next((m for m in pictures if not _is_floorplan(m)), pictures[0] if pictures else None),
        "details": details_sections(listing.get("details")),
        "portal": portals.provider_label(listing["provider"]),
        # Only http(s) URLs become links/images; contact_url's output is checked as well.
        "image_url": portals.safe_url(listing["image_url"]),
        "contact_url": portals.safe_url(portals.contact_url(listing["provider"], listing["url"])),
        "fredy_url": portals.fredy_url(listing, db.get_settings()["fredy_base_url"]),
        "drafts": [_draft_view(d) for d in db.list_drafts(listing["id"])],
        "error": error,
    }


@router.get("/", dependencies=PAGE)
def listings_page(request: Request, status: str | None = None):
    if status not in settings.STATUSES:
        status = None
    thumbs = db.thumb_positions()  # listings with stored ImmoScout24 pictures
    listings = [{
        **row,
        "portal": portals.provider_label(row["provider"]),
        # Server-built link to a stored picture; otherwise the template falls back to Fredy's image_url (safe_url).
        "thumb_url": f"/listing/{row['id']}/media/{thumbs[row['id']]}" if row["id"] in thumbs else None,
    } for row in db.list_listings(status)]
    return render(request, "listings.html", {
        "active": "listings",
        "listings": listings,
        "stats": db.stats(),
        "status_filter": status,
    })


@router.get("/listing/{listing_id}", dependencies=PAGE)
def listing_page(request: Request, listing_id: int):
    return render(request, "listing.html", _listing_context(_listing_or_404(listing_id)))


@router.get("/listing/{listing_id}/media/{position}", dependencies=PAGE)
def listing_media(listing_id: int, position: int):
    """A stored listing picture. Only files recorded in listing_media and lying inside
    DATA_DIR/media/<listing id>/ are served; the browser may cache them privately for a day."""
    path = media.media_file(listing_id, position) if listing_id > 0 and position > 0 else None
    if path is None:
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/listing/{listing_id}/media", dependencies=ACTION)
async def listing_load_media(listing_id: int):
    """"Bilder laden" (also retries after a failure). Takes a few seconds: the pictures one at a time (plus one
    exposé request for ImmoScout24). Does nothing without a source (no Fredy pictures, own fetch off)."""
    _listing_or_404(listing_id)
    await media.ensure_media(listing_id, retry=True)
    return RedirectResponse(f"/listing/{listing_id}#bilder", status_code=303)


@router.post("/listing/{listing_id}/status", dependencies=ACTION)
def listing_set_status(listing_id: int, status: str = Form("")):
    _listing_or_404(listing_id)
    if status not in settings.STATUSES:
        raise HTTPException(status_code=400, detail="Ungültiger Status")
    db.set_listing_status(listing_id, status)
    return RedirectResponse(f"/listing/{listing_id}", status_code=303)


@router.post("/listing/{listing_id}/draft", dependencies=ACTION)
async def listing_create_draft(request: Request, listing_id: int):
    _listing_or_404(listing_id)
    kind = "regenerate" if db.latest_draft(listing_id) else "initial"
    try:
        draft = await llm.create_and_store_draft(listing_id, kind)
    except Exception as exc:
        message = str(exc) if isinstance(exc, llm.LLMError) else GENERIC_DRAFT_ERROR
        if not isinstance(exc, llm.LLMError):
            log.exception("Draft generation failed for listing %s", listing_id)
        context = _listing_context(_listing_or_404(listing_id), error=redact(request, message))
        return render(request, "listing.html", context)
    anchor = f"d{draft['id']}" if isinstance(draft, dict) and draft.get("id") else "entwuerfe"
    return RedirectResponse(f"/listing/{listing_id}#{anchor}", status_code=303)


# --- settings -------------------------------------------------------------------

def validate_settings(form: dict[str, str], current: dict[str, str],
                      section: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    """Form fields -> (values to store, German error messages per field). Unknown keys are ignored.

    Fields missing from the form keep their stored value (`current`); absent checkboxes mean "off" ("0") -
    only those of `section` (the sub-page the form belongs to), or all of them without a section.
    """
    values: dict[str, str] = {}
    errors: dict[str, str] = {}

    for key in TEXT_SETTINGS:
        if key in form:
            values[key] = form[key].replace("\r\n", "\n")
            if len(values[key]) > MAX_TEXT_CHARS:
                errors[key] = f"Höchstens {MAX_TEXT_CHARS} Zeichen."
    if "system_prompt" in values and not values["system_prompt"].strip():
        errors["system_prompt"] = "Darf nicht leer sein (oder auf Standard zurücksetzen)."

    for key, low, high in [("target_words", 30, 600), ("followup_delay_s", 0, 60), ("llm_max_images", 0, 20)]:
        if key in form:
            raw = form[key].strip()
            try:
                number = int(raw)
            except ValueError:
                number = None
            if number is None or not low <= number <= high:
                errors[key] = f"Ganze Zahl von {low} bis {high}."
            values[key] = str(number) if number is not None else raw

    provider = form.get("provider", settings.provider_of(current))
    if "provider" in form:
        values["provider"] = provider
        if provider not in settings.PROVIDERS:
            errors["provider"] = "Unbekannter Anbieter."
    if provider in settings.PROVIDERS:
        # Each provider panel on /settings has its own model select ("model_<provider>").
        model = form.get(f"model_{provider}", form.get("model", current.get("model")))
        if model in settings.PRICES[provider]:
            values["model"] = model
        elif provider != settings.provider_of(current):  # switched provider: its default model
            values["model"] = settings.PROVIDERS[provider]["default_model"]
        elif "model" in form or f"model_{provider}" in form:
            values["model"] = model
            errors["model"] = "Unbekanntes Modell."
    if "effort" in form:
        values["effort"] = form["effort"]
        if form["effort"] not in settings.EFFORTS:
            errors["effort"] = "Unbekannter Effort-Wert."

    if "monthly_budget_eur" in form:
        raw = form["monthly_budget_eur"].strip()
        try:
            budget = float(raw.replace(",", "."))
        except ValueError:
            budget = None
        if budget is None or not math.isfinite(budget) or budget < 0:
            errors["monthly_budget_eur"] = "Zahl ≥ 0 (0 = kein Limit)."
            values["monthly_budget_eur"] = raw
        else:
            values["monthly_budget_eur"] = f"{budget:.10g}"

    if "eur_usd_rate" in form:
        raw = form["eur_usd_rate"].strip()
        try:
            rate = float(raw.replace(",", "."))
        except ValueError:
            rate = None
        if rate is None or not math.isfinite(rate) or not 0.1 <= rate <= 10:
            errors["eur_usd_rate"] = "Zahl zwischen 0,1 und 10, z. B. 1,10."
            values["eur_usd_rate"] = raw
        else:
            values["eur_usd_rate"] = f"{rate:.10g}"

    if "fredy_base_url" in form:  # empty: the links Fredy sent are used
        values["fredy_base_url"] = form["fredy_base_url"].strip().rstrip("/")
        error = _fredy_base_url_error(values["fredy_base_url"]) if values["fredy_base_url"] else None
        if error:
            errors["fredy_base_url"] = error

    for key in CHECKBOX_SETTINGS:
        if section is None or key in SECTION_FIELDS[section]:
            values[key] = "1" if form.get(key) else "0"
    return values, errors


def _fredy_base_url_error(url: str) -> str | None:
    """Why `url` can't be the address of the Fredy instance, or None. Wanted: http(s)://host[:port][/path]."""
    if len(url) > FREDY_BASE_URL_MAX:
        return f"Höchstens {FREDY_BASE_URL_MAX} Zeichen."
    try:
        with_login = "@" in urlsplit(url).netloc
    except ValueError:  # e.g. an unclosed [IPv6 bracket; safe_url rejects it below
        with_login = False
    if with_login:
        return "Ohne Benutzername und Passwort."
    if portals.safe_url(url) is None:
        return "Volle Adresse mit http(s)://, z. B. https://fredy.example.com."
    if "?" in url or "#" in url:  # also a bare "?" or "#", which urlsplit reports as empty
        return "Nur die Adresse von Fredy, ohne „?…“ oder „#…“."
    return None


def _ai_status(current: dict) -> dict:
    """The selected AI provider, its key status (hint only, never the value) and a warning without a key."""
    provider = settings.provider_of(current)
    info = settings.PROVIDERS[provider]
    key = vault.credential_status(info["credential"])
    return {
        "provider": provider,
        "label": info["label"],
        "currency": info["currency"],
        "model_label": settings.model_label(provider, settings.model_of(current)),
        "key": key,
        "no_key_warning": (f"Kein {info['label']}-API-Key hinterlegt – Entwürfe schlagen fehl. "
                           "Unter Einstellungen → KI & Kosten eintragen.") if key["source"] is None else None,
    }


def _model_choices(request: Request, provider: str, model: str) -> dict:
    """Models to offer for a provider: those in its live list (cached) that also have a price. Without a usable
    live list, every priced model, marked as unchecked. `model` is preselected (and always offered)."""
    prices = settings.PRICES[provider]
    sign = settings.CURRENCY_SIGNS[settings.PROVIDERS[provider]["currency"]]
    cache = llm.cached_models(provider)
    checked = bool(cache) and not cache.get("error")
    live = cache["models"] if checked else []
    ids = [m for m in prices if m in live] if checked else list(prices)
    if model not in ids:
        ids.append(model)  # keep the saved choice selectable
    return {
        "sign": sign,
        "model_options": [  # short ("Opus 5.5 – 4,00 $ / 20,00 $") so the price isn't cut off on phones
            (m, f"{prices[m]['label'].removeprefix('Claude ')} – {_price(prices[m]['input'])} {sign} / "
                f"{_price(prices[m]['output'])} {sign}")
            for m in ids
        ],
        "current_model": model,
        "models_checked": checked,
        "models_fetched_at": cache.get("fetched_at") if cache else None,
        "models_error": redact(request, cache["error"]) if cache and cache.get("error") else None,
        "unsupported_models": [m for m in live if m not in prices],
    }


def _provider_panels(request: Request, current: dict) -> list[dict]:
    """Everything "KI & Kosten" shows per provider (key status + hint, models, warning). Both panels are
    rendered; app.js shows the one chosen in the select (without JS both stay visible)."""
    saved_provider, saved_model = settings.provider_of(current), settings.model_of(current)
    panels = []
    for provider, info in settings.PROVIDERS.items():
        model = saved_model if provider == saved_provider else info["default_model"]
        panels.append({**_ai_status({**current, "provider": provider, "model": model}),
                       **_model_choices(request, provider, model)})
    return panels


def _credential_need(name: str, provider: str) -> str:
    """"required", "optional", or "unused" for the API key of the provider that isn't selected."""
    if name in OPTIONAL_CREDENTIALS:
        return "optional"
    if name in AI_CREDENTIALS and AI_CREDENTIALS[name] != provider:
        return "unused"
    return "required"


def _settings_alerts(current: dict) -> dict[str, bool]:
    """Per sub-page: is a required credential missing or unreadable? Marked with a dot in the sub-navigation."""
    provider = settings.provider_of(current)
    alerts = {section: False for section in SETTINGS_SECTIONS}
    for c in vault.status():
        if c["undecryptable"] or (c["source"] is None and _credential_need(c["name"], provider) == "required"):
            alerts["ki" if c["name"] in AI_CREDENTIALS else "telegram"] = True
    return alerts


def _render_section(request: Request, section: str, *, values: dict | None = None, errors=None, message=None,
                    error=None, show: str | None = None, status_code=200, **extra):
    """One of the three "Einstellungen" pages. `values`: what its form shows (the stored settings, or the
    user's input after a validation error). `show`: provider to preselect on "KI & Kosten" (e.g. after
    saving its key); the saved one otherwise. Never gets a secret value."""
    current = db.get_settings()
    context = {
        "active": "settings",
        "section": section,
        "sections": SETTINGS_SECTIONS,
        "alerts": _settings_alerts(current),
        "values": values or current,
        "errors": errors or {},
        "message": message,
        "error": error,
        **extra,
    }
    if section == "ki":
        saved_provider = settings.provider_of(current)
        context.update(
            panels=_provider_panels(request, current),
            saved_provider=saved_provider,
            shown_provider=show if show in settings.PROVIDERS else saved_provider,
            effort_options=[(e, EFFORT_LABELS.get(e, e)) for e in settings.EFFORTS],
        )
    elif section == "telegram":
        context.update(_telegram_context(request, current))
    return render(request, SETTINGS_SECTIONS[section]["template"], context, status_code=status_code)


SETTINGS_MESSAGES = {
    "saved": "Gespeichert.",
    "key": "API-Key gespeichert.",
    "models": "Modellliste neu geladen.",
}


@router.get("/settings", dependencies=PAGE)
def settings_page(request: Request, saved: str | None = None, reset: str | None = None):
    message = None
    if saved:
        message = SETTINGS_MESSAGES["saved"]
    elif reset in TEXT_SETTING_LABELS:
        message = f"„{TEXT_SETTING_LABELS[reset]}“ auf Standard zurückgesetzt."
    return _render_section(request, "texte", message=message)


@router.get("/settings/ki", dependencies=PAGE)
def settings_ki_page(request: Request, saved: str | None = None, cleared: str | None = None, show: str | None = None):
    message = None
    if saved in AI_CREDENTIALS:
        message = f"„{vault.LABELS[saved]}“ gespeichert."
    elif saved:
        message = SETTINGS_MESSAGES.get(saved, SETTINGS_MESSAGES["saved"])
    elif cleared in AI_CREDENTIALS:
        message = f"„{vault.LABELS[cleared]}“ entfernt."
    return _render_section(request, "ki", message=message, show=show)


async def _refresh_models_quietly(provider: str) -> None:
    """Fetch the live model list; a failure is cached by llm.refresh_models and shown on /settings."""
    try:
        await llm.refresh_models(provider)
    except llm.LLMError:
        pass


def _form_provider(provider: str) -> str:
    if provider not in settings.PROVIDERS:
        raise HTTPException(status_code=400, detail="Unbekannter Anbieter")
    return provider


@router.post("/settings/provider-key", dependencies=ACTION)
async def settings_provider_key(request: Request, provider: str = Form(""), value: str = Form(""),
                                password: str = Form("")):
    """Save a provider's API key from its panel (same rules as on /credentials), then load its model list.
    Doesn't switch the provider: that is saved with the settings form."""
    provider = _form_provider(provider)
    name = settings.PROVIDERS[provider]["credential"]
    error_page = _error_page_for(name)
    denied = await _step_up(request, password, error_page)
    if denied is not None:
        return denied
    try:
        await _change(request, name, "set", lambda: vault.set(name, value))
    except vault.InvalidValue as exc:  # the message never contains the value
        return error_page(request, error=f"{vault.LABELS[name]}: {exc}", status_code=400)
    await _refresh_models_quietly(provider)
    return RedirectResponse(f"/settings/ki?saved=key&show={provider}", status_code=303)


@router.post("/settings/models/refresh", dependencies=ACTION)
async def settings_models_refresh(provider: str = Form("")):
    provider = _form_provider(provider)
    await _refresh_models_quietly(provider)
    return RedirectResponse(f"/settings/ki?saved=models&show={provider}", status_code=303)


@router.post("/settings", dependencies=ACTION)
async def settings_save(request: Request):
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    section = form.get("section") if form.get("section") in SETTINGS_SECTIONS else None
    current = db.get_settings()
    values, errors = validate_settings(form, current, section)
    if errors:
        shown = {**current, **values}  # keep the user's input in the form
        page = section or FIELD_SECTION[next(iter(errors))]  # without a section: the page of the first bad field
        return _render_section(request, page, values=shown, errors=errors, error="Nicht gespeichert, bitte Eingaben prüfen.",
                               show=values.get("provider"), status_code=400)
    db.update_settings(values)
    provider = values.get("provider")
    if (provider and provider != settings.provider_of(current) and llm.cached_models(provider) is None
            and vault.get(settings.PROVIDERS[provider]["credential"])):
        await _refresh_models_quietly(provider)  # first switch to a provider: check which models its key offers
    return RedirectResponse(SETTINGS_SECTIONS[section or "texte"]["path"] + "?saved=1", status_code=303)


@router.post("/settings/reset/{key}", dependencies=ACTION)
def settings_reset(key: str):
    if key not in TEXT_SETTINGS:
        raise HTTPException(status_code=404, detail="Unbekannte Einstellung")
    db.reset_setting(key)
    return RedirectResponse(f"/settings?reset={key}", status_code=303)


@router.post("/settings/test", dependencies=ACTION)
async def settings_test(request: Request):
    listing = db.latest_listing()
    if listing is None:
        return _render_section(request, "texte", error="Noch kein Inserat vorhanden – der Test braucht mindestens eins.")
    try:
        draft = await llm.create_and_store_draft(listing["id"], "test")
    except Exception as exc:
        message = str(exc) if isinstance(exc, llm.LLMError) else GENERIC_DRAFT_ERROR
        if not isinstance(exc, llm.LLMError):
            log.exception("Test draft failed")
        return _render_section(request, "texte", error=redact(request, message))
    return _render_section(request, "texte", test={"listing": listing, "draft": _draft_view(draft)})


# --- credentials (API keys on "KI & Kosten", the rest on "Telegram & Fredy") ---------------

CREDENTIAL_HELP = {  # the AI keys are explained on "KI & Kosten" itself
    "telegram_bot_token": "Derselbe Token wie in Fredys Telegram-Adapter (von @BotFather).",
    "telegram_chat_id": "Dieselbe Chat-ID wie in Fredys Telegram-Adapter – oder per Code verbinden (Bot muss laufen).",
    "telegram_allowed_user_ids": ("Wer im Chat Buttons drücken darf, kommagetrennt. Leer = jedes Mitglied des Chats. "
                                  "„Chat per Code verbinden“ trägt dich automatisch ein."),
    "fredy_webhook_token": "Mindestens 32 Zeichen. Am einfachsten „Neu erzeugen“ und in Fredys HTTP-Adapter eintragen.",
}
# test -> credential named in the audit log when it fails
CONNECTION_TESTS = {"anthropic": "anthropic_api_key", "deutschlandgpt": "deutschlandgpt_api_key", "telegram": "telegram_bot_token"}
EVENT_NAME_LABELS = {**vault.LABELS, "dashboard": "Dashboard-Login"}
OPEN_CHAT_NOTE = "Optional, nicht gesetzt: jedes Mitglied des Chats darf Buttons drücken."
LOCKED_CHAT_WARNING = ("Die Liste erlaubter Telegram-User ist nicht entschlüsselbar (SESSION_SECRET geändert?) – "
                       "niemand darf Buttons drücken. Pairing erneut ausführen oder User-ID neu eintragen.")
ONE_TIME_TTL_S = 300
_one_time: dict[str, tuple[float, str]] = {}  # one-time id -> (monotonic expiry, value); server memory only


def _allowlist_warning() -> str | None:
    """Only an unreadable list is a problem; an empty one is allowed (the setting is optional)."""
    return LOCKED_CHAT_WARNING if bot.allowed_user_ids() is None else None


def _stash_once(value: str) -> str:
    """Keep a value for one display after a redirect. Returns a random id; the value never leaves server memory
    except on that one page (not in the URL, not in the cookie)."""
    now = time.monotonic()
    for key in [k for k, (expires, _) in _one_time.items() if expires <= now]:
        del _one_time[key]
    ref = secrets.token_urlsafe(16)
    _one_time[ref] = (now + ONE_TIME_TTL_S, value)
    return ref


def _take_once(ref: str | None) -> str | None:
    """The stashed value, removed on first use; None if unknown or expired."""
    expires, value = _one_time.pop(ref or "", (0.0, None))
    return value if expires > time.monotonic() else None


def _credential_events() -> list[dict]:
    return [
        {**e, "label": EVENT_NAME_LABELS.get(e["name"], e["name"]), "action_label": vault.ACTION_LABELS.get(e["action"], e["action"])}
        for e in db.list_credential_events(20)
    ]


def _hook_url(request: Request) -> str:
    return f"https://{request.headers.get('host', 'immobot.<domain>')}/hook/fredy"


def _telegram_context(request: Request, current: dict) -> dict:
    """What "Telegram & Fredy" shows besides its form: credential status (never values), pairing, hook URL."""
    provider = settings.provider_of(current)
    return {
        "credentials": [{**c, "help": CREDENTIAL_HELP[c["name"]], "need": _credential_need(c["name"], provider)}
                        for c in vault.status() if c["name"] not in AI_CREDENTIALS],
        "chat_id": vault.get("telegram_chat_id"),  # not secret
        "allowed_user_ids": vault.get("telegram_allowed_user_ids"),  # not secret
        "allowlist_warning": _allowlist_warning(),
        "pairing": vault.pairing_info(),
        "last_pairing": db.last_credential_event("telegram_chat_id", "pair"),
        "bot_disabled": request.app.state.config.disable_bot,
        "secret_names": vault.SECRET_NAMES,
        "public_url": bot.public_url_info(),
        "hook_url": _hook_url(request),
    }


def _render_telegram(request: Request, **kwargs):
    """`new_token` (keyword) is the one-time display after "Neu erzeugen"; nothing else is ever a secret."""
    return _render_section(request, "telegram", **kwargs)


def _error_page_for(name: str):
    """Error pages of a credential change: the page the credential is managed on."""
    if name in AI_CREDENTIALS:
        return lambda request, **kwargs: _render_section(request, "ki", show=AI_CREDENTIALS[name], **kwargs)
    return _render_telegram


def _credential_page(name: str) -> str:
    return f"/settings/ki?show={AI_CREDENTIALS[name]}&" if name in AI_CREDENTIALS else "/settings/telegram?"


async def _step_up(request: Request, password: str, error_page=None):
    """Re-check the dashboard password before a credential change. Returns an error page (by default
    "Telegram & Fredy"), or None if ok. Wrong passwords count toward the login limit; once it is reached
    every attempt is refused."""
    error_page = error_page or _render_telegram
    client = _client_ip(request)
    if _too_many_failures(client):
        return error_page(request, error="Zu viele Fehlversuche – bitte ein paar Minuten warten.", status_code=429)
    if not _password_ok(request, password):
        log.warning("Wrong dashboard password for a credential change from %s", client)
        await _count_failure(client)
        return error_page(request, error="Dashboard-Passwort falsch – nichts geändert.", status_code=403)
    return None


async def _change(request: Request, name: str, action: str, apply):
    """Run apply() (the change itself; may raise vault.InvalidValue), then write the audit entry, send the
    Telegram notice and let the bot pick up a new token or chat. Returns apply()'s result.

    The notice goes to the chat configured *before* the change, sent by the bot running before it, so a
    replaced chat id or bot token is still reported to the owner's chat."""
    old_chat = bot.configured_chat_id()
    result = apply()
    client = _client_ip(request)
    db.add_credential_event(name, action, client)
    log.info("Credential %s: %s (from %s)", name, action, client)
    await bot.notify(bot.change_notice(name, action, "Dashboard"), old_chat)
    if action in ("set", "clear") and not request.app.state.config.disable_bot:
        if name == "telegram_bot_token":
            bot.schedule_reload()  # in the background: stopping waits for running handlers
        elif name == "telegram_chat_id":  # read fresh on every use, no restart needed
            bot.schedule_announce(db.unannounced_listing_ids())
    return result


def _known_credential(name: str) -> str:
    if name not in vault.LABELS:
        raise HTTPException(status_code=404, detail="Unbekannter Zugang")
    return name


@router.get("/settings/telegram", dependencies=PAGE)
def settings_telegram_page(request: Request, saved: str | None = None, cleared: str | None = None,
                           token: str | None = None):
    message = error = new_token = None
    if saved in vault.LABELS:
        message = f"„{vault.LABELS[saved]}“ gespeichert."
    elif saved:
        message = SETTINGS_MESSAGES["saved"]
    elif cleared in vault.LABELS:
        message = f"„{vault.LABELS[cleared]}“ entfernt."
    elif token is not None:
        new_token = _take_once(token)
        if new_token is None:
            error = "Der neue Webhook-Token wurde schon angezeigt (oder die 5 Minuten sind um). Bei Bedarf neu erzeugen."
    return _render_telegram(request, message=message, error=error, new_token=new_token)


@router.get("/credentials", dependencies=PAGE)
def credentials_moved(request: Request):
    """The former "Zugänge" page, now part of "Einstellungen" (old bookmarks)."""
    return RedirectResponse("/settings/telegram", status_code=303)


@router.post("/credentials/save/{name}", dependencies=ACTION)
async def credentials_save(request: Request, name: str, value: str = Form(""), password: str = Form("")):
    name = _known_credential(name)
    error_page = _error_page_for(name)
    denied = await _step_up(request, password, error_page)
    if denied is not None:
        return denied
    try:
        await _change(request, name, "set", lambda: vault.set(name, value))
    except vault.InvalidValue as exc:  # the message never contains the value
        return error_page(request, error=f"{vault.LABELS[name]}: {exc}", status_code=400)
    return RedirectResponse(f"{_credential_page(name)}saved={name}", status_code=303)


@router.post("/credentials/clear/{name}", dependencies=ACTION)
async def credentials_clear(request: Request, name: str, password: str = Form("")):
    name = _known_credential(name)
    denied = await _step_up(request, password, _error_page_for(name))
    if denied is not None:
        return denied
    await _change(request, name, "clear", lambda: vault.clear(name))
    return RedirectResponse(f"{_credential_page(name)}cleared={name}", status_code=303)


@router.post("/credentials/regenerate-webhook-token", dependencies=ACTION)
async def credentials_regenerate_webhook_token(request: Request, password: str = Form("")):
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    token = secrets.token_urlsafe(32)
    await _change(request, "fredy_webhook_token", "regenerate", lambda: vault.set("fredy_webhook_token", token))
    # Post/redirect/get, so F5 or "back" can't rotate the token again. The page after the redirect shows it
    # exactly once; until then it only exists in server memory under a random one-time id.
    return RedirectResponse(f"/settings/telegram?token={_stash_once(token)}", status_code=303)


@router.post("/credentials/pair", dependencies=ACTION)
async def credentials_pair(request: Request, password: str = Form("")):
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    bot_username = None if request.app.state.config.disable_bot else bot.username()
    if not bot_username:
        return _render_telegram(request, error="Der Telegram-Bot läuft nicht – zuerst einen gültigen Bot-Token speichern.",
                                status_code=409)
    code = await _change(request, "telegram_chat_id", "pair_start", lambda: vault.start_pairing(bot_username))
    return _render_telegram(request, pairing_code=code)


@router.post("/credentials/test/{target}", dependencies=ACTION)
async def credentials_test(request: Request, target: str):
    if target not in CONNECTION_TESTS:
        raise HTTPException(status_code=404, detail="Unbekannter Test")
    try:
        if target in settings.PROVIDERS:
            text = await llm.check_api_key(target)
        else:
            text = await bot.check_connection()
        result = {"ok": True, "text": text}
    except (llm.LLMError, bot.CheckFailed) as exc:
        result = {"ok": False, "text": str(exc)}
    except Exception as exc:
        log.warning("Connection test %s failed unexpectedly: %s", target, type(exc).__name__)  # type only: may hold a token
        result = {"ok": False, "text": "Unerwarteter Fehler beim Test – Details im Log."}
    if not result["ok"]:
        db.add_credential_event(CONNECTION_TESTS[target], "test_failed", _client_ip(request))
    result["text"] = redact(request, result["text"])
    if target in settings.PROVIDERS:
        return _render_section(request, "ki", test_result=result, show=target)
    return _render_telegram(request, test_result=result)


# --- status ---------------------------------------------------------------------

@router.get("/status", dependencies=PAGE)
def status_page(request: Request):
    config = request.app.state.config
    bot_status = bot.status() or {}
    last_error = bot_status.get("last_error")
    current = db.get_settings()
    provider = settings.provider_of(current)
    return render(request, "status.html", {
        "active": "status",
        "last_webhook_at": db.get_meta("last_webhook_at"),
        "bot_disabled": config.disable_bot,
        "bot_configured": vault.get("telegram_bot_token") is not None,
        "bot_running": bool(bot_status.get("running")),
        "chat_connected": bot.configured_chat_id() is not None,
        "allowed_user_ids": sorted(bot.allowed_user_ids() or ()),  # not secret
        "allowlist_warning": _allowlist_warning(),
        "open_chat_note": OPEN_CHAT_NOTE,
        "bot_last_update_at": bot_status.get("last_update_at"),
        "bot_last_error": redact(request, str(last_error)) if last_error else None,
        # Names, sources and dates only - never any part of a secret.
        "credentials": [{**c, "need": _credential_need(c["name"], provider),
                         "page": "/settings/ki" if c["name"] in AI_CREDENTIALS else "/settings/telegram"}
                        for c in vault.status()],
        "ai": _ai_status(current),
        "media_last_error": db.get_meta("media_last_error"),
        "fredy_images": {"last_webhook": db.get_meta("last_webhook_had_images"),
                         "last_at": db.get_meta("fredy_images_last_at")},
        "events": _credential_events(),
        "version": settings.APP_VERSION,
        "stats": db.stats(),
        "hook_url": _hook_url(request),
    })
