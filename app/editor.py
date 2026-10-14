"""Draft editor (PLAN-LERNEN.md section 2): one mobile-first page per listing to edit Claude's draft by hand, let
Claude revise it after a critique, finish it (the final version becomes a style example and Claude proposes rules)
and mark the listing as sent.

Two ways in, one page:
- dashboard login: /editor/{id} (button on the listing page);
- Telegram: the "📝 Bearbeiten" login_url button opens /e/{id}/{exp}/{sig}?<login data>. editor_access.py checks link
  and login; on success the browser gets an editor session (cookie EDITOR_COOKIE, this listing only, until the
  link's first use + 24 h) and is redirected to the clean /editor/{id}.
An editor session only reaches this page for its own listing: no navigation, settings, other listings, rule list or
versions (those stay behind web.require_login). Every POST re-checks access for its listing, goes through
web.same_origin and carries a form token: editor pages are sent with "Referrer-Policy: no-referrer", so browsers
post their forms with "Origin: null", which same_origin lets through. editor_headers() adds no-referrer, noindex and
no-store to every response under /e/ and /editor/.

Claude's rule proposals after "Fertig" run as a background task; the page polls /editor/{id}/rules/status (JS, with
a <noscript> meta refresh as fallback) until they are there. The text saves itself (editor.js posts to
/editor/{id}/save with "Accept: application/json" and gets JSON back); without JS a "Zwischenspeichern" button does.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from . import bot, db, editor_access, learning, llm, media, portals, settings, textdiff, web

log = logging.getLogger(__name__)

router = APIRouter()

EDITOR_HEADERS = {"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
EDITOR_COOKIE = editor_access.EDITOR_COOKIE
GENERIC_ERROR = "Unerwarteter Fehler – Details im Log."
STALE_PAGE = "Diese Seite war nicht mehr aktuell – nichts gespeichert. Bitte den Text unten prüfen."

web.templates.env.globals["EDITOR_JS_VERSION"] = hashlib.sha256(
    (web.STATIC_DIR / "editor.js").read_bytes()).hexdigest()[:12]

_ID = re.compile(r"[1-9][0-9]{0,11}")

# Shown after a redirect (?m=…); only these keys, the query value itself is never shown.
MESSAGES = {
    "saved": "Gespeichert.",
    "revised": "Claude hat den Text überarbeitet.",
    "done": "Endversion gespeichert.",
    "rule_added": "Regel gespeichert – sie gilt ab dem nächsten Entwurf.",
    "done_note": "Endversion gespeichert. Deine Kritik im Feld hat Claude nicht mehr umgesetzt – sie ist als "
                 "Rückmeldung gespeichert und fließt ins Lernen ein.",
    "reopened": "Weiter bearbeiten – die Endversion bleibt bis zum nächsten „Fertig“ gespeichert.",
    "round": "Du bearbeitest jetzt den neueren Entwurf.",
    "accepted": "Regel übernommen – sie gilt ab dem nächsten Entwurf.",
    "removed": "Regel abgeschaltet.",
    "rejected": "Vorschlag verworfen.",
    "sent": "Als gesendet markiert.",
    "draft": "Entwurf erzeugt.",
    "shorter": "Claude hat den Text gekürzt.",
    "formal": "Claude hat den Text förmlicher formuliert.",
    "regenerate": "Claude hat den Text ganz neu geschrieben. Das ist jetzt Fassung 1 einer neuen Runde.",
}

LOGIN_AGAIN = "Im Dashboard anmelden"
TAP_AGAIN = "Bitte in Telegram noch einmal auf „📝\u00a0Bearbeiten“ tippen oder im Dashboard anmelden."
# /e/ errors (editor_access.ERROR_CODES): status code, title, text. Never contains anything from the request.
LINK_ERRORS = {
    "bad_link": (400, "Link ungültig", "Dieser Editor-Link ist ungültig oder unvollständig. " + TAP_AGAIN),
    "link_expired": (410, "Link abgelaufen", "Editor-Links aus Telegram gelten 7 Tage. Im Dashboard anmelden oder in "
                                             "Telegram einen neuen Entwurf anfordern."),
    "login_missing": (400, "Keine Anmeldung von Telegram",
                      "Telegram hat beim Öffnen keine Anmeldedaten mitgeschickt. Beim Tippen auf „📝\u00a0Bearbeiten“ "
                      "fragt Telegram, ob du dich bei dieser Seite anmelden willst – dort „Anmelden“ wählen bzw. den "
                      "Haken drin lassen. Kommt diese Frage gar nicht, ist beim Bot (@BotFather → Bot Settings → "
                      "Domain) eine andere Domain eingetragen als die dieser Seite. " + TAP_AGAIN),
    "bad_login": (400, "Anmeldung ungültig", "Die Anmeldedaten von Telegram ließen sich nicht prüfen. " + TAP_AGAIN),
    "login_stale": (410, "Link abgelaufen", "Die Anmeldung aus Telegram gilt nur 10 Minuten. " + TAP_AGAIN),
    "login_reused": (410, "Link schon benutzt", "Jeder Tipp auf „📝\u00a0Bearbeiten“ öffnet den Editor genau einmal – "
                                                "kopierte oder weitergeleitete Links gehen nicht. " + TAP_AGAIN),
    "not_allowed": (403, "Kein Zugriff", "Dieses Telegram-Konto darf den Editor nicht öffnen."),
    "allowlist_missing": (403, "Erst erlaubte User-IDs eintragen",
                          "Der Editor aus Telegram ist nur für eingetragene Telegram-Konten. Im Dashboard unter "
                          "Einstellungen → Zugänge bei „Erlaubte Telegram-User-IDs“ deine User-ID eintragen (oder den "
                          "Chat per Code neu verbinden, das trägt sie ein). Danach in Telegram erneut auf „📝\u00a0Bearbeiten“ "
                          "tippen."),
    "window_over": (410, "Link abgelaufen", "Über einen Editor-Button lässt sich der Editor 24 Stunden lang öffnen, "
                                            "gerechnet ab dem ersten Tipp. Im Dashboard anmelden oder in Telegram "
                                            "einen neuen Entwurf anfordern."),
}
EXPIRED = (401, "Sitzung abgelaufen – bitte anmelden",
           "Der Editor aus Telegram gilt einen Tag. Danach geht es mit dem Dashboard-Login weiter – oder in Telegram "
           "unter einem neuen Entwurf noch einmal auf „📝\u00a0Bearbeiten“ tippen.")
NOT_FOUND = (404, "Inserat nicht gefunden", "Dieses Inserat gibt es nicht (mehr).")

RULE_ACTION_LABELS = {"add": "Neue Regel", "update": "Regel ändern", "remove": "Regel streichen"}
CALLS_SHOWN_FROM = 20  # "Heute schon n von 30 Claude-Aufrufen …" only when the daily limit comes near

# Rule proposals after "Fertig": session id -> {"state": "running" | "done" | "error", "error": message or None}.
_rule_jobs: dict[int, dict] = {}
_job_tasks: set[asyncio.Task] = set()  # strong refs, so running jobs aren't garbage-collected


async def editor_headers(request: Request, call_next):
    """HTTP middleware (main.py, registered after web.security_headers so it runs outside it and wins):
    the editor's responses go nowhere via Referer, aren't indexed and aren't cached."""
    response = await call_next(request)
    if request.url.path.startswith(("/e/", "/editor/")):
        response.headers.update(EDITOR_HEADERS)
    return response


