"""Draft editor (PLAN-LERNEN.md section 2): one mobile-first page per listing to edit Claude's draft by hand, let
Claude revise it after a critique, finish it (the final version becomes a style example and Claude proposes rules)
and mark the listing as sent.

Two ways in, one page:
- dashboard login: /editor/{id} ("✍️ Anschreiben" on the listing page);
- Telegram: the "✍️ Anschreiben" login_url button opens /e/{id}/{exp}/{sig}?<login data>. editor_access.py checks link
  and login; on success the browser gets an editor session (cookie EDITOR_COOKIE, this listing only, until the
  link's first use + 24 h) and is redirected to the clean /editor/{id}.
An editor session only reaches this page for its own listing: no navigation, settings, other listings, rule list or
versions (those stay behind web.require_login). Every POST re-checks access for its listing, goes through
web.same_origin and carries a form token: editor pages are sent with "Referrer-Policy: no-referrer", so browsers
post their forms with "Origin: null", which same_origin lets through. editor_headers() adds no-referrer, noindex and
no-store to every response under /e/ and /editor/.

Without a draft the page is a progress bar: the first draft is written in the background (drafting.py), started by
the listing page's POST or by editor.js posting /editor/{id}/draft once the page is open (never by the GET itself;
without JS a button does it), and polled at /editor/{id}/draft/status until the letter is there.

One action finishes a text: "Kopieren & Inserat öffnen" (editor.js copies the text and opens the ad, then posts
/finish; without JS only /finish). Claude's rule proposals after that run as a background task and are shown on
their own page, /editor/{id}/lernen, away from copying and sending; it polls /editor/{id}/rules/status (JS, with a
<noscript> meta refresh as fallback) until they are there. The text saves itself (editor.js posts to
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

from . import (bot, cards, db, drafting, editor_access, history, learning, llm, media, portals, rent, settings,
               textdiff, web)

log = logging.getLogger(__name__)

router = APIRouter()

EDITOR_HEADERS = {"Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex", "Cache-Control": "no-store"}
EDITOR_COOKIE = editor_access.EDITOR_COOKIE
NAV_HEADER = "x-editor-nav"  # "replace": editor.js submits a form and wants the redirect as JSON (editor_headers)
GENERIC_ERROR = "Unerwarteter Fehler – Details im Log."
STALE_PAGE = "Diese Seite war nicht mehr aktuell – nichts gespeichert. Bitte den Text unten prüfen."

web.templates.env.globals["EDITOR_JS_VERSION"] = hashlib.sha256(
    (web.STATIC_DIR / "editor.js").read_bytes()).hexdigest()[:12]

_ID = re.compile(r"[1-9][0-9]{0,11}")

# Shown after a redirect (?m=…); only these keys, the query value itself is never shown.
MESSAGES = {
    "saved": "Gespeichert.",
    "revised": "Claude hat den Text überarbeitet.",
    "done": "Fertig. Text kopieren, im Inserat einfügen und abschicken.",
    "done_copied": "Kopiert. Im Inserat einfügen und abschicken – dann unten bestätigen.",
    "rule_added": "Regel gespeichert – sie gilt ab dem nächsten Entwurf.",
    "done_note": "Fertig. Deine Kritik im Feld hat Claude nicht mehr umgesetzt – sie ist als Rückmeldung "
                 "gespeichert und fließt ins Lernen ein.",
    "reopened": "Weiter bearbeiten – der fertige Text bleibt gespeichert, bis du wieder abschließt.",
    "round": "Du bearbeitest jetzt das neuere Anschreiben.",
    "accepted": "Regel übernommen – sie gilt ab dem nächsten Entwurf.",
    "removed": "Regel abgeschaltet.",
    "rejected": "Vorschlag verworfen.",
    "fact": "„Über mich“ geändert – gilt ab dem nächsten Entwurf.",
    "revision_kept": "Überarbeitung übernommen.",
    "revision_undone": "Zurück zu deinem Text von vor der Überarbeitung.",
    "sent": "Abgeschickt ✓ – steht jetzt unter „Bewerbungen“.",
    "unsent": "„Gesendet“ zurückgenommen.",
    "dismissed": "Aussortiert: uninteressant.",
    "restored": "Zurückgeholt – das Inserat ist wieder in der Liste.",
    "locked": "Das Inserat ist aussortiert – am Status hat sich nichts geändert.",
    "draft": "Claude hat ein Anschreiben geschrieben.",
    "media": "Inserat neu geladen.",
    "media_loaded": "Bilder geladen.",
    "media_soon": "Gerade erst geladen – in einer Minute geht es wieder.",
    "shorter": "Claude hat den Text gekürzt.",
    "formal": "Claude hat den Text förmlicher formuliert.",
    "regenerate": "Claude hat den Text ganz neu geschrieben. Das ist jetzt Fassung 1 einer neuen Runde.",
}

LOGIN_AGAIN = "Im Dashboard anmelden"
TAP_AGAIN = "Bitte in Telegram noch einmal auf „✍️\u00a0Anschreiben“ tippen oder im Dashboard anmelden."
# /e/ errors (editor_access.ERROR_CODES): status code, title, text. Never contains anything from the request.
LINK_ERRORS = {
    "bad_link": (400, "Link ungültig", "Dieser Editor-Link ist ungültig oder unvollständig. " + TAP_AGAIN),
    "link_expired": (410, "Link abgelaufen", "Editor-Links aus Telegram gelten 7 Tage. Im Dashboard anmelden oder in "
                                             "Telegram einen neuen Entwurf anfordern."),
    "login_missing": (400, "Keine Anmeldung von Telegram",
                      "Telegram hat beim Öffnen keine Anmeldedaten mitgeschickt. Beim Tippen auf „✍️\u00a0Anschreiben“ "
                      "fragt Telegram, ob du dich bei dieser Seite anmelden willst – dort „Anmelden“ wählen bzw. den "
                      "Haken drin lassen. Kommt diese Frage gar nicht, ist beim Bot (@BotFather → Bot Settings → "
                      "Domain) eine andere Domain eingetragen als die dieser Seite. " + TAP_AGAIN),
    "bad_login": (400, "Anmeldung ungültig", "Die Anmeldedaten von Telegram ließen sich nicht prüfen. " + TAP_AGAIN),
    "login_stale": (410, "Link abgelaufen", "Die Anmeldung aus Telegram gilt nur 10 Minuten. " + TAP_AGAIN),
    "login_reused": (410, "Link schon benutzt", "Jeder Tipp auf „✍️\u00a0Anschreiben“ öffnet den Editor genau einmal – "
                                                "kopierte oder weitergeleitete Links gehen nicht. " + TAP_AGAIN),
    "not_allowed": (403, "Kein Zugriff", "Dieses Telegram-Konto darf den Editor nicht öffnen."),
    "allowlist_missing": (403, "Erst erlaubte User-IDs eintragen",
                          "Der Editor aus Telegram ist nur für eingetragene Telegram-Konten. Im Dashboard unter "
                          "Einstellungen → Zugänge bei „Erlaubte Telegram-User-IDs“ deine User-ID eintragen (oder den "
                          "Chat per Code neu verbinden, das trägt sie ein). Danach in Telegram erneut auf „✍️\u00a0Anschreiben“ "
                          "tippen."),
    "window_over": (410, "Link abgelaufen", "Über einen Editor-Button lässt sich der Editor 24 Stunden lang öffnen, "
                                            "gerechnet ab dem ersten Tipp. Im Dashboard anmelden oder in Telegram "
                                            "einen neuen Entwurf anfordern."),
}
REENTRY_ERRORS = ("login_missing", "login_stale", "login_reused")  # checked after the link signature
EXPIRED = (401, "Sitzung abgelaufen – bitte anmelden",
           "Der Editor aus Telegram gilt einen Tag. Danach geht es mit dem Dashboard-Login weiter – oder in Telegram "
           "unter einem neuen Entwurf noch einmal auf „✍️\u00a0Anschreiben“ tippen.")
NOT_FOUND = (404, "Inserat nicht gefunden", "Dieses Inserat gibt es nicht (mehr).")

RULE_ACTION_LABELS = {"add": "Neue Vorliebe", "update": "Regel ändern", "remove": "Regel streichen",
                      "fact": "„Über mich“ ergänzen"}
CALLS_SHOWN_FROM = 20  # "Heute schon n von 30 Claude-Aufrufen …" only when the daily limit comes near

# Rule proposals after "Fertig": session id -> {"state": "running" | "done" | "error", "error": message or None}.
_rule_jobs: dict[int, dict] = {}
_job_tasks: set[asyncio.Task] = set()  # strong refs, so running jobs aren't garbage-collected


async def editor_headers(request: Request, call_next):
    """HTTP middleware (main.py, registered after web.security_headers so it runs outside it and wins):
    the editor's responses go nowhere via Referer, aren't indexed and aren't cached."""
    response = await call_next(request)
    if not request.url.path.startswith(("/e/", "/editor/")):
        return response
    if response.status_code == 303 and request.headers.get(NAV_HEADER) == "replace":
        # editor.js sent the form: it gets the redirect target as JSON and replaces the page with it, so the
        # browser history keeps one entry (one "back" returns to Telegram). Cookies set on the way are kept.
        replaced = JSONResponse({"location": response.headers.get("location", "")})
        for name, value in response.raw_headers:
            if name.lower() == b"set-cookie":
                replaced.raw_headers.append((name, value))
        response = replaced
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
    db.mark_seen(listing_id)  # opened or used in the editor: the main page's card stops glowing
    return listing, mode, None


