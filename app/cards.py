"""What a main-page card says about a listing beyond its title: the rent that matters (warm if known), rooms,
district, one flag from Claude's hint, and up to triage.SHOWN word flags from the ad itself (Tausch, WBS, Balkon,
"2× gemeldet", …: also for listings without a draft). Pure functions on a listing row (and the hint of its newest
draft, the duplicate count), so the card stays one line of facts and the tests need no database.

facts(listing, hint, duplicate_count) -> {"rent": "810 € warm" | "750 €" | None, "rooms": "2 Zimmer" | None,
    "district": "Mülheim" | None, "flag": {"label", "count", "alarm"} | None, "tags": [{"label", "kind"}],
    "effort": "Volle Mühe" | None}
"""

import re
from datetime import datetime, timezone

from . import db, rent, triage
from .settings import OUTCOME_WAITING, OUTCOMES, QUIET_DAYS

_ROOMS_LINE = re.compile(r"^Zimmer:\s*([0-9]+(?:[.,][0-9]+)?)\s*$", re.MULTILINE)
_ROOMS_TITLE = re.compile(r"(?<![0-9.,])([1-9](?:[.,]5)?)\s*-?\s*(?:Zimmer|Zi\b|Zi\.|Raum)", re.IGNORECASE)
_POSTCODE = re.compile(r"^\s*[0-9]{5}\s+")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$", re.MULTILINE)
_BOLD_LEAD = re.compile(r"^\*\*([^*\n]{2,80}?)\*\*\s*:?")
_BOLD = re.compile(r"\*\*([^*\n]{2,80}?)\*\*")
# A bullet containing one of these is the flag, whatever comes first: it can cost money or more.
ALARM_WORDS = ("vorsicht", "fake", "betrug", "achtung", "unseriös", "scam", "warnung")
LABEL_MAX = 34


def rooms(listing: dict) -> str | None:
    """"2 Zimmer" from the exposé ("Zimmer: 2") or the title ("3-Zimmer-Wohnung"), else None."""
    match = _ROOMS_LINE.search(listing.get("details") or "") or _ROOMS_TITLE.search(listing.get("title") or "")
    if not match:
        return None
    value = match.group(1).replace(".", ",")
    if value.endswith(",0"):
        value = value[:-2]
    return f"{value} Zimmer"


def district(listing: dict) -> str | None:
    """The last part of the address ("51065 Köln, Mülheim" -> "Mülheim"); a lone "50667 Köln" -> "Köln"."""
    parts = [p.strip() for p in (listing.get("address") or "").split(",") if p.strip()]
    if not parts:
        return None
    return _POSTCODE.sub("", parts[-1])[:40] or None


def rent_text(listing: dict) -> str | None:
    """The rent that matters for deciding: warm (or total) when the exposé says, else Fredy's price unlabelled."""
    info = rent.info(listing)
    for value, label in ((info["warm"], "warm"), (info["total"], "gesamt")):
        if value:  # only the amount on a card: "900 € gesamt" (the listing page has "zzgl. Heizkosten" and the like)
            amount = rent._AMOUNT.match(value)
            return f"{amount.group(1)} {label}" if amount else rent._labelled(value, label)
    return info["cold"]


def _short(label: str) -> str:
    label = " ".join(label.replace("*", "").split()).rstrip(":").strip()
    return label if len(label) <= LABEL_MAX else label[:LABEL_MAX - 1].rstrip() + "…"


def flag(hint: str | None) -> dict | None:
    """One flag from Claude's hint (a Markdown list of "**Label:** text" points, usually): the point with an alarm
    word if there is one, else the first. {"label", "count" (points), "alarm"} or None without a hint."""
    text = (hint or "").strip()
    if not text:
        return None
    bullets = _BULLET.findall(text)
    if not bullets:
        bold = _BOLD.search(text)
        label = bold.group(1) if bold else " ".join(text.split()[:4])
        return {"label": _short(label), "count": 1, "alarm": any(w in text.lower() for w in ALARM_WORDS)}
    chosen = next((b for b in bullets if any(w in b.lower() for w in ALARM_WORDS)), None)
    alarm = chosen is not None
    chosen = chosen or bullets[0]
    lead = _BOLD_LEAD.match(chosen.strip())
    if lead:
        label = lead.group(1)
    else:
        bold = _BOLD.search(chosen)
        label = bold.group(1) if bold else " ".join(chosen.split()[:4])
    return {"label": _short(label), "count": len(bullets), "alarm": alarm}


def facts(listing: dict, hint: str | None = None, duplicate_count: int = 0) -> dict:
    return {"rent": rent_text(listing), "rooms": rooms(listing), "district": district(listing), "flag": flag(hint),
            "tags": triage.flags(listing, duplicate_count)[:triage.SHOWN], "effort": _effort_tag(listing)}


def _effort_tag(listing: dict) -> str | None:
    """A small tag only for "Volle Mühe" (db.EFFORT_LEVELS): honest and normal are the usual case."""
    return db.EFFORT_LEVELS[3] if db.effort_of(listing) == 3 else None


# --- "Bewerbungen": one sent listing and what happened since (db.listing_outcomes) --------------------------------

_WEEKDAYS = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


def when_label(value: str | None) -> str | None:
    """'2026-11-18T17:30' -> 'Mi 18.11., 17:30 Uhr'; a date alone -> 'Mi 18.11.'; anything else as it is."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return value
    label = f"{_WEEKDAYS[when.weekday()]} {when:%d.%m.}"
    return label + f", {when:%H:%M} Uhr" if "T" in value else label


def _days_since(iso: str | None, now: datetime) -> int | None:
    if not iso:
        return None
    try:
        then = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0, (now - then).days)


def application(listing: dict, events: list[dict] | None, now: datetime | None = None) -> dict:
    """{"stage": "wartet" | an OUTCOMES key, "stage_label", "viewing" (the newest viewing's ISO date or None),
    "viewing_label", "sent_days", "quiet" (waiting for QUIET_DAYS or more), "events": [{…, "label", "when"}]}."""
    now = now or datetime.now(timezone.utc)
    events = events or []
    stage = events[-1]["kind"] if events else "wartet"
    viewing = next((e["happened_on"] for e in reversed(events) if e["kind"] == "besichtigung" and e["happened_on"]),
                   None)
    sent_days = _days_since(listing.get("sent_at") or listing.get("updated_at"), now)
    return {
        "stage": stage,
        "stage_label": OUTCOMES.get(stage, OUTCOME_WAITING),
        "viewing": viewing,
        "viewing_label": when_label(viewing),
        "sent_days": sent_days,
        "quiet": stage == "wartet" and (sent_days or 0) >= QUIET_DAYS,
        "events": [{**e, "label": OUTCOMES.get(e["kind"], e["kind"]), "when": when_label(e["happened_on"])}
                   for e in events],
    }