# --- access ------------------------------------------------------------------------------

def _listing_id(raw) -> int | None:
    return int(raw) if isinstance(raw, str) and _ID.fullmatch(raw) else None


def _access(request: Request, listing_id: int) -> str:
    """"dashboard" (logged in), "editor" (editor session for this listing), "expired" (it was one) or "none"."""
    if web.is_logged_in(request):
        return "dashboard"
    state = editor_access.check_editor_browser_session(request.cookies.get(EDITOR_COOKIE), listing_id)
    return "editor" if state == "ok" else state


def _form_token(request: Request, listing_id: int, mode: str) -> str:
    """CSRF token of the editor forms: bound to the listing and to what grants access (the dashboard login's
    stamp, or the editor cookie), keyed with SESSION_SECRET. Another site can't read or compute it."""
    if mode == "dashboard":
        identity = "d:" + web._session_stamp(request)
    else:
        identity = "e:" + (request.cookies.get(EDITOR_COOKIE) or "")
    key = request.app.state.config.session_secret.encode("utf-8")
    return hmac.new(key, f"editor-form:{listing_id}:{identity}".encode("utf-8"), "sha256").hexdigest()[:32]


def _login_path(listing_id: int | None) -> str:
    return f"/login?next=/editor/{listing_id}" if listing_id else "/login"