# --- Telegram way in ------------------------------------------------------------------------------

@router.get("/e/{listing_id}/{exp}/{sig}")
def editor_link(request: Request, listing_id: str, exp: str, sig: str):
    """The "✍️ Anschreiben" button: check link and Telegram login (editor_access), start the editor browser session."""
    cookie, error = editor_access.start_editor_browser_session(listing_id, exp, sig, dict(request.query_params))
    lid = _listing_id(listing_id)
    if error in REENTRY_ERRORS and lid is not None and _access(request, lid) in ("dashboard", "editor"):
        # A valid link, but no (usable) Telegram login data - Telegram doesn't always attach them, e.g. on a
        # message it edited. This browser already has access to the listing (its 24 h editor session or the
        # dashboard login), so the button simply opens the editor again.
        return RedirectResponse(f"/editor/{lid}", status_code=303)
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
            open_media: bool = False, learn_view: bool = False, undo_dismiss: bool = False,
            answer: dict | None = None, status_code: int = 200):
    """The editor page; learn_view: its "Was Claude daraus lernt" view (/editor/{id}/lernen, a finished text).
    answer: {"question", "answer"} of a "Nur fragen" without JS (with JS editor.js puts it in place)."""
    listing_id = listing["id"]
    session = learning.open_or_create_session(listing_id)
    current = db.get_settings()
    context = {
        "active": "listings",
        "logged_in": mode == "dashboard",
        "listing": listing,
        "portal": portals.provider_label(listing["provider"]),
        "facts": [fact for fact in [rent.price_text(listing), listing.get("size"), listing.get("address")]
                  if str(fact or "").strip()],
        "contact_url": portals.safe_url(portals.contact_url(listing["provider"], listing["url"])),
        "token": _form_token(request, listing_id, mode),
        "session": session,
        "message": message,
        "undo_dismiss": undo_dismiss,  # "Aussortiert · Rückgängig" after "Aussortieren"
        "error": web.redact(request, error) if error else None,
        "lost_text": lost_text,
        "manual_rule": manual_rule,  # "Regel selbst eintragen" typed, but refused: shown again
        "use_rules": settings.as_bool(current.get("use_rules")),
        "rule_max_chars": learning.RULE_MAX_CHARS,
        # "Inserat-Infos" (collapsed): pictures via /editor/{id}/media/{position}, the exposé text or Fredy's text
        "media": media.stored_pictures(listing_id),
        "has_gallery": media.has_gallery(listing),
        "pictures_missing": media.pictures_missing(listing),
        "open_media": open_media,
        "details": web.details_sections(listing.get("details")),
        "learn_view": learn_view or manual_rule is not None,
    }
    if session is None:  # no draft yet: the progress bar while Claude writes the first one (drafting.py)
        job = drafting.status(listing_id)
        context["draft_job"] = {**job, "error": web.redact(request, job["error"]) if job["error"] else None}
    else:
        drafting.forget(listing_id)
        text = session["final_text"] if session["status"] == "done" else session["current_text"]
        feedback = db.list_feedback(session_id=session["id"])
        revisions = [f for f in feedback if f["kind"] in ("critique", "quick")]  # one per revision by Claude
        last_draft = db.get_draft(session["last_draft_id"]) if session["last_draft_id"] else None
        newer = learning.newer_draft(session)
        proposals, decided = _proposals(session)
        review = learning.pending_review(session)
        job = _rule_job(session["id"])
        if job and job["state"] == "done" and (learn_view or manual_rule is not None):
            _rule_jobs.pop(session["id"], None)  # shown now: the page lists the proposals
        # A revision / question running in the background (drafting.start_editor_job): its bar; a failed one: the
        # error with the critique back in its field; an answered question: the answer (each shown once).
        editor_job = drafting.take_editor_job(listing_id) if session["status"] == "open" and not learn_view else None
        if editor_job and editor_job["state"] == "error" and critique is None:
            critique = editor_job.get("critique")
        if editor_job and editor_job["state"] == "done" and editor_job["kind"] == drafting.ASK and answer is None:
            answer = {"question": editor_job.get("critique"), "answer": editor_job.get("answer")}
        if editor_job and editor_job["state"] == "done":
            editor_job = None
        if editor_job and editor_job.get("error"):
            editor_job = {**editor_job, "error": web.redact(request, editor_job["error"])}
        versions, base = history.timeline(listing_id, web.DRAFT_KIND_LABELS)
        live = None
        if session["status"] == "open" and base is not None:  # her edits since the newest version, live in editor.js
            segments = textdiff.word_diff(base, text)
            live = {"base": base, "diff": textdiff.trim_equal(segments), "changed": textdiff.has_changes(segments)}
        context.update(
            text=text,
            changed=textdiff.has_changes(textdiff.word_diff(session["original_text"], text)),
            versions=list(reversed(versions)),  # newest first, like a commit log
            live=live,
            version_no=len(revisions) + 1,
            hint=last_draft.get("hint") if last_draft else None,
            hint_flag=cards.flag(last_draft.get("hint")) if last_draft else None,  # the hint box's one line
            newer={**newer, "kind_label": web.DRAFT_KIND_LABELS.get(newer["kind"], newer["kind"])} if newer else None,
            proposals=proposals,
            decided=decided,
            review={**review, "quick_label": learning.QUICK_LABELS.get(review["text"])} if review else None,
            job=job if job and job["state"] != "done" else None,
            job_error=web.redact(request, job["error"]) if job and job.get("error") else None,
            editor_job=editor_job,
            revising=bool(editor_job and editor_job["state"] == "running" and editor_job["kind"] != drafting.ASK),
            answer=answer,
            calls_today=learning.editor_calls_today(listing_id),
            calls_limit=learning.EDITOR_DAILY_LIMIT,
            calls_shown_from=CALLS_SHOWN_FROM,
            # after "Fertig" without proposals and without a running job: may Claude still be asked (button)?
            rule_state=learning.rule_round_state(session) if session["status"] == "done" else "off",
        )
    context["critique"] = critique
    return web.render(request, "editor.html", context, status_code=status_code)


