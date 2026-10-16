"""ImmoScout24 app API: the exposé of a listing (details text and picture URLs).

Used by media.py when Fredy sends no pictures and the setting "is24_fetch" is on: one GET per listing (the
same request Fredy makes), then the pictures in the size variant IS24 serves itself (no custom resize
parameters, so the CDN cache hits), only from *.immobilienscout24.de. Downloading and storing is media.py's job.
"""

import re
from urllib.parse import urlsplit

import httpx

from .portals import safe_url

API_URL = "https://api.mobile.immobilienscout24.de/expose/{}"
APP_USER_AGENT = "ImmoScout_28.1_26.5.2_._"  # what Fredy sends to the same API
IMAGE_HOST_SUFFIX = ".immobilienscout24.de"
EXPOSE_TIMEOUT_S = 15.0
MAX_PICTURES = 30
DETAILS_MAX_CHARS = 20_000
FLOORPLAN_WORDS = ("grundriss", "floorplan", "floor plan")

_EXPOSE_PATH = re.compile(r"/expose/([0-9]{1,15})/?")
_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here


class Is24Error(Exception):
    """str(exc) is a short German message, safe to show."""


# --- URLs --------------------------------------------------------------------

def expose_id(url) -> str | None:
    """'https://www.immobilienscout24.de/expose/123456789' -> '123456789'; anything else -> None."""
    url = safe_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname.lower()
    if host != "immobilienscout24.de" and not host.endswith(".immobilienscout24.de"):
        return None
    match = _EXPOSE_PATH.fullmatch(parts.path)
    return match.group(1) if match else None


# An exposé link inside pasted text (the app's share text: "Schau dir das an: Titel https://…/expose/123?referrer=…").
# Same pattern as the preview in static/app.js.
_EXPOSE_IN_TEXT = re.compile(r"(?<![a-z0-9.-])(?:https?://)?(?:[a-z0-9-]+\.)*immobilienscout24\.de/expose/([0-9]{1,15})(?![0-9])",
                             re.IGNORECASE)


def expose_in_text(text) -> str | None:
    """The exposé id of the first ImmoScout24 exposé link in `text` (a bare link or shared text around one;
    query string, tracking parameters and the rest are ignored), else None."""
    match = _EXPOSE_IN_TEXT.search(str(text or "")[:5000])
    return match.group(1) if match else None


def canonical_url(expose_id: str) -> str:
    return f"https://www.immobilienscout24.de/expose/{expose_id}"


def is_supported(listing: dict) -> bool:
    """ImmoScout24 Germany only (Fredy's "immoscout"; the Austrian site is a different API)."""
    return listing.get("provider") == "immoscout" and expose_id(listing.get("url")) is not None


def allowed_image_url(url) -> bool:
    """https on *.immobilienscout24.de, and no "%WIDTH%x%HEIGHT%" template (only sizes IS24 serves itself)."""
    url = safe_url(url)
    if url is None or "%" in url:
        return False
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname.lower().endswith(IMAGE_HOST_SUFFIX) and parts.port in (None, 443)


# --- exposé ------------------------------------------------------------------

async def fetch_expose(expose_id: str) -> dict:
    """One GET to the IS24 app API. Raises Is24Error."""
    if not re.fullmatch(r"[0-9]{1,15}", expose_id or ""):
        raise Is24Error("Ungültige Exposé-ID.")
    headers = {"User-Agent": APP_USER_AGENT, "Accept": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=EXPOSE_TIMEOUT_S, follow_redirects=False, headers=headers,
                                     transport=_transport) as client:
            response = await client.get(API_URL.format(expose_id))
    except httpx.HTTPError as exc:
        raise Is24Error(f"Exposé nicht erreichbar ({type(exc).__name__}).") from None
    if response.status_code != 200:
        raise Is24Error(f"Exposé nicht abrufbar (HTTP {response.status_code}).")
    try:
        data = response.json()
    except ValueError:
        raise Is24Error("Exposé-Antwort ist kein JSON.") from None
    if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
        raise Is24Error("Exposé hat ein unerwartetes Format.")
    return data


def _clean(value) -> str:
    """One line, no non-breaking spaces."""
    return " ".join(str(value).split()) if value is not None else ""