def _message_page(request: Request, page: tuple[int, str, str], listing_id: int | None = None,
                  login_link: bool = True):
    status, title, text = page
    return web.render(request, "editor_message.html", {
        "title": title, "text": text, "login_url": _login_path(listing_id) if login_link else None,
        "logged_in": web.is_logged_in(request),
    }, status_code=status)


def _gate(request: Request, raw_id: str, form_token: str | None = None):
    """Access check shared by every editor route. Returns (listing, mode, None) or (None, None, response to send
    instead: login redirect, expired page, not found). With form_token (POSTs) the token must match too."""
    listing_id = _listing_id(raw_id)
    if listing_id is None:
        return None, None, _message_page(request, NOT_FOUND, login_link=False)
    mode = _access(request, listing_id)
    if mode == "expired":
        return None, None, _message_page(request, EXPIRED, listing_id)
    if mode == "none":
        return None, None, RedirectResponse(_login_path(listing_id), status_code=303)
    listing = db.get_listing(listing_id)
    if listing is None:
        return None, None, _message_page(request, NOT_FOUND, login_link=False)
    if form_token is not None and not hmac.compare_digest(form_token.encode("utf-8"),
                                                          _form_token(request, listing_id, mode).encode("utf-8")):
        return None, None, _message_page(request, (403, "Seite veraltet", "Bitte den Editor neu laden und es noch "
                                                                          "einmal versuchen."), listing_id)
    return listing, mode, None


# --- Telegram way in ------------------------------------------------------------------------------

@router.get("/e/{listing_id}/{exp}/{sig}")
def editor_link(request: Request, listing_id: str, exp: str, sig: str):
    """The "📝 Bearbeiten" button: check link and Telegram login (editor_access), start the editor browser session."""
    cookie, error = editor_access.start_editor_browser_session(listing_id, exp, sig, dict(request.query_params))
    lid = _listing_id(listing_id)
    if error:
        log.info("Editor link refused: %s (login fields: %s; %s)", error,
                 ",".join(sorted(k for k in request.query_params if k.isidentifier())[:8]) or "none",
                 (request.headers.get("user-agent") or "?")[:120])
        return _message_page(request, LINK_ERRORS.get(error, LINK_ERRORS["bad_link"]), lid)
    session = editor_access.get_editor_browser_session(cookie)
    max_age = max(0, int(datetime.fromisoformat(session["expires_at"]).timestamp() - time.time()))
    response = RedirectResponse(f"/editor/{lid}", status_code=303)  # login data and token leave the address bar
    response.set_cookie(EDITOR_COOKIE, cookie, max_age=max_age, path="/", httponly=True, samesite="lax",
                        secure=request.app.state.config.cookie_secure)
    return response


# --- page --------------------------------------------------------------------------------------------

def _rule_job(session_id: int) -> dict | None:
    return _rule_jobs.get(session_id)


def _proposals(session: dict) -> tuple[list[dict], list[dict]]:
    """(undecided proposals of the session with "target_text" for update/remove, its decided rules)."""
    proposals = []
    for rule in db.list_rules("proposed", session_id=session["id"]):
        target = db.get_rule(rule["replaces_rule_id"]) if rule["replaces_rule_id"] else None
        proposals.append({**rule, "action_label": RULE_ACTION_LABELS.get(rule["action"], rule["action"]),
                          "target_text": target["text"] if target else None})
    decided = [{**rule, "status_label": learning.RULE_STATUS_LABELS.get(rule["status"], rule["status"])}
               for rule in db.list_rules(("active", "rejected", "inactive", "replaced", "merged"),
                                         session_id=session["id"])]
    return proposals, decided