@router.get("/editor/{listing_id}")
async def editor_page(request: Request, listing_id: str, m: str | None = None, k: str | None = None):
    """k: a critique kept across a quick button (editor_quick), by a one-time id; the text never is in the URL.
    The first visit gets the pictures (and the ImmoScout24 exposé text) if nothing was fetched yet, like a draft
    would: opening the editor is the same intent. Takes a few seconds once; later visits only read what's stored."""
    listing, mode, denied = _gate(request, listing_id)
    if denied is not None:
        return denied
    # Without a draft the page has to show its progress bar at once; the draft job fetches the pictures itself.
    if media.pictures_missing(listing) and not listing.get("media_error") and db.latest_draft(listing["id"]):
        await media.ensure_media(listing["id"])
        listing = db.get_listing(listing["id"]) or listing
    # Just aussortiert by hand: "Aussortiert · Rückgängig" (a toast with JS) instead of the plain message.
    undo_dismiss = m == "dismissed" and listing.get("discard_reason") == settings.MANUAL_DISCARD
    return _render(request, listing, mode, message=None if undo_dismiss else MESSAGES.get(m or ""),
                   critique=web._take_once(k) if k else None, undo_dismiss=undo_dismiss,
                   open_media=m in ("media", "media_loaded", "media_failed", "media_soon"))