def _attribute_lines(items) -> list[str]:
    lines = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        label = _clean(item.get("label")).rstrip(":")
        if not label:
            continue
        if item.get("type") == "CHECK":  # a ticked feature such as "Balkon/Terrasse"
            lines.append(f"{label}: ja")
        elif item.get("text") not in (None, ""):
            lines.append(f"{label}: {_clean(item['text'])}")
    return lines


def _pictures(items) -> list[dict]:
    """PICTURE items only (no ads), floor plans first, then IS24's order; numbered from 1."""
    pictures = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("type") != "PICTURE":
            continue
        url = next((u for u in (item.get("fullImageUrl"), item.get("previewImageUrl")) if allowed_image_url(u)), None)
        if url is None:
            continue
        caption = _clean(item.get("caption"))[:100]
        pictures.append({"caption": caption, "url": url,
                         "is_floorplan": any(word in caption.lower() for word in FLOORPLAN_WORDS)})
    pictures.sort(key=lambda p: not p["is_floorplan"])  # stable sort
    pictures = pictures[:MAX_PICTURES]
    for position, picture in enumerate(pictures, start=1):
        picture["position"] = position
    return pictures


def parse_expose(data: dict) -> dict:
    """{details_text, contact_name, media: [{position, caption, url, is_floorplan}]} from the exposé JSON.
    The details text has the key facts, all attribute lists and text sections and "Anbieter: …";
    phone numbers and the agent's address are left out."""
    blocks: list[str] = []
    media: list[dict] = []
    name = company = ""
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        kind, title = section.get("type"), _clean(section.get("title"))
        if kind == "TOP_ATTRIBUTES":
            lines = _attribute_lines(section.get("attributes"))
            if lines:
                blocks.append("\n".join(lines))
        elif kind == "MAP":
            address = ", ".join(filter(None, [_clean(section.get("addressLine1")), _clean(section.get("addressLine2"))]))
            if address:
                blocks.append("Adresse: " + address)
        elif kind == "ATTRIBUTE_LIST":
            lines = _attribute_lines(section.get("attributes"))
            if lines:
                blocks.append((f"## {title}\n" if title else "") + "\n".join(lines))
        elif kind == "TEXT_AREA":
            text = str(section.get("text") or "").replace("\xa0", " ").strip()
            if text:
                blocks.append((f"## {title}\n" if title else "") + text)
        elif kind == "AGENTS_INFO":
            name, company = _clean(section.get("name")), _clean(section.get("company"))
        elif kind == "MEDIA":
            media = _pictures(section.get("media"))
    if name or company:
        blocks.append("Anbieter: " + ", ".join(filter(None, [name, company])))
    return {"details_text": "\n\n".join(blocks)[:DETAILS_MAX_CHARS] or None, "contact_name": name or None, "media": media}


def listing_fields(data: dict) -> dict:
    """{title, price, size, address, description} as Fredy would send them, from the exposé JSON (a listing added
    by hand, scripts/test_listing.py). Missing values are ""; the description is the first text, cut to 500."""
    fields = {"title": "", "price": "", "size": "", "address": "", "description": ""}
    for section in data.get("sections") or []:
        if not isinstance(section, dict):
            continue
        kind = section.get("type")
        if kind == "TITLE":
            fields["title"] = _clean(section.get("title"))[:300]
        elif kind == "TOP_ATTRIBUTES":
            for item in section.get("attributes") if isinstance(section.get("attributes"), list) else []:
                label = _clean(item.get("label")) if isinstance(item, dict) else ""
                if label.startswith("Kaltmiete") and not fields["price"]:
                    fields["price"] = _clean(item.get("text"))[:60]
                elif label.startswith("Wohnfläche") and not fields["size"]:
                    fields["size"] = _clean(item.get("text"))[:60]
        elif kind == "MAP":
            fields["address"] = ", ".join(filter(None, [_clean(section.get("addressLine1")),
                                                        _clean(section.get("addressLine2"))]))[:200]
        elif kind == "TEXT_AREA" and not fields["description"]:
            fields["description"] = str(section.get("text") or "").replace("\xa0", " ").strip()[:500]
    return fields
