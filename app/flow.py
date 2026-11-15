"""The two pages after writing (behind the dashboard login), ported from the ui-bold prototype onto the main layout:

- "/bewerbungen"  Meine Bewerbungen: every sent listing and what happened since - Antwort, Besichtigung (with date),
                  Zusage / Absage - as steps in db.listing_outcomes (append-only; the newest step is the stage, a
                  step can be taken back). "seit n Tagen still" after settings.QUIET_DAYS. The listing page shows the
                  same steps and form for a sent listing (outcome_macros.html).
- "/stil"         So schreibe ich: the weekly look at what immobot learned - this week's numbers, the newest
                  proposals with one tap each (text, "Gilt, wenn" and "immer" behind "Anpassen"), older ones expire in
                  one go, the active rules as one readable list (switch off / on again). Replaces the wall of
                  proposal forms on Einstellungen → Gelernt, which now only links here.
"""

import difflib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from . import cards, db, learning, portals, settings, web

log = logging.getLogger(__name__)

router = APIRouter()

PAGE, ACTION = web.PAGE, web.ACTION
STALE_PROPOSAL_DAYS = 14  # "Ältere verfallen lassen" on /stil
PROPOSALS_SHOWN = 5
RULES_SHOWN = 6  # active rules before "Alle anzeigen"
NOTE_MAX = 300


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


# --- Meine Bewerbungen ------------------------------------------------------------------------------------------

def _rows() -> list[dict]:
    listings = db.applications()
    thumbs = db.thumb_positions()
    steps = db.outcomes([l["id"] for l in listings])
    rows = []
    for listing in listings:
        thumb = (f"/listing/{listing['id']}/media/{thumbs[listing['id']]}" if listing["id"] in thumbs
                 else portals.safe_url(listing.get("image_url")))
        rows.append({**listing, "thumb": thumb, "card": cards.facts(listing),
                     "app": cards.application(listing, steps.get(listing["id"]))})
    return rows


GROUPS = [  # (key, title, stages) in the order of the page: what needs you first
    ("besichtigung", "Besichtigung", ("besichtigung",)),
    ("antwort", "Antwort bekommen", ("antwort",)),
    ("wartet", "Wartet auf Antwort", ("wartet",)),
    ("entschieden", "Zu- und Absagen", ("zusage", "absage")),
]


@router.get("/bewerbungen", dependencies=PAGE)
def applications_page(request: Request, m: str | None = None):
    rows = _rows()
    groups = []
    for key, title, stages in GROUPS:
        items = [r for r in rows if r["app"]["stage"] in stages]
        if key == "besichtigung":
            items.sort(key=lambda r: r["app"]["viewing"] or "9999")
        groups.append({"key": key, "title": title, "items": items})
    today = datetime.now(settings.LOCAL_TZ).strftime("%Y-%m-%d")  # a viewing today still counts as coming up
    upcoming = next((r for r in groups[0]["items"] if (r["app"]["viewing"] or "") >= today), None)
    reached = {kind: sum(1 for r in rows if any(e["kind"] == kind for e in r["app"]["events"]))
               for kind in settings.OUTCOMES}
    return web.render(request, "bewerbungen.html", {
        "active": "bewerbungen",
        "groups": groups,
        "total": len(rows),
        "reached": reached,  # how many got an Antwort / Besichtigung / Zusage / Absage at some point
        "answered": sum(1 for r in rows if r["app"]["events"]),
        "quiet_count": sum(1 for r in rows if r["app"]["quiet"]),
        "upcoming": upcoming,
        "quiet_days": settings.QUIET_DAYS,
        "message": {"step": "Eingetragen.", "undo": "Zurückgenommen."}.get(m or ""),
    })


def _back(next: str, listing_id: int, message: str) -> RedirectResponse:
    back = (web.safe_next(next) or "/bewerbungen").split("#")[0].split("?")[0]
    anchor = "bewerbung" if back.startswith("/listing/") else f"b{listing_id}"
    return RedirectResponse(f"{back}?m={message}#{anchor}", status_code=303)


def _clean_when(date: str, time: str) -> str | None:
    """The viewing's date (and time) from the form: 'YYYY-MM-DD' (+ 'HH:MM'), or a datetime-local value."""
    value = (date or "").strip()[:16]
    if "T" not in value and value and (time or "").strip():
        value = f"{value}T{time.strip()[:5]}"
    if not value:
        return None
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return None
    return value


@router.post("/bewerbung/{listing_id}", dependencies=ACTION)
async def application_step(listing_id: int, kind: str = Form(""), when: str = Form(""), time: str = Form(""),
                           note: str = Form(""), next: str = Form("")):
    """A step after sending: Antwort / Besichtigung (with the date if known) / Zusage / Absage. Only sent listings."""
    web._listing_or_404(listing_id)
    if kind in settings.OUTCOMES:
        happened = _clean_when(when, time) if kind == "besichtigung" else None
        db.add_outcome(listing_id, kind, happened, " ".join(note.split())[:NOTE_MAX] or None)
    return _back(next, listing_id, "step")


@router.post("/bewerbung/{listing_id}/zurueck", dependencies=ACTION)
async def application_undo(listing_id: int, next: str = Form("")):
    """"Rückgängig": the newest step goes away (pressed by mistake)."""
    web._listing_or_404(listing_id)
    db.undo_outcome(listing_id)
    return _back(next, listing_id, "undo")