@router.get("/editor/{listing_id}/lernen")
def editor_learn_page(request: Request, listing_id: str, m: str | None = None):
    """What Claude learns from the finished text: its proposals to decide, a rule of one's own. Linked from the
    finished editor; nothing here is needed to send the letter."""
    listing, mode, denied = _gate(request, listing_id)
    if denied is not None:
        return denied
    return _render(request, listing, mode, message=MESSAGES.get(m or ""), learn_view=True)


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


@router.get("/editor/{listing_id}/draft/status")
def editor_draft_status(request: Request, listing_id: str):
    """For the progress bar while Claude writes the first draft (drafting.status): state "running" | "done" |
    "error" | "idle", the error text, elapsed and expected milliseconds."""
    lid = _listing_id(listing_id)
    if lid is None or _access(request, lid) not in ("dashboard", "editor"):
        return JSONResponse({"state": "denied"}, status_code=403)
    job = drafting.status(lid)
    return {**job, "error": web.redact(request, job["error"]) if job["error"] else None}


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


def _back_learn(listing_id: int, message: str) -> RedirectResponse:
    """Back to the "Was Claude daraus lernt" view after deciding a proposal or adding a rule."""
    return RedirectResponse(f"/editor/{listing_id}/lernen?m={message}#regeln", status_code=303)


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


