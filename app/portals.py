"""Portal helpers: URL validation, contact deep links, links into Fredy and display names per Fredy provider id."""

import os
import re
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit

# Fredy provider id -> display name. Unknown ids are shown as-is.
PROVIDER_LABELS = {
    "immoscout": "ImmoScout24",
    "immoscoutAt": "ImmoScout24 (AT)",
    "wgGesucht": "WG-Gesucht",
    "kleinanzeigen": "Kleinanzeigen",
    "immowelt": "Immowelt",
    "immonet": "Immonet",
    "ohneMakler": "Ohne-Makler",
    "regionalimmobilien24": "Regionalimmobilien24",
    "sparkasse": "Sparkasse",
    "mcMakler": "McMakler",
    "neubauKompass": "NeubauKompass",
    "inberlinwohnen": "inberlinwohnen",
    "immobilienDe": "Immobilien.de",
    "einsAImmobilien": "1A-Immobilienmarkt",
    "wohnungsboerse": "Wohnungsbörse",
    "immoswp": "Immo SWP",
}

# Portals whose Fredy job sends only to immobot (no Fredy Telegram message): immobot posts the pictures and the
# message itself, marked with the badge; each can be switched off with its setting.
OWN_POST_PROVIDERS = {"kleinanzeigen": "kleinanzeigen_enabled"}
PROVIDER_BADGES = {"kleinanzeigen": "🟢 Kleinanzeigen"}

_IS24_EXPOSE_PATH = re.compile(r"/expose/\d+/?")
_WG_LISTING_PATH = re.compile(r"/[^/]+\.html")


def safe_url(url: str | None) -> str | None:
    """Return the URL if it is a plain http(s) link with a host, else None.

    Used for Telegram URL buttons and dashboard hrefs, so javascript:, data:, relative
    links, credentials in the URL and whitespace/control characters are all rejected.
    """
    if not isinstance(url, str):
        return None
    url = url.strip()
    if not url or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises ValueError for a malformed port
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    if "@" in parts.netloc:  # https://immobilienscout24.de@evil.example
        return None
    return url


def contact_url(provider: str | None, url: str | None) -> str | None:
    """Deep link to the portal's contact form where we know one, else the listing URL."""
    url = safe_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname.lower()

    if provider == "immoscout" and host.endswith("immobilienscout24.de") and _IS24_EXPOSE_PATH.fullmatch(parts.path):
        return urlunsplit(parts._replace(fragment="/basicContact/email"))
    if provider == "wgGesucht" and host.endswith("wg-gesucht.de") and _WG_LISTING_PATH.fullmatch(parts.path):
        return urlunsplit(parts._replace(path="/nachricht-senden" + parts.path))
    if provider == "mcMakler" and host.endswith("mcmakler.de"):
        return urlunsplit(parts._replace(fragment="expose-section-anfrage"))
    return url


def fredy_url(listing: dict, base_url: str | None) -> str | None:
    """"In Fredy öffnen": with the address of the Fredy instance (setting "fredy_base_url") built the way Fredy
    builds its own links (lib/notification/adapter/http.js), so the links survive a move of Fredy. Otherwise,
    and for listings that didn't come from a Fredy job (no job_id), the link Fredy sent. Always via safe_url."""
    base = safe_url(base_url)
    if base and listing.get("fredy_id") and listing.get("job_id"):
        return safe_url(f"{base.rstrip('/')}/#/listings/listing/{quote(str(listing['fredy_id']), safe='')}")
    return safe_url(listing.get("fredy_url"))


def provider_label(provider: str | None) -> str:
    if not provider:
        return "?"
    return PROVIDER_LABELS.get(provider, provider)


# --- map links (listing page: "🗺 Karte" and "In Google Maps öffnen") -----------------------------------------
# Listings only carry a free-text address ("Venloer Straße 123, 50823 Köln, Ehrenfeld", "Ehrenfeld, Köln",
# "50823 Köln - Ehrenfeld"), no coordinates: Google searches for it. All searches are in one city (env MAPS_CITY,
# default Köln; empty = none), so an address without the city gets ", <city>" appended (else "Ehrenfeld" alone may
# land anywhere).
MAPS_CITY = os.environ.get("MAPS_CITY", "Köln").strip()
MAPS_QUERY_MAX = 200
_CITY_WORDS = ("köln", "koeln", "cologne") if MAPS_CITY == "Köln" else ((MAPS_CITY.lower(),) if MAPS_CITY else ())


def maps_query(address: str | None) -> str | None:
    """The address as a Google Maps search text, or None without an address."""
    if not isinstance(address, str):
        return None
    text = " ".join(unicodedata.normalize("NFC", address).split())[:MAPS_QUERY_MAX].strip()
    if not text:
        return None
    if MAPS_CITY and not any(word in text.lower() for word in _CITY_WORDS):
        text += f", {MAPS_CITY}"
    return text


def maps_url(address: str | None) -> str | None:
    """"In Google Maps öffnen": a search link (on Android it opens the Maps app)."""
    query = maps_query(address)
    return f"https://www.google.com/maps/search/?api=1&query={quote(query, safe='')}" if query else None


def maps_embed_url(address: str | None) -> str | None:
    """The key-less Google embed for the iframe behind "🗺 Karte" (set only when the map is opened, app.js).
    Its hosts are the ones allowed in the CSP's frame-src (web.CSP)."""
    query = maps_query(address)
    return f"https://maps.google.com/maps?q={quote(query, safe='')}&z=15&output=embed" if query else None