def _render(request: Request, listing: dict, mode: str, *, message: str | None = None, error: str | None = None,
            critique: str | None = None, lost_text: str | None = None, manual_rule: str | None = None,
            status_code: int = 200):
    listing_id = listing["id"]
    session = learning.open_or_create_session(listing_id)
    current = db.get_settings()
    context = {
        "active": "listings",
        "logged_in": mode == "dashboard",
        "listing": listing,
        "portal": portals.provider_label(listing["provider"]),
        "facts": [listing[key] for key in ("price", "size", "address") if str(listing.get(key) or "").strip()],
        "contact_url": portals.safe_url(portals.contact_url(listing["provider"], listing["url"])),
        "token": _form_token(request, listing_id, mode),
        "session": session,
        "message": message,
        "error": web.redact(request, error) if error else None,
        "critique": critique,
        "lost_text": lost_text,
        "manual_rule": manual_rule,  # "Regel selbst eintragen" typed, but refused: shown again
        "use_rules": settings.as_bool(current.get("use_rules")),
        "rule_max_chars": learning.RULE_MAX_CHARS,
        # "Inserat-Infos" (collapsed): pictures via /editor/{id}/media/{position}, the exposé text or Fredy's text
        "media": media.stored_pictures(listing_id),
        "has_gallery": media.has_gallery(listing),
        "details": web.details_sections(listing.get("details")),
    }
    if session is not None:
        text = session["final_text"] if session["status"] == "done" else session["current_text"]
        feedback = list(reversed(db.list_feedback(session_id=session["id"])))  # oldest first
        history = [{**f, "quick_label": learning.QUICK_LABELS.get(f["text"]) if f["kind"] == "quick" else None}
                   for f in feedback if f["kind"] in ("critique", "quick")]  # one per revision by Claude
        notes = [f for f in feedback if f["kind"] == "note"]  # typed but not sent before "Fertig"
        last_draft = db.get_draft(session["last_draft_id"]) if session["last_draft_id"] else None
        newer = learning.newer_draft(session)
        proposals, decided = _proposals(session)
        job = _rule_job(session["id"])
        if job and job["state"] == "done":
            _rule_jobs.pop(session["id"], None)  # shown now: the page lists the proposals
        context.update(
            text=text,
            changed=textdiff.has_changes(textdiff.word_diff(session["original_text"], text)),
            history=history,
            notes=notes,
            version_no=len(history) + 1,
            hint=last_draft.get("hint") if last_draft else None,
            newer={**newer, "kind_label": web.DRAFT_KIND_LABELS.get(newer["kind"], newer["kind"])} if newer else None,
            proposals=proposals,
            decided=decided,
            job=job if job and job["state"] != "done" else None,
            job_error=web.redact(request, job["error"]) if job and job.get("error") else None,
            calls_today=learning.editor_calls_today(listing_id),
            calls_limit=learning.EDITOR_DAILY_LIMIT,
            calls_shown_from=CALLS_SHOWN_FROM,
            # after "Fertig" without proposals and without a running job: may Claude still be asked (button)?
            rule_state=learning.rule_round_state(session) if session["status"] == "done" else "off",
        )
    return web.render(request, "editor.html", context, status_code=status_code)


@router.get("/editor/{listing_id}")
async def editor_page(request: Request, listing_id: str, m: str | None = None, k: str | None = None):
    """k: a critique kept across a quick button (editor_quick), by a one-time id; the text never is in the URL.
    The first visit gets the pictures (and the ImmoScout24 exposé text) if nothing was fetched yet, like a draft
    would: opening the editor is the same intent. Takes a few seconds once; later visits only read what's stored."""
    listing, mode, denied = _gate(request, listing_id)
    if denied is not None:
        return denied
    if media.has_gallery(listing) and not listing.get("media_fetched_at"):
        await media.ensure_media(listing["id"])
        listing = db.get_listing(listing["id"]) or listing
    return _render(request, listing, mode, message=MESSAGES.get(m or ""), critique=web._take_once(k) if k else None)