# "Überarbeiten lassen" with only "Wie viel Mühe?" / "Was findest du gut?" changed: the request Claude gets (and the
# Verlauf shows), by the new level and whether she wrote a line. Only the first paragraph changes.
PERSONAL_REVISION_KEEP = " Der Rest bleibt, wie er ist."
PERSONAL_REVISION = {
    1: "Schreib den ersten Absatz ehrlicher und sachlicher (siehe <persoenlich>): echtes Interesse, aber keine "
       "Begeisterung, die ich nicht habe.",
    2: "Greif im ersten Absatz auf, was ich selbst zu dieser Wohnung sage (siehe <persoenlich>).",
    3: "Gib dir im ersten Absatz mehr Mühe (siehe <persoenlich>): Nenn konkret, was an dieser Wohnung gut ist{words}.",
}
PERSONAL_REVISION_PLAIN = ("Schreib den ersten Absatz in normalem Ton, ohne besondere Zurückhaltung und ohne extra "
                           "Persönliches.")


def personal_revision(listing: dict) -> str:
    """The request for a changed level / line alone (PERSONAL_REVISION)."""
    level, words = db.effort_of(listing), bool((listing.get("own_words") or "").strip())
    if level == 2 and not words:  # back to normal without a line: no <persoenlich> block to point at
        return PERSONAL_REVISION_PLAIN + PERSONAL_REVISION_KEEP
    text = PERSONAL_REVISION[level].replace("{words}", ", und bring ein, was ich selbst dazu sage" if words else "")
    return text + PERSONAL_REVISION_KEEP


def _store_personal(listing_id: int, personal: str, own_words: str, effort_level: str) -> bool:
    """The form had the "Wie viel Mühe?" / "Was findest du gut?" fields (personal=1): store them. Returns whether
    they changed."""
    return personal == "1" and db.set_personal(listing_id, own_words, db.parse_effort(effort_level))


def _wants_json(request: Request) -> bool:
    return "application/json" in request.headers.get("accept", "")


def _autosave_answer(status_code: int, error: str | None = None, stale: bool = False) -> JSONResponse:
    if error is None:
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False, "error": error, "stale": stale}, status_code=status_code)


