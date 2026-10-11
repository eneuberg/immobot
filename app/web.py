"""Dashboard (login, listings, drafts, settings, credentials, status) and the Fredy webhook.

Security model (see PLAN.md section 3):
- Every page needs a logged-in session, except /healthz, /login and /hook/fredy (Bearer token).
- Secret values are never passed to templates: /credentials and /status only show whether and where a
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

from . import bot, db, is24, llm, portals, settings, vault

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
CHECKBOX_SETTINGS = ["send_image", "paused"]
TEXT_SETTING_LABELS = {"system_prompt": "System-Prompt", "profile": "Über mich", "wishes": "Wünsche", "style": "Schreibstil"}
MAX_TEXT_CHARS = 20_000

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


templates.env.filters["usd"] = settings.fmt_usd
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


# --- password checks (login form and step-up on /credentials share one per-IP limit) ----

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

    received, new_ids = 0, []
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

    log.info("Fredy webhook: %d listings, %d new", received, len(new_ids))
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
        "cost_text": settings.draft_cost_text(draft),  # "2,1 ct (€) ≈ 2,3 ct ($)"
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


def _is_floorplan(media: dict) -> bool:
    return any(word in (media.get("caption") or "").lower() for word in is24.FLOORPLAN_WORDS)


def _listing_context(listing: dict, error: str | None = None) -> dict:
    media = db.list_media(listing["id"])
    return {
        "active": "listings",
        "listing": listing,
        "is24": is24.is_supported(listing),
        "media": media,
        # Big picture at the top: the first stored photo that isn't a floor plan (those come first).
        "hero": next((m for m in media if not _is_floorplan(m)), media[0] if media else None),
        "details": details_sections(listing.get("details")),
        "portal": portals.provider_label(listing["provider"]),
        # Only http(s) URLs become links/images; contact_url's output is checked as well.
        "image_url": portals.safe_url(listing["image_url"]),
        "contact_url": portals.safe_url(portals.contact_url(listing["provider"], listing["url"])),
        "fredy_url": portals.safe_url(listing["fredy_url"]),
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
    """A stored ImmoScout24 picture. Only files recorded in listing_media and lying inside
    DATA_DIR/media/<listing id>/ are served; the browser may cache them privately for a day."""
    path = is24.media_file(listing_id, position) if listing_id > 0 and position > 0 else None
    if path is None:
        raise HTTPException(status_code=404, detail="Bild nicht gefunden")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@router.post("/listing/{listing_id}/media", dependencies=ACTION)
async def listing_load_media(listing_id: int):
    """"Bilder & Details laden" (also retries after a failure). Takes a few seconds: one exposé request,
    then the pictures one at a time."""
    _listing_or_404(listing_id)
    await is24.ensure_media(listing_id, retry=True)
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

def validate_settings(form: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Form fields -> (values to store, German error messages per field). Unknown keys are ignored.

    Fields missing from the form keep their stored value (`current`); absent checkboxes mean "off" ("0").
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
        model = form.get("model", current.get("model"))
        if model in settings.PRICES[provider]:
            values["model"] = model
        elif provider != settings.provider_of(current):  # switched provider: its default model
            values["model"] = settings.PROVIDERS[provider]["default_model"]
        elif "model" in form:
            values["model"] = form["model"]
            errors["model"] = "Unbekanntes Modell."
    if "effort" in form:
        values["effort"] = form["effort"]
        if form["effort"] not in settings.EFFORTS:
            errors["effort"] = "Unbekannter Effort-Wert."

    if "monthly_budget_usd" in form:
        raw = form["monthly_budget_usd"].strip()
        try:
            budget = float(raw.replace(",", "."))
        except ValueError:
            budget = None
        if budget is None or not math.isfinite(budget) or budget < 0:
            errors["monthly_budget_usd"] = "Zahl ≥ 0 (0 = kein Limit)."
            values["monthly_budget_usd"] = raw
        else:
            values["monthly_budget_usd"] = f"{budget:.10g}"

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

    for key in CHECKBOX_SETTINGS:
        values[key] = "1" if form.get(key) else "0"
    return values, errors


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
                           "Unter Einstellungen → KI-Anbieter eintragen.") if key["source"] is None else None,
    }


def _model_choices(request: Request, current: dict) -> dict:
    """Models to offer: those in the provider's live list (cached) that also have a price. Without a usable
    live list, every priced model, marked as unchecked."""
    provider, model = settings.provider_of(current), settings.model_of(current)
    prices = settings.PRICES[provider]
    sign = settings.CURRENCY_SIGNS[settings.PROVIDERS[provider]["currency"]]
    cache = llm.cached_models(provider)
    checked = bool(cache) and not cache.get("error")
    live = cache["models"] if checked else []
    ids = [m for m in prices if m in live] if checked else list(prices)
    if model not in ids:
        ids.append(model)  # keep the saved choice selectable
    return {
        "model_options": [
            (m, f"{prices[m]['label']} – {_price(prices[m]['input'])} {sign} / {_price(prices[m]['output'])} {sign} "
                "pro 1 Mio. Tokens (Input/Output)")
            for m in ids
        ],
        "current_model": model,
        "models_checked": checked,
        "models_fetched_at": cache.get("fetched_at") if cache else None,
        "models_error": redact(request, cache["error"]) if cache and cache.get("error") else None,
        "unsupported_models": [m for m in live if m not in prices],
    }


def _render_settings(request: Request, values: dict, *, errors=None, message=None, error=None, test=None, status_code=200):
    current = db.get_settings()
    return render(request, "settings.html", {
        "active": "settings",
        "values": values,
        "errors": errors or {},
        "message": message,
        "error": error,
        "test": test,
        "ai": {**_ai_status(current), **_model_choices(request, current)},
        "providers": [(key, p["label"]) for key, p in settings.PROVIDERS.items()],
        "public_url": bot.public_url_info(),
        "effort_options": [(e, EFFORT_LABELS.get(e, e)) for e in settings.EFFORTS],
    }, status_code=status_code)


SETTINGS_MESSAGES = {
    "saved": "Gespeichert.",
    "provider": "KI-Anbieter gewechselt.",
    "key": "API-Key gespeichert.",
    "models": "Modellliste neu geladen.",
}


@router.get("/settings", dependencies=PAGE)
def settings_page(request: Request, saved: str | None = None, reset: str | None = None):
    message = None
    if saved:
        message = SETTINGS_MESSAGES.get(saved, SETTINGS_MESSAGES["saved"])
    elif reset in TEXT_SETTING_LABELS:
        message = f"„{TEXT_SETTING_LABELS[reset]}“ auf Standard zurückgesetzt."
    return _render_settings(request, db.get_settings(), message=message)


async def _refresh_models_quietly(provider: str) -> None:
    """Fetch the live model list; a failure is cached by llm.refresh_models and shown on /settings."""
    try:
        await llm.refresh_models(provider)
    except llm.LLMError:
        pass


@router.post("/settings/provider", dependencies=ACTION)
async def settings_provider(provider: str = Form("")):
    """Switch the AI provider. Keeps the model if the new provider prices it, else uses its default model."""
    if provider not in settings.PROVIDERS:
        raise HTTPException(status_code=400, detail="Unbekannter Anbieter")
    current = db.get_settings()
    values = {"provider": provider}
    if current.get("model") not in settings.PRICES[provider]:
        values["model"] = settings.PROVIDERS[provider]["default_model"]
    db.update_settings(values)
    if vault.get(settings.PROVIDERS[provider]["credential"]) and llm.cached_models(provider) is None:
        await _refresh_models_quietly(provider)
    return RedirectResponse("/settings?saved=provider#ki", status_code=303)


@router.post("/settings/provider-key", dependencies=ACTION)
async def settings_provider_key(request: Request, value: str = Form(""), password: str = Form("")):
    """Save the selected provider's API key (same rules as on /credentials), then load its model list."""
    provider = settings.provider_of(db.get_settings())
    name = settings.PROVIDERS[provider]["credential"]

    def error_page(request: Request, *, error: str, status_code: int):
        return _render_settings(request, db.get_settings(), error=error, status_code=status_code)

    denied = await _step_up(request, password, error_page)
    if denied is not None:
        return denied
    try:
        await _change(request, name, "set", lambda: vault.set(name, value))
    except vault.InvalidValue as exc:  # the message never contains the value
        return error_page(request, error=f"{vault.LABELS[name]}: {exc}", status_code=400)
    await _refresh_models_quietly(provider)
    return RedirectResponse("/settings?saved=key#ki", status_code=303)