@router.get("/editor/{listing_id}/media/{position}")
def editor_media(request: Request, listing_id: str, position: int):
    """A stored picture of the listing, for an editor session from Telegram as well as the dashboard login."""
    listing, mode, denied = _gate(request, listing_id)
    if denied is not None:
        return denied
    path = media.media_file(listing["id"], position) if position > 0 else None
    if path is None:
        return _message_page(request, (404, "Bild nicht gefunden", "Dieses Bild gibt es nicht (mehr)."),
                             login_link=False)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/editor/{listing_id}/rules/status")
def editor_rules_status(request: Request, listing_id: str):
    """For the page's polling while Claude looks for rules: {"state": "running" | "done" | "error" | "idle"}."""
    lid = _listing_id(listing_id)
    if lid is None or _access(request, lid) not in ("dashboard", "editor"):
        return JSONResponse({"state": "denied"}, status_code=403)
    session = db.latest_session(lid)
    job = _rule_job(session["id"]) if session else None
    return {"state": job["state"] if job else "idle"}


# --- actions ------------------------------------------------------------------------------------------

def _back(listing_id: int, message: str, anchor: str = "") -> RedirectResponse:
    return RedirectResponse(f"/editor/{listing_id}?m={message}" + (f"#{anchor}" if anchor else ""), status_code=303)


def _current_session(listing: dict, session_id: str) -> dict | None:
    """The listing's current session if the form was made for it (else the page was stale: another tab started a
    new round, say)."""
    session = db.latest_session(listing["id"])
    if session is None or str(session["id"]) != session_id.strip():
        return None
    return session


def _failed(request: Request, listing: dict, mode: str, exc: Exception, **kwargs):
    """The page again with the error: LLMError texts (budget, limit, session state) as they are, anything else
    generic (and logged)."""
    if isinstance(exc, llm.LLMError):
        message = str(exc) or GENERIC_ERROR
    else:
        log.exception("Editor action failed for listing %s", listing["id"])
        message = GENERIC_ERROR
    return _render(request, listing, mode, error=message, status_code=400, **kwargs)


def _stale(request: Request, listing: dict, mode: str, text: str | None = None):
    session = db.latest_session(listing["id"])
    current = (session or {}).get("current_text")
    lost = text if text and text.replace("\r\n", "\n") != current else None
    return _render(request, listing, mode, error=STALE_PAGE, lost_text=lost, status_code=409)


ACTION = [Depends(web.same_origin)]

# Autosave answers in JSON: {"ok": true} or {"ok": false, "error": short German text, "stale": bool}.
AUTOSAVE_DENIED = {
    401: "Sitzung abgelaufen – Text kopieren und neu anmelden.",
    403: "Seite veraltet – bitte neu laden.",
    404: "Inserat nicht gefunden.",
}
AUTOSAVE_STALE = "Seite nicht mehr aktuell."


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _autosave_answer(status_code: int, error: str | None = None, stale: bool = False) -> JSONResponse:
    if error is None:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": error, "stale": stale}, status_code=status_code)


@router.post("/editor/{listing_id}/save", dependencies=ACTION)
def editor_save(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                text: str = Form("")):
    """Autosave from editor.js (JSON in, see _autosave_answer), or "Zwischenspeichern" without JS (redirect)."""
    autosave = _wants_json(request)
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        if not autosave:
            return denied
        status = 401 if isinstance(denied, RedirectResponse) else denied.status_code  # redirect: not logged in
        return _autosave_answer(status, AUTOSAVE_DENIED.get(status, AUTOSAVE_DENIED[403]))
    session = _current_session(listing, session_id)
    if session is None or session["status"] != "open":  # another tab started a new round or pressed "Fertig"
        return _autosave_answer(409, AUTOSAVE_STALE, stale=True) if autosave else _stale(request, listing, mode, text)
    try:
        learning.save_text(session["id"], text)
    except Exception as exc:
        if not autosave:
            return _failed(request, listing, mode, exc)
        if not isinstance(exc, llm.LLMError):
            log.exception("Autosave failed for listing %s", listing["id"])
        return _autosave_answer(400, str(exc) if isinstance(exc, llm.LLMError) else GENERIC_ERROR)
    return _autosave_answer(200) if autosave else _back(listing["id"], "saved", "text")