@router.post("/editor/{listing_id}/media", dependencies=ACTION)
async def editor_load_media(request: Request, listing_id: str, token: str = Form(""), reload: str = Form("")):
    """"Bilder laden" (missing pictures, also after a failure) or "Inserat neu laden" (reload=1: exposé text and all
    pictures again, at most once a minute). Takes a few seconds."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    if reload == "1":
        message = "media" if await media.reload(listing["id"]) else "media_soon"
    else:
        await media.ensure_media(listing["id"], retry=True)
        message = "media_loaded"
    if message != "media_soon" and (db.get_listing(listing["id"]) or {}).get("media_error"):
        message = "media_failed"  # no flash: the opened gallery shows the reason
    bot.schedule_refresh(listing["id"])  # rent in the Telegram message
    return _back(listing["id"], message, "bilder")


@router.post("/editor/{listing_id}/save", dependencies=ACTION)
def editor_save(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                text: str = Form(""), personal: str = Form(""), own_words: str = Form(""), effort_level: str = Form("")):
    """Autosave from editor.js (JSON in, see _autosave_answer), or "Zwischenspeichern" without JS (redirect)."""
    autosave = _wants_json(request)
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        if not autosave:
            return denied
        status = 401 if isinstance(denied, RedirectResponse) else denied.status_code  # redirect: not logged in
        return _autosave_answer(status, AUTOSAVE_DENIED.get(status, AUTOSAVE_DENIED[403]))
    _store_personal(listing["id"], personal, own_words, effort_level)
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
                        text: str = Form(""), critique: str = Form(""), personal: str = Form(""),
                        own_words: str = Form(""), effort_level: str = Form("")):
    """"Überarbeiten lassen": synchronous, a draft takes 10-60 s. The hand edits are saved first in any case.
    A changed "Wie viel Mühe?" / "Was findest du gut?" goes into the prompt (llm.personal_block); changed without a
    critique, Claude is asked to rework the first paragraph for it (personal_revision)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return _job_denied(request, denied)
    session = _current_session(listing, session_id)
    if session is None:
        return _job_stale(request, listing, mode, text)
    if _store_personal(listing["id"], personal, own_words, effort_level) and not critique.strip():
        critique = personal_revision(db.get_listing(listing["id"]))
    if _wants_json(request):
        if not critique.strip():
            return _job_error(request, learning.LearningError("Bitte schreiben, was Claude ändern soll."))
        return _start_job(request, listing, session, "custom", text, critique)
    try:
        learning.save_text(session["id"], text)
        await learning.revise_in_editor(session["id"], text, critique)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    return _back(listing["id"], "revised", "text")


@router.post("/editor/{listing_id}/ask", dependencies=ACTION)
async def editor_ask(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                     text: str = Form(""), critique: str = Form(""), personal: str = Form(""),
                     own_words: str = Form(""), effort_level: str = Form("")):
    """"Nur fragen": the critique field holds a question; Claude answers from the ad, the profile and the letter
    (drafting.ask). The letter isn't changed (only its hand edits saved) and nothing goes into learning. With JS in
    the background like a revision (editor.js shows the answer under the card), without JS synchronously."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return _job_denied(request, denied)
    session = _current_session(listing, session_id)
    if session is None:
        return _job_stale(request, listing, mode, text)
    _store_personal(listing["id"], personal, own_words, effort_level)
    if not critique.strip():
        return _job_error(request, learning.LearningError("Bitte schreiben, was du wissen willst."),
                          listing=listing, mode=mode)
    if _wants_json(request):
        return _start_job(request, listing, session, drafting.ASK, text, critique)
    try:
        learning.save_text(session["id"], text)
        answer = await drafting.ask(listing["id"], critique, text)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    return _render(request, db.get_listing(listing["id"]) or listing, mode, answer={"question": critique.strip(),
                                                                                    "answer": answer})


@router.get("/editor/{listing_id}/job/status")
def editor_job_status(request: Request, listing_id: str):
    """For the progress bar while Claude revises the letter or answers a question (drafting.editor_status): state
    "running" | "done" | "error" | "idle", the error, the answer (as HTML), elapsed and expected milliseconds and
    where to go when a revision is done."""
    lid = _listing_id(listing_id)
    if lid is None or _access(request, lid) not in ("dashboard", "editor"):
        return JSONResponse({"state": "denied"}, status_code=403)
    return _job_answer(request, lid)


def _job_answer(request: Request, listing_id: int, status_code: int = 200) -> JSONResponse:
    job = drafting.editor_status(listing_id)
    answer = job.pop("answer")
    message = job.pop("message")
    job.pop("critique", None)
    body = {**job, "error": web.redact(request, job["error"]) if job["error"] else None,
            "answer_html": str(web.templates.env.filters["md"](answer)) if answer else None,
            "done_url": f"/editor/{listing_id}?m={message}#text" if message and job["state"] == "done" else None}
    return JSONResponse(body, status_code=status_code)


def _start_job(request: Request, listing: dict, session: dict, kind: str, text: str, critique: str = ""):
    """editor.js's "Überarbeiten lassen" / quick button / "Nur fragen": save the hand edits, check the daily limit
    and the budget at once (so a refusal shows before the bar), then let drafting run it in the background."""
    try:
        learning.save_text(session["id"], text)
        if not drafting.editor_running(listing["id"]):
            learning.check_editor_limit(listing["id"])
            llm.check_budget(db.get_settings())
    except Exception as exc:
        return _job_error(request, exc)
    drafting.start_editor_job(listing["id"], session["id"], kind, text, critique)
    return _job_answer(request, listing["id"])


def _job_error(request: Request, exc: Exception, *, listing: dict | None = None, mode: str | None = None):
    """A job that couldn't start: JSON for editor.js ({"state": "error", "error"}), else the page with the error."""
    if not _wants_json(request) and listing is not None:
        return _failed(request, listing, mode, exc)
    if isinstance(exc, llm.LLMError):
        message = str(exc) or GENERIC_ERROR
    else:
        log.exception("Editor job could not start")
        message = GENERIC_ERROR
    return JSONResponse({"state": "error", "error": web.redact(request, message)}, status_code=400)