@router.post("/settings/models/refresh", dependencies=ACTION)
async def settings_models_refresh():
    await _refresh_models_quietly(settings.provider_of(db.get_settings()))
    return RedirectResponse("/settings?saved=models#ki", status_code=303)


@router.post("/settings", dependencies=ACTION)
async def settings_save(request: Request):
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    values, errors = validate_settings(form, db.get_settings())
    if errors:
        shown = {**db.get_settings(), **values}  # keep the user's input in the form
        return _render_settings(request, shown, errors=errors, error="Nicht gespeichert, bitte Eingaben prüfen.", status_code=400)
    db.update_settings(values)
    return RedirectResponse("/settings?saved=1", status_code=303)


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
        return _render_settings(request, db.get_settings(), error="Noch kein Inserat vorhanden – der Test braucht mindestens eins.")
    try:
        draft = await llm.create_and_store_draft(listing["id"], "test")
    except Exception as exc:
        message = str(exc) if isinstance(exc, llm.LLMError) else GENERIC_DRAFT_ERROR
        if not isinstance(exc, llm.LLMError):
            log.exception("Test draft failed")
        return _render_settings(request, db.get_settings(), error=redact(request, message))
    return _render_settings(request, db.get_settings(), test={"listing": listing, "draft": _draft_view(draft)})