@router.post("/editor/{listing_id}/revise", dependencies=ACTION)
async def editor_revise(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                        text: str = Form(""), critique: str = Form("")):
    """"Überarbeiten lassen": synchronous, a draft takes 10-60 s. The hand edits are saved first in any case."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode, text)
    try:
        learning.save_text(session["id"], text)
        await learning.revise_in_editor(session["id"], text, critique)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    return _back(listing["id"], "revised", "text")


# --- quick buttons next to the critique field ("Kürzer", "Förmlicher", "Ganz neu schreiben") ---------------------

@router.post("/editor/{listing_id}/quick", dependencies=ACTION)
async def editor_quick(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                       text: str = Form(""), critique: str = Form(""), kind: str = Form("")):
    """A quick button (learning.quick_in_editor): Claude shortens or formalises the current text like Telegram's
    buttons, or writes a new draft that starts a new round. Hand edits are saved first; a critique typed into the
    field but not sent stays there afterwards."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode, text)
    try:
        learning.save_text(session["id"], text)
        await learning.quick_in_editor(session["id"], text, kind)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    keep = f"&k={web._stash_once(critique)}" if critique.strip() else ""
    return RedirectResponse(f"/editor/{listing['id']}?m={kind}{keep}#text", status_code=303)


def _start_rule_job(session_id: int) -> None:
    """Claude's rule proposals for a finished session, in the background (the page polls the state)."""
    if (_rule_jobs.get(session_id) or {}).get("state") == "running":
        return
    _rule_jobs[session_id] = {"state": "running", "error": None}
    task = asyncio.create_task(_propose_rules(session_id))
    _job_tasks.add(task)
    task.add_done_callback(_job_tasks.discard)


async def _propose_rules(session_id: int) -> None:
    try:
        await learning.propose_rules(session_id)
    except llm.LLMError as exc:  # budget, daily limit, refusal, API problems: short German texts
        _rule_jobs[session_id] = {"state": "error", "error": str(exc) or GENERIC_ERROR}
    except Exception:
        log.exception("Rule proposals failed for session %s", session_id)
        _rule_jobs[session_id] = {"state": "error", "error": GENERIC_ERROR}
    else:
        _rule_jobs[session_id] = {"state": "done", "error": None}


@router.post("/editor/{listing_id}/finish", dependencies=ACTION)
async def editor_finish(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                        text: str = Form(""), critique: str = Form("")):
    """"Fertig": store the final version; with "Gelernte Regeln nutzen" on, Claude looks for rules in the background.
    A critique still in its field (typed, but "Überarbeiten lassen" not pressed) is kept as a note for learning."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode, text)
    try:
        session = learning.finish(session["id"], text, note=critique)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    if settings.as_bool(db.get_settings().get("use_rules")):
        _start_rule_job(session["id"])
    bot.schedule_refresh(listing["id"])  # Telegram shows the Endversion instead of Claude's draft
    return _back(listing["id"], "done_note" if critique.strip() else "done", "ende")


@router.post("/editor/{listing_id}/rules/retry", dependencies=ACTION)
async def editor_rules_retry(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form("")):
    """"Regeln vorschlagen" / "Nochmal versuchen": ask Claude for rules of a finished session again (after an error,
    or when no round was made for this final version, e.g. because a restart cut the background call off)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None or session["status"] != "done":
        return _stale(request, listing, mode)
    if settings.as_bool(db.get_settings().get("use_rules")):
        _start_rule_job(session["id"])
    return RedirectResponse(f"/editor/{listing['id']}#regeln", status_code=303)


