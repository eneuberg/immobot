"""Portal helpers: URL validation, contact deep links and display names per Fredy provider id."""

import re
from urllib.parse import urlsplit, urlunsplit

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


def provider_label(provider: str | None) -> str:
    if not provider:
        return "?"
    return PROVIDER_LABELS.get(provider, provider)
