"""Searching the stored listings (text only, never pictures): the search field on the listings page.

Free and instant: every meaningful word of the query must appear in the listing's title, address, description or
exposé text; spelling is normalized ("Montanusstraße" = "Montanusstr." = "montanus strasse").
"""

import re
import unicodedata

QUERY_MAX = 300

# Words that say nothing about a particular listing.
STOPWORDS = {
    "wohnung", "wohnungen", "inserat", "inserate", "zimmer", "die", "der", "das", "den", "dem", "des", "ein", "eine",
    "einer", "einem", "einen", "in", "im", "am", "an", "auf", "bei", "mit", "und", "oder", "von", "vom", "zu", "zur",
    "zum", "fur", "nach", "nahe", "nah", "ist", "hat", "gibt", "es", "mir", "ich", "alle", "welche", "wo", "suche",
    "zeig", "zeige",
}

_ABBREVIATIONS = [(re.compile(r"(strasse|str\b\.?)"), "str"), (re.compile(r"\bpl\b\.?"), "platz")]


def normalize(text: str | None) -> str:
    """Lowercase, umlauts and ß spelled out, "straße"/"str." -> "str", punctuation -> spaces."""
    text = (text or "").lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def query_terms(query: str) -> list[str]:
    """The words of a query that must match (no stopwords; "Montanus Straße" -> one term "montanusstr")."""
    words = normalize(query[:QUERY_MAX]).split()
    joined: list[str] = []
    for word in words:
        if word in ("str", "platz", "weg", "allee", "ring", "gasse") and joined:
            joined[-1] += word  # "montanus str" -> "montanusstr"
        else:
            joined.append(word)
    return [w for w in joined if w not in STOPWORDS and (len(w) >= 3 or w.isdigit())]


def haystack(listing: dict) -> str:
    parts = [listing.get(k) for k in ("title", "address", "price", "size", "description", "details", "contact_name")]
    text = normalize(" ".join(str(p) for p in parts if p))
    return text + " " + text.replace(" ", "")  # also finds "montanusstr" in "montanus str 5"


def keyword_search(query: str, listings: list[dict]) -> list[dict]:
    """Listings containing every query term, in the given order (newest first). [] for a query without terms."""
    terms = query_terms(query)
    if not terms:
        return []
    return [listing for listing in listings if all(term in haystack(listing) for term in terms)]