def _job_denied(request: Request, denied):
    if not _wants_json(request):
        return denied
    status = 401 if isinstance(denied, RedirectResponse) else denied.status_code
    return JSONResponse({"state": "denied", "error": AUTOSAVE_DENIED.get(status, AUTOSAVE_DENIED[403])},
                        status_code=status)


def _job_stale(request: Request, listing: dict, mode: str, text: str):
    if not _wants_json(request):
        return _stale(request, listing, mode, text)
    return JSONResponse({"state": "stale", "error": STALE_PAGE}, status_code=409)


@router.post("/editor/{listing_id}/revision", dependencies=ACTION)
def editor_revision(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                    text: str = Form(""), decision: str = Form("")):
    """"👍 Übernehmen" (hand edits since are saved) / "↩️ Verwerfen" (back to the text before the revision)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None or session["status"] != "open":
        return _stale(request, listing, mode, text)
    try:
        if decision == "accept":
            learning.save_text(session["id"], text)
        learning.decide_revision(session["id"], decision == "accept")
    except Exception as exc:
        return _failed(request, listing, mode, exc)
    return _back(listing["id"], "revision_kept" if decision == "accept" else "revision_undone", "text")


# --- quick buttons next to the critique field ("Kürzer", "Förmlicher", "Ganz neu schreiben") ---------------------

@router.post("/editor/{listing_id}/quick", dependencies=ACTION)
async def editor_quick(request: Request, listing_id: str, token: str = Form(""), session_id: str = Form(""),
                       text: str = Form(""), critique: str = Form(""), kind: str = Form(""),
                       personal: str = Form(""), own_words: str = Form(""), effort_level: str = Form("")):
    """A quick button (learning.quick_in_editor): Claude shortens or formalises the current text like Telegram's
    buttons, or writes a new draft that starts a new round. Hand edits are saved first; a critique typed into the
    field but not sent stays there afterwards."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return _job_denied(request, denied)
    session = _current_session(listing, session_id)
    if session is None:
        return _job_stale(request, listing, mode, text)
    _store_personal(listing["id"], personal, own_words, effort_level)
    if _wants_json(request):
        if kind not in learning.QUICK_LABELS:
            return _job_error(request, learning.LearningError("Unbekannte Aktion."))
        return _start_job(request, listing, session, kind, text)
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
                        text: str = Form(""), critique: str = Form(""), copied: str = Form(""),
                        personal: str = Form(""), own_words: str = Form(""), effort_level: str = Form("")):
    """"Kopieren & Inserat öffnen" (copied=1: editor.js copied the text and opened the ad first): store the final
    version; with "Gelernte Regeln nutzen" on, Claude looks for rules in the background. A critique still in its
    field (typed, but "Überarbeiten lassen" not pressed) is kept as a note for learning."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    if session is None:
        return _stale(request, listing, mode, text)
    _store_personal(listing["id"], personal, own_words, effort_level)
    try:
        session = learning.finish(session["id"], text, note=critique)
    except Exception as exc:
        return _failed(request, listing, mode, exc, critique=critique)
    if settings.as_bool(db.get_settings().get("use_rules")):
        _start_rule_job(session["id"])
    bot.schedule_refresh(listing["id"])  # Telegram shows the Endversion instead of Claude's draft
    message = "done_note" if critique.strip() else ("done_copied" if copied == "1" else "done")
    return _back(listing["id"], message, "ende")


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
    return RedirectResponse(f"/editor/{listing['id']}/lernen#regeln", status_code=303)


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
    return _back_learn(listing["id"], "rule_added")


@router.post("/editor/{listing_id}/rules/{rule_id}", dependencies=ACTION)
def editor_decide_rule(request: Request, listing_id: str, rule_id: str, token: str = Form(""),
                       session_id: str = Form(""), decision: str = Form(""), text: str = Form(""),
                       scope: str = Form(""), strength: str = Form(""), has_opts: str = Form("")):
    """✅ / ❌ on a rule proposal - only one of this listing's current session."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    session = _current_session(listing, session_id)
    rid = _listing_id(rule_id)
    rule = db.get_rule(rid) if rid else None
    if session is None or rule is None or rule["session_id"] != session["id"]:
        return _render(request, listing, mode, error="Diesen Vorschlag gibt es hier nicht (mehr).", learn_view=True,
                       status_code=404)
    if decision not in ("accept", "reject"):
        return _render(request, listing, mode, error="Bitte „Übernehmen“ oder „Verwerfen“ wählen.", learn_view=True,
                       status_code=400)
    try:
        opts = {"scope": scope, "strength": "hard" if strength == "hard" else "soft"} if has_opts == "1" else {}
        decided = learning.decide_rule(rule["id"], decision == "accept", text if rule["action"] != "remove" else None,
                                       **opts)
    except Exception as exc:
        return _failed(request, listing, mode, exc, learn_view=True)
    if decision == "reject":
        message = "rejected"
    elif rule["action"] == "fact":
        message = "fact"
    else:
        message = "removed" if rule["action"] == "remove" and decided["id"] != rule["id"] else "accepted"
    return _back_learn(listing["id"], message)