# --- credentials ("Zugänge") -------------------------------------------------------

CREDENTIAL_HELP = {
    "anthropic_api_key": "Von console.anthropic.com → API Keys, beginnt mit „sk-ant-“.",
    "deutschlandgpt_api_key": "Aus dem DeutschlandGPT-Dashboard (API-Keys). Welcher Anbieter genutzt wird, steht unter Einstellungen.",
    "telegram_bot_token": "Derselbe Token wie in Fredys Telegram-Adapter (von @BotFather).",
    "telegram_chat_id": "Dieselbe Chat-ID wie in Fredys Telegram-Adapter – oder per Code verbinden (Bot muss laufen).",
    "telegram_allowed_user_ids": ("Wer im Chat Buttons drücken darf, kommagetrennt. Leer = jedes Mitglied des Chats. "
                                  "„Chat per Code verbinden“ trägt dich automatisch ein."),
    "fredy_webhook_token": "Mindestens 32 Zeichen. Am einfachsten „Neu erzeugen“ und in Fredys HTTP-Adapter eintragen.",
}
# test -> credential named in the audit log when it fails
CONNECTION_TESTS = {"anthropic": "anthropic_api_key", "deutschlandgpt": "deutschlandgpt_api_key", "telegram": "telegram_bot_token"}
EVENT_NAME_LABELS = {**vault.LABELS, "dashboard": "Dashboard-Login"}
OPEN_CHAT_WARNING = "Jedes Mitglied des Chats kann Buttons drücken – Pairing erneut ausführen oder User-ID eintragen."
LOCKED_CHAT_WARNING = ("Die Liste erlaubter Telegram-User ist nicht entschlüsselbar (SESSION_SECRET geändert?) – "
                       "niemand darf Buttons drücken. Pairing erneut ausführen oder User-ID neu eintragen.")
ONE_TIME_TTL_S = 300
_one_time: dict[str, tuple[float, str]] = {}  # one-time id -> (monotonic expiry, value); server memory only


def _allowlist_warning() -> str | None:
    allowed = bot.allowed_user_ids()
    if allowed is None:
        return LOCKED_CHAT_WARNING
    return None if allowed else OPEN_CHAT_WARNING


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


def _render_credentials(request: Request, *, message=None, error=None, new_token=None, pairing_code=None,
                        test_result=None, status_code=200):
    """The page never gets a secret value; `new_token` is the one-time display after "Neu erzeugen"."""
    return render(request, "credentials.html", {
        "active": "credentials",
        "credentials": [{**c, "help": CREDENTIAL_HELP[c["name"]]} for c in vault.status()],
        "chat_id": vault.get("telegram_chat_id"),  # not secret
        "allowed_user_ids": vault.get("telegram_allowed_user_ids"),  # not secret
        "allowlist_warning": _allowlist_warning(),
        "pairing": vault.pairing_info(),
        "last_pairing": db.last_credential_event("telegram_chat_id", "pair"),
        "events": _credential_events(),
        "bot_disabled": request.app.state.config.disable_bot,
        "secret_names": vault.SECRET_NAMES,
        "message": message,
        "error": error,
        "new_token": new_token,
        "pairing_code": pairing_code,
        "test_result": test_result,
    }, status_code=status_code)


