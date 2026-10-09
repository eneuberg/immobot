"""Dashboard (login, listings, drafts, settings, status) and the Fredy webhook.

Security model (see PLAN.md section 3):
- Every page needs a logged-in session, except /healthz, /login and /hook/fredy (Bearer token).
- Secrets are never passed to templates; the status page only shows yes/no. Error texts shown on a page
  additionally go through redact().
- Listing data from Fredy is untrusted: it is rendered only through Jinja autoescape, and links/images
  only after portals.safe_url().
- CSRF: the session cookie is SameSite=Strict; POSTs with a foreign Origin header are rejected too.
"""

import asyncio
import hmac
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import bot, db, llm, portals, settings

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))  # autoescape is on for .html

try:
    LOCAL_TZ = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:  # container without tz database
    LOCAL_TZ = timezone.utc

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

LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_S = 300
LOGIN_FAIL_DELAY_S = 1.0
_login_failures: deque[float] = deque()  # monotonic timestamps of failed logins (global, single user)


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
templates.env.globals["STATUSES"] = settings.STATUSES


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    return templates.TemplateResponse(request, name, context or {}, status_code=status_code)


def redact(request: Request, text: str) -> str:
    """Defense in depth: blank out any secret value that slipped into a message we are about to show."""
    config = request.app.state.config
    secrets = [
        config.anthropic_api_key,
        config.telegram_bot_token,
        config.telegram_bot_token.partition(":")[2],  # the secret part of "<bot id>:<secret>"
        config.fredy_webhook_token,
        config.dashboard_password,
        config.session_secret,
    ]
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


# --- security middleware & dependencies ----------------------------------------

CSP = (
    "default-src 'self'; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline'; "
    "img-src 'self' https: data:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP,
    "Cache-Control": "no-store",  # every response is dynamic and most are behind login
}


async def security_headers(request: Request, call_next):
    """HTTP middleware; registered in main.py via app.middleware("http")(security_headers)."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def is_logged_in(request: Request) -> bool:
    return request.session.get("auth") is True


async def require_login(request: Request) -> None:
    if not is_logged_in(request):
        raise HTTPException(status_code=303, headers={"Location": "/login"})


async def same_origin(request: Request) -> None:
    """Cheap CSRF backstop on top of SameSite=Strict: reject form posts from another site."""
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(status_code=403, detail="Anfrage von fremder Herkunft abgelehnt")


PAGE = [Depends(require_login)]
ACTION = [Depends(same_origin), Depends(require_login)]


# --- public endpoints -----------------------------------------------------------

@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.post("/hook/fredy")
async def fredy_webhook(request: Request):
    """Receives Fredy's HTTP notification adapter call (one job run, possibly several listings)."""
    config = request.app.state.config
    expected = f"Bearer {config.fredy_webhook_token}".encode()
    given = request.headers.get("authorization", "").encode()
    if not hmac.compare_digest(given, expected):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

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
    now = time.monotonic()
    while _login_failures and _login_failures[0] < now - LOGIN_WINDOW_S:
        _login_failures.popleft()
    if len(_login_failures) >= LOGIN_MAX_FAILURES:
        return render(request, "login.html", {"error": "Zu viele Versuche, bitte warten."}, status_code=429)

    expected = request.app.state.config.dashboard_password
    if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
        _login_failures.append(now)
        log.warning("Failed dashboard login (%d in the last %d s)", len(_login_failures), LOGIN_WINDOW_S)
        await asyncio.sleep(LOGIN_FAIL_DELAY_S)
        return render(request, "login.html", {"error": "Falsches Passwort."}, status_code=401)

    request.session.clear()
    request.session["auth"] = True
    return RedirectResponse("/", status_code=303)


@router.post("/logout", dependencies=[Depends(same_origin)])
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- listings -------------------------------------------------------------------

def _listing_or_404(listing_id: int) -> dict:
    listing = db.get_listing(listing_id)
    if listing is None:
        raise HTTPException(status_code=404, detail="Inserat nicht gefunden")
    return listing