# --- So schreibe ich --------------------------------------------------------------------------------------------

def _changed_share(original: str, final: str) -> int:
    a, b = (original or "").split(), (final or "").split()
    if not a and not b:
        return 0
    return round(100 * (1 - difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()))


def _week() -> dict:
    since = _since(7)
    with db.connect() as conn:
        sessions = [dict(r) for r in conn.execute(
            "SELECT original_text, final_text FROM edit_sessions WHERE status = 'done' AND finished_at >= ?", (since,))]
        sent = conn.execute("SELECT COUNT(*) FROM listings WHERE status = 'sent' AND sent_at >= ?", (since,)).fetchone()[0]
    shares = [_changed_share(s["original_text"], s["final_text"] or "") for s in sessions]
    return {
        "sent": sent,
        "finished": len(sessions),
        "changed": sum(1 for s in shares if s > 0),
        "avg_share": round(sum(shares) / len(shares)) if shares else 0,
    }


STYLE_MESSAGES = {
    "ja": "Übernommen – gilt ab dem nächsten Anschreiben.",
    "nein": "Verworfen – Claude schlägt das nicht wieder vor.",
    "aus": "Abgeschaltet – die Regel bleibt gespeichert.",
    "an": "Wieder an.",
    "aufgeraeumt": "Ältere Vorschläge sind verfallen.",
}


@router.get("/stil", dependencies=PAGE)
def style_page(request: Request, m: str | None = None, error: str | None = None):
    proposals = db.list_rules("proposed", newest_first=True)
    cutoff = _since(STALE_PROPOSAL_DAYS)
    fresh = [p for p in proposals if (p.get("created_at") or "") >= cutoff]
    shown = (fresh or proposals)[:PROPOSALS_SHOWN]
    for p in shown:
        target = db.get_rule(p["replaces_rule_id"]) if p.get("replaces_rule_id") else None
        p["target_text"] = target["text"] if target else None
        p["action_label"] = {"add": "Neu", "update": "Ändern", "remove": "Streichen",
                             "fact": "Über mich"}.get(p["action"], p["action"])
    active = db.list_rules("active")
    current = db.get_settings()
    return web.render(request, "stil.html", {
        "active": "stil",
        "week": _week(),
        "shown": shown,
        "proposal_count": len(proposals),
        "stale_count": len(proposals) - len(fresh),
        "stale_days": STALE_PROPOSAL_DAYS,
        "active_rules": active,
        "rules_shown": RULES_SHOWN,
        "inactive_rules": db.list_rules("inactive", newest_first=True, limit=10),
        "rule_max": learning.MAX_ACTIVE_RULES,
        "rule_max_chars": learning.RULE_MAX_CHARS,
        "use_rules": settings.as_bool(current.get("use_rules")),
        "rewrite": next(iter(db.list_rewrite_proposals("pending", limit=1)), None),
        "message": STYLE_MESSAGES.get(m or ""),
        "error": error and error[:200],
    })


def _style_error(exc: Exception) -> RedirectResponse:
    message = str(exc) if isinstance(exc, learning.LearningError) else "Unerwarteter Fehler – Details im Log."
    if not isinstance(exc, learning.LearningError):
        log.exception("Stil action failed")
    return RedirectResponse(f"/stil?error={quote(message)}", status_code=303)


@router.post("/stil/vorschlag/{rule_id}", dependencies=ACTION)
def style_decide(rule_id: int, decision: str = Form(""), text: str | None = Form(None), scope: str | None = Form(None),
                 strength: str = Form("")):
    """One tap: "Ja, so schreibe ich" (decision=ja) / "Nein". Under "Anpassen" the text, "Gilt, wenn" and "immer"
    can be changed first ("Angepasst übernehmen", decision=angepasst); "Ja" takes the proposal as it is."""
    yes = decision in ("ja", "angepasst")
    try:
        if decision == "angepasst":
            learning.decide_rule(rule_id, True, text, scope, "hard" if strength == "hard" else "soft")
        else:
            learning.decide_rule(rule_id, yes)
    except Exception as exc:
        return _style_error(exc)
    return RedirectResponse(f"/stil?m={'ja' if yes else 'nein'}#neu", status_code=303)


@router.post("/stil/aufraeumen", dependencies=ACTION)
def style_expire():
    """Proposals older than STALE_PROPOSAL_DAYS are rejected in one go (Claude won't propose them again)."""
    cutoff = _since(STALE_PROPOSAL_DAYS)
    for rule in db.list_rules("proposed"):
        if (rule.get("created_at") or "") < cutoff:
            try:
                learning.decide_rule(rule["id"], False)
            except learning.LearningError:
                pass
    return RedirectResponse("/stil?m=aufgeraeumt#neu", status_code=303)


@router.post("/stil/regel/{rule_id}", dependencies=ACTION)
def style_toggle(rule_id: int, active: str = Form("")):
    try:
        learning.set_rule_active(rule_id, active == "1")
    except Exception as exc:
        return _style_error(exc)
    return RedirectResponse(f"/stil?m={'an' if active == '1' else 'aus'}#regeln", status_code=303)
