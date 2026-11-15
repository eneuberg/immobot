"""What a main-page card says about a listing beyond its title: the rent that matters (warm if known), rooms,
district and one flag from Claude's hint. Pure functions on a listing row (and the hint of its newest draft), so the
card stays one line of facts and the tests need no database.

facts(listing, hint) -> {"rent": "810 € warm" | "750 €" | None, "rooms": "2 Zimmer" | None,
                         "district": "Mülheim" | None, "flag": {"label", "count", "alarm"} | None}
"""

import re

from . import rent

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


def facts(listing: dict, hint: str | None = None) -> dict:
    return {"rent": rent_text(listing), "rooms": rooms(listing), "district": district(listing), "flag": flag(hint)}