def _draft_view(draft: dict) -> dict:
    kind, model = draft.get("kind"), draft.get("model")
    return {
        **draft,
        "kind_label": DRAFT_KIND_LABELS.get(kind, kind or "?"),
        "model_label": settings.MODELS.get(model, {}).get("label", model or "?"),
    }


def _listing_context(listing: dict, error: str | None = None) -> dict:
    return {
        "active": "listings",
        "listing": listing,
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
    listings = [{**row, "portal": portals.provider_label(row["provider"])} for row in db.list_listings(status)]
    return render(request, "listings.html", {
        "active": "listings",
        "listings": listings,
        "stats": db.stats(),
        "status_filter": status,
    })


@router.get("/listing/{listing_id}", dependencies=PAGE)
def listing_page(request: Request, listing_id: int):
    return render(request, "listing.html", _listing_context(_listing_or_404(listing_id)))


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
    except llm.LLMError as exc:
        context = _listing_context(_listing_or_404(listing_id), error=redact(request, str(exc)))
        return render(request, "listing.html", context)
    anchor = f"d{draft['id']}" if isinstance(draft, dict) and draft.get("id") else "entwuerfe"
    return RedirectResponse(f"/listing/{listing_id}#{anchor}", status_code=303)


# --- settings -------------------------------------------------------------------

def validate_settings(form: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Form fields -> (values to store, German error messages per field). Unknown keys are ignored.

    Fields missing from the form keep their stored value; absent checkboxes mean "off" ("0").
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

    for key, low, high in [("target_words", 30, 600), ("followup_delay_s", 0, 60)]:
        if key in form:
            raw = form[key].strip()
            try:
                number = int(raw)
            except ValueError:
                number = None
            if number is None or not low <= number <= high:
                errors[key] = f"Ganze Zahl von {low} bis {high}."
            values[key] = str(number) if number is not None else raw

    if "model" in form:
        values["model"] = form["model"]
        if form["model"] not in settings.MODELS:
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

    for key in CHECKBOX_SETTINGS:
        values[key] = "1" if form.get(key) else "0"
    return values, errors


def _render_settings(request: Request, values: dict, *, errors=None, message=None, error=None, test=None, status_code=200):
    model_options = [
        (key, f"{m['label']} – {_price(m['input'])} $ / {_price(m['output'])} $ pro MTok (Input/Output)")
        for key, m in settings.MODELS.items()
    ]
    return render(request, "settings.html", {
        "active": "settings",
        "values": values,
        "errors": errors or {},
        "message": message,
        "error": error,
        "test": test,
        "model_options": model_options,
        "effort_options": [(e, EFFORT_LABELS.get(e, e)) for e in settings.EFFORTS],
    }, status_code=status_code)


@router.get("/settings", dependencies=PAGE)
def settings_page(request: Request, saved: str | None = None, reset: str | None = None):
    message = None
    if saved:
        message = "Gespeichert."
    elif reset in TEXT_SETTING_LABELS:
        message = f"„{TEXT_SETTING_LABELS[reset]}“ auf Standard zurückgesetzt."
    return _render_settings(request, db.get_settings(), message=message)


@router.post("/settings", dependencies=ACTION)
async def settings_save(request: Request):
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    values, errors = validate_settings(form)
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
    except llm.LLMError as exc:
        return _render_settings(request, db.get_settings(), error=redact(request, str(exc)))
    return _render_settings(request, db.get_settings(), test={"listing": listing, "draft": _draft_view(draft)})


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
        "bot_running": bool(bot_status.get("running")),
        "bot_last_update_at": bot_status.get("last_update_at"),
        "bot_last_error": redact(request, str(last_error)) if last_error else None,
        # Booleans only - never any part of a secret.
        "api_key_set": bool(config.anthropic_api_key),
        "chat_configured": config.telegram_chat_id is not None,
        "version": settings.APP_VERSION,
        "stats": db.stats(),
        "hook_url": f"https://{request.headers.get('host', 'immobot.<domain>')}/hook/fredy",
    })