async def _step_up(request: Request, password: str, error_page=None):
    """Re-check the dashboard password before a credential change. Returns an error page (by default the
    credentials page), or None if ok. Wrong passwords count toward the login limit; once it is reached
    every attempt is refused."""
    error_page = error_page or _render_credentials
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


@router.get("/credentials", dependencies=PAGE)
def credentials_page(request: Request, saved: str | None = None, cleared: str | None = None, token: str | None = None):
    message = error = new_token = None
    if saved in vault.LABELS:
        message = f"„{vault.LABELS[saved]}“ gespeichert."
    elif cleared in vault.LABELS:
        message = f"„{vault.LABELS[cleared]}“ entfernt."
    elif token is not None:
        new_token = _take_once(token)
        if new_token is None:
            error = "Der neue Webhook-Token wurde schon angezeigt (oder die 5 Minuten sind um). Bei Bedarf neu erzeugen."
    return _render_credentials(request, message=message, error=error, new_token=new_token)


@router.post("/credentials/save/{name}", dependencies=ACTION)
async def credentials_save(request: Request, name: str, value: str = Form(""), password: str = Form("")):
    name = _known_credential(name)
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    try:
        await _change(request, name, "set", lambda: vault.set(name, value))
    except vault.InvalidValue as exc:  # the message never contains the value
        return _render_credentials(request, error=f"{vault.LABELS[name]}: {exc}", status_code=400)
    return RedirectResponse(f"/credentials?saved={name}", status_code=303)


@router.post("/credentials/clear/{name}", dependencies=ACTION)
async def credentials_clear(request: Request, name: str, password: str = Form("")):
    name = _known_credential(name)
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    await _change(request, name, "clear", lambda: vault.clear(name))
    return RedirectResponse(f"/credentials?cleared={name}", status_code=303)


@router.post("/credentials/regenerate-webhook-token", dependencies=ACTION)
async def credentials_regenerate_webhook_token(request: Request, password: str = Form("")):
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    token = secrets.token_urlsafe(32)
    await _change(request, "fredy_webhook_token", "regenerate", lambda: vault.set("fredy_webhook_token", token))
    # Post/redirect/get, so F5 or "back" can't rotate the token again. The page after the redirect shows it
    # exactly once; until then it only exists in server memory under a random one-time id.
    return RedirectResponse(f"/credentials?token={_stash_once(token)}", status_code=303)


@router.post("/credentials/pair", dependencies=ACTION)
async def credentials_pair(request: Request, password: str = Form("")):
    denied = await _step_up(request, password)
    if denied is not None:
        return denied
    bot_username = None if request.app.state.config.disable_bot else bot.username()
    if not bot_username:
        return _render_credentials(request, error="Der Telegram-Bot läuft nicht – zuerst einen gültigen Bot-Token speichern.",
                                   status_code=409)
    code = await _change(request, "telegram_chat_id", "pair_start", lambda: vault.start_pairing(bot_username))
    return _render_credentials(request, pairing_code=code)


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
    return _render_credentials(request, test_result=result)


# --- status ---------------------------------------------------------------------

@router.get("/status", dependencies=PAGE)
def status_page(request: Request):
    config = request.app.state.config
    bot_status = bot.status() or {}
    last_error = bot_status.get("last_error")
    return render(request, "status.html", {
        "active": "status",
        "last_webhook_at": db.get_meta("last_webhook_at"),
        "bot_disabled": config.disable_bot,
        "bot_configured": vault.get("telegram_bot_token") is not None,
        "bot_running": bool(bot_status.get("running")),
        "chat_connected": bot.configured_chat_id() is not None,
        "allowed_user_ids": sorted(bot.allowed_user_ids() or ()),  # not secret
        "allowlist_warning": _allowlist_warning(),
        "bot_last_update_at": bot_status.get("last_update_at"),
        "bot_last_error": redact(request, str(last_error)) if last_error else None,
        # Names, sources and dates only - never any part of a secret.
        "credentials": vault.status(),
        "ai": _ai_status(db.get_settings()),
        "is24_last_error": db.get_meta("is24_last_error"),
        "events": _credential_events(),
        "version": settings.APP_VERSION,
        "stats": db.stats(),
        "hook_url": f"https://{request.headers.get('host', 'immobot.<domain>')}/hook/fredy",
    })
