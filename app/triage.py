"""What speaks for or against a listing at a glance, found by words in the ad (no LLM call, free): Tausch, WBS, WG,
befristet, Senioren, nur Studierende ("warn": most people won't apply), möbliert ("info"), Balkon/Terrasse and Küche
("good"), and "2× gemeldet" when the same street and price came in more than once recently (Fredy's jobs overlap).

Shown on the cards of the main page (listing_card.html via web._listing_cards), so listings without a draft - and
thus without Claude's hint - get a signal too. Words, not a verdict: the patterns are kept narrow (title only where
descriptions say "WG ungeeignet" or "Austausch"), and a negation right before a good word ("kein Balkon") drops it.
Ported from the ui-bold prototype (app/triage.py there), with its false positives on the real feed fixed.
"""

import re
from datetime import datetime, timedelta, timezone

from . import db

# (pattern, where: "title" | "text" (title, description and exposé), label, kind)
FLAG_PATTERNS = [
    (re.compile(r"\btausch|wohnungstausch", re.IGNORECASE), "title", "Tauschwohnung", "warn"),
    (re.compile(r"\bWBS\b", re.IGNORECASE), "title", "WBS nötig", "warn"),
    (re.compile(r"(?:\bwbs\b|wohnberechtigungsschein)\)?\s*(?:ist\s+)?(?:zwingend\s+)?"
                r"(?:erforderlich|notwendig|nötig|benötigt|voraussetzung)(?!:\s*nein)", re.IGNORECASE),
     "text", "WBS nötig", "warn"),
    (re.compile(r"\bWG\b|WG-Zimmer|wohngemeinschaft", re.IGNORECASE), "title", "WG", "warn"),
    (re.compile(r"zwischenmiete|(?<!un)befristet", re.IGNORECASE), "text", "befristet", "warn"),
    (re.compile(r"senior|ab\s+(?:50|55|60|65)\s*(?:\+|jahre)", re.IGNORECASE), "title", "Senioren", "warn"),
    (re.compile(r"seniorenwohnung|wohnen für senioren|personen ab (?:50|55|60|65)", re.IGNORECASE), "text",
     "Senioren", "warn"),
    (re.compile(r"nur (?:für )?studen|ausschließlich für studen|studentenwohn|studierendenwohn", re.IGNORECASE),
     "text", "nur Studierende", "warn"),
    (re.compile(r"(?<!un)(?<!teil)möbliert", re.IGNORECASE), "text", "möbliert", "info"),
    (re.compile(r"balkon|terrasse|loggia", re.IGNORECASE), "text", "Balkon/Terrasse", "good"),
    (re.compile(r"einbauküche|\bEBK\b", re.IGNORECASE), "text", "Küche", "good"),
]
_NEGATION = re.compile(r"(?:kein|keine|keinen|ohne)\s+(?:\w+\s+)?$", re.IGNORECASE)
_NO_AFTER = re.compile(r"^[^\n:]{0,20}:\s*nein", re.IGNORECASE)  # "Balkon/Terrasse: nein" in the exposé facts
KIND_ORDER = {"warn": 0, "info": 1, "good": 2}
DUPLICATE_DAYS = 14
SHOWN = 3  # flags on a card


def _found(pattern: re.Pattern, text: str, kind: str) -> bool:
    for match in pattern.finditer(text):
        if kind == "good" and (_NEGATION.search(text[max(0, match.start() - 25):match.start()])
                               or _NO_AFTER.match(text[match.end():])):
            continue
        return True
    return False


def flags(listing: dict, duplicate_count: int = 0) -> list[dict]:
    """[{"label", "kind"}], warnings first, each label once."""
    title = str(listing.get("title") or "")
    text = " ".join(str(listing.get(k) or "") for k in ("title", "description", "details"))
    found, labels = [], set()
    for pattern, where, label, kind in FLAG_PATTERNS:
        if label not in labels and _found(pattern, title if where == "title" else text, kind):
            found.append({"label": label, "kind": kind})
            labels.add(label)
    if duplicate_count > 1:
        found.append({"label": f"{duplicate_count}× gemeldet", "kind": "info"})
    return sorted(found, key=lambda f: KIND_ORDER[f["kind"]])


def _norm_street(address: str) -> str:
    street = (address or "").split(",")[0].lower()
    street = street.replace("strasse", "straße").replace("str.", "straße")
    return re.sub(r"[^a-zäöüß0-9]", "", street)


def duplicates(listing_ids: list[int]) -> dict[int, int]:
    """listing id -> how many listings of the last DUPLICATE_DAYS share its street (with house number) and price
    (only ≥ 2). The same exposé reported by two Fredy jobs, or re-posted."""
    wanted = set(listing_ids)
    if not wanted:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=DUPLICATE_DAYS)).isoformat(timespec="seconds")
    with db.connect() as conn:
        rows = conn.execute("SELECT id, address, price FROM listings WHERE received_at >= ?", (since,)).fetchall()
    groups: dict[tuple, list[int]] = {}
    for row in rows:
        street = _norm_street(row["address"])
        if not any(ch.isdigit() for ch in street) or re.match(r"[0-9]{5}", street):  # "50737 Köln, …": no street
            continue
        groups.setdefault((street, (row["price"] or "").strip()), []).append(row["id"])
    return {i: len(ids) for ids in groups.values() if len(ids) > 1 for i in ids if i in wanted}
