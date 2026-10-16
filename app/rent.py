"""Rent of a listing: Fredy's price (the cold rent on ImmoScout24) plus warm and total rent from the exposé text
(listings.details, "Warmmiete: 690 €" / "Gesamtmiete: 790 €"). Shown wherever a listing's price appears.

parts() -> ["570 € kalt", "690 € warm"]; the total rent only when it differs from the warm rent or that is missing
("900 € gesamt zzgl. Heizkosten"). Without exposé data just Fredy's price, unlabelled (other portals may mean
something else by it).
"""

import re

UNKNOWN = {"", "keine angabe", "k. a.", "k.a.", "-", "–"}
_LINE = re.compile(r"^(Warmmiete|Gesamtmiete):\s*(.+)$", re.MULTILINE)
_AMOUNT = re.compile(r"^([0-9][0-9.,]*\s*€)\s*(.*)$")


def _first_values(details: str | None) -> dict:
    values: dict[str, str] = {}
    for label, value in _LINE.findall(details or ""):
        value = " ".join(value.split())
        if label not in values and value.lower() not in UNKNOWN and any(ch.isdigit() for ch in value):
            values[label] = value[:60]
    return values


def _same(a: str, b: str) -> bool:
    def digits(value: str) -> str:
        match = _AMOUNT.match(value)
        whole, _, cents = re.sub(r"[^0-9,]", "", match.group(1) if match else value).partition(",")
        return whole + "," + cents.rstrip("0")  # "1.077,00 €" = "1.077 €"
    def rest(value: str) -> str:
        match = _AMOUNT.match(value)
        return match.group(2).lower() if match else value.lower()
    return digits(a) == digits(b) and rest(a) == rest(b)


def _labelled(value: str, label: str) -> str:
    """'690 €' -> '690 € warm'; '900 € zzgl. Heizkosten' -> '900 € gesamt zzgl. Heizkosten'."""
    match = _AMOUNT.match(value)
    return f"{match.group(1)} {label} {match.group(2)}".strip() if match else f"{label} {value}"


def info(listing: dict) -> dict:
    """{"cold", "warm", "total"}: strings or None. total is None when it equals warm."""
    values = _first_values(listing.get("details"))
    warm, total = values.get("Warmmiete"), values.get("Gesamtmiete")
    if warm and total and _same(warm, total):
        total = None
    cold = " ".join(str(listing.get("price") or "").split()) or None
    return {"cold": cold, "warm": warm, "total": total}


def parts(listing: dict) -> list[str]:
    rent = info(listing)
    if not (rent["warm"] or rent["total"]):
        return [rent["cold"]] if rent["cold"] else []
    result = [f"{rent['cold']} kalt"] if rent["cold"] else []
    if rent["warm"]:
        result.append(_labelled(rent["warm"], "warm"))
    if rent["total"]:
        result.append(_labelled(rent["total"], "gesamt"))
    return result


def price_text(listing: dict) -> str:
    """'570 € kalt · 690 € warm', or just Fredy's price, or ''."""
    return " · ".join(parts(listing))