@router.post("/editor/{listing_id}/reopen", dependencies=ACTION)
async def editor_reopen(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form("")):
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode)
    if (_rule_job(session["id"]) or {}).get("state") == "running":
        return _render(request, listing, mode, error="Claude sucht gerade nach Regeln – bitte kurz warten.",
                       status_code=409)
    try:
        learning.reopen(session["id"])
    except Exception as exc:
        return _failed(request, listing, mode, exc)
    _rule_jobs.pop(session["id"], None)
    bot.schedule_refresh(listing["id"])  # back to Claude's latest draft in Telegram
    return _back(listing["id"], "reopened", "text")


@router.post("/editor/{listing_id}/new-round", dependencies=ACTION)
def editor_new_round(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form("")):
    """"Mit dem neueren Entwurf weitermachen": a new session from the newest draft (the open one is superseded)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode)
    if (_rule_job(session["id"]) or {}).get("state") == "running":
        return _render(request, listing, mode, error="Claude sucht gerade nach Regeln – bitte kurz warten.",
                       status_code=409)
    try:
        learning.start_new_round(listing["id"])
    except Exception as exc:
        return _failed(request, listing, mode, exc)
    return _back(listing["id"], "round", "text")


@router.post("/editor/{listing_id}/rules/add", dependencies=ACTION)
def editor_add_rule(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                    text: str = Form("")):
    """"Regel selbst eintragen" after "Fertig": a rule of the user's own, active at once, stored with this session.
    Registered before /rules/{rule_id}."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None or session["status"] != "done":
        return _stale(request, listing, mode)
    try:
        learning.add_manual_rule(text, session_id=session["id"])
    except Exception as exc:
        return _failed(request, listing, mode, exc, manual_rule=text)
    return _back(listing["id"], "rule_added", "regeln")


@router.post("/editor/{listing_id}/rules/{rule_id}", dependencies=ACTION)
def editor_decide_rule(request: Request, listing_id: str, rule_id: str, token: str = Form(""),
                       session_id: str = Form(""), decision: str = Form(""), text: str = Form("")):
    """✅ / ❌ on a rule proposal - only one of this listing's current session."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    rid = _listing_id(rule_id)
    rule = db.get_rule(rid) if rid else None
    if session is None or rule is None or rule["session_id"] != session["id"]:
        return _render(request, listing, mode, error="Diesen Vorschlag gibt es hier nicht (mehr).", status_code=404)
    if decision not in ("accept", "reject"):
        return _render(request, listing, mode, error="Bitte „Übernehmen“ oder „Verwerfen“ wählen.", status_code=400)
    try:
        decided = learning.decide_rule(rule["id"], decision == "accept", text if rule["action"] != "remove" else None)
    except Exception as exc:
        return _failed(request, listing, mode, exc)
    if decision == "reject":
        message = "rejected"
    else:
        message = "removed" if rule["action"] == "remove" and decided["id"] != rule["id"] else "accepted"
    return _back(listing["id"], message, "regeln")


@router.post("/editor/{listing_id}/sent", dependencies=ACTION)
async def editor_mark_sent(request: Request, listing_id: str, token: str = Form("")):
    """"Als gesendet markieren": status "sent", and the Telegram message collapses like after "✅ Gesendet"."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    db.set_listing_status(listing["id"], "sent")
    bot.schedule_refresh(listing["id"])
    return _back(listing["id"], "sent", "ende")


@router.post("/editor/{listing_id}/draft", dependencies=ACTION)
async def editor_create_draft(request: Request, listing_id: str, token: str = Form("")):
    """"Entwurf erzeugen" while the listing has no draft yet (afterwards the page edits that one)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    if db.latest_draft(listing["id"]) is None:
        try:
            await llm.create_and_store_draft(listing["id"], "initial", source="editor", record_feedback=False)
        except Exception as exc:
            return _failed(request, listing, mode, exc)
        bot.schedule_refresh(listing["id"])  # the Telegram message shows the draft too
    return _back(listing["id"], "draft", "text")