@router.post("/editor/{listing_id}/sent", dependencies=ACTION)
async def editor_mark_sent(request: Request, listing_id: str, token: str = Form("")):
    """"Als gesendet markieren": status "sent", and the Telegram message collapses like after "✅ Gesendet"."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    if not db.mark_sent(listing["id"]):  # aussortiert meanwhile (a stale page)
        return _back(listing["id"], "locked")
    bot.schedule_refresh(listing["id"])
    return _back(listing["id"], "sent", "ende")


@router.post("/editor/{listing_id}/unsent", dependencies=ACTION)
async def editor_unmark_sent(request: Request, listing_id: str, token: str = Form("")):
    """"Zurücknehmen" beside "✓ Gesendet": pressed by mistake, back to "Angefangen"."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    if db.unmark_sent(listing["id"]):
        bot.schedule_refresh(listing["id"])
    return _back(listing["id"], "unsent", "ende")


@router.post("/editor/{listing_id}/dismiss", dependencies=ACTION)
async def editor_mark_dismissed(request: Request, listing_id: str, token: str = Form("")):
    """"Uninteressant" (not sent after all, or before writing): aussortiert by hand, the Telegram message collapses
    like after "❌ Uninteressant". The status stays underneath; the editor keeps working."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    db.discard(listing["id"])
    bot.schedule_refresh(listing["id"])
    return _back(listing["id"], "dismissed", "ende")


@router.post("/editor/{listing_id}/restore", dependencies=ACTION)
async def editor_restore(request: Request, listing_id: str, token: str = Form("")):
    """"↩️ Zurückholen" in the banner of an "uninteressant" listing (offline / veraltet can't be undone)."""
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        return denied
    if db.restore(listing["id"]):
        bot.schedule_refresh(listing["id"])
    return _back(listing["id"], "restored")


@router.post("/editor/{listing_id}/draft", dependencies=ACTION)
async def editor_create_draft(request: Request, listing_id: str, token: str = Form(""), personal: str = Form(""),
                              own_words: str = Form(""), effort_level: str = Form("")):
    """Start the first draft in the background (drafting.start: nothing if there is one or it is being written).
    editor.js posts it as soon as the page shows "no draft yet" (and for "Nochmal versuchen") and gets the job's
    status as JSON; without JS the button "Anschreiben schreiben lassen" posts it and comes back to the progress
    bar. The optional "Wie viel Mühe?" and "Was findest du gut?" are stored first, so this first draft uses them."""
    wants_json = _wants_json(request)
    listing, mode, denied = _gate(request, listing_id, token)
    if denied is not None:
        if not wants_json:
            return denied
        status = 401 if isinstance(denied, RedirectResponse) else denied.status_code
        return JSONResponse({"state": "denied", "error": AUTOSAVE_DENIED.get(status, AUTOSAVE_DENIED[403])},
                            status_code=status)
    _store_personal(listing["id"], personal, own_words, effort_level)
    drafting.start(listing["id"])
    if wants_json:
        return editor_draft_status(request, str(listing["id"]))
    return RedirectResponse(f"/editor/{listing['id']}#text", status_code=303)
