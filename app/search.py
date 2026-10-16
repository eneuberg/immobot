"""Searching the stored listings (text only, never pictures): the search field on the listings page.

- keyword_search: free and instant. Every meaningful word of the query must appear in the listing's title, address,
  description or exposé text; spelling is normalized ("Montanusstraße" = "Montanusstr." = "montanus strasse").
- claude_search: for queries about meaning ("ruhig, mit Balkon, nah am Grünen", "bezahlbar für zwei, Altbau"). One
  call to the cheapest Claude model of the configured provider with a compact text catalogue of all listings;
  Claude returns the matching ids, best first, each with a short reason. Logged in llm_usage (purpose "search").
"""

import re
import unicodedata

from . import db, learning, settings

CATALOGUE_MAX = 400  # listings sent to Claude, newest first
ENTRY_TEXT_MAX = 600  # characters of description / exposé text per listing
QUERY_MAX = 300
RESULTS_MAX = 40
SEARCH_MODELS = {"anthropic": "claude-haiku-5-5", "deutschlandgpt": "claude-haiku-5.5"}

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


# --- Claude --------------------------------------------------------------------------------------------------

SYSTEM = (
    "Du hilfst bei der Wohnungssuche. Du bekommst eine Suchanfrage und einen Katalog gespeicherter Wohnungsinserate "
    "(nur Text). Finde die Inserate, die zur Anfrage passen – nach Bedeutung, nicht nur nach Wörtern: „ruhig“ passt "
    "zu „ruhige Seitenstraße“, „Freifläche“ zu Balkon, Terrasse oder Garten, „bezahlbar“ zum Preis im Verhältnis zu "
    "den anderen. Harte Angaben (Straße, Stadtteil, Zimmerzahl, Höchstpreis, Mindestgröße) müssen erfüllt sein. "
    "Erfinde nichts, was nicht im Inserat steht. Lieber wenige gute Treffer als viele unsichere; keine Treffer ist "
    "eine gültige Antwort. Sortiere die besten zuerst. Begründe jeden Treffer in einem kurzen deutschen Halbsatz "
    "(höchstens 15 Wörter), der sagt, was genau passt."
)
SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "why": {"type": "string"}},
                "required": ["id", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["matches"],
    "additionalProperties": False,
}
SHAPE = '{"matches": [{"id": 12, "why": "Montanusstraße, 3 Zimmer, Balkon"}]}'


def catalogue_entry(listing: dict) -> str:
    facts = " · ".join(str(listing[k]) for k in ("price", "size", "address") if str(listing.get(k) or "").strip())
    text = " ".join(str(listing.get("details") or listing.get("description") or "").split())[:ENTRY_TEXT_MAX]
    status = settings.STATUSES.get(listing.get("status"), listing.get("status") or "")
    return f"[{listing['id']}] {listing.get('title') or '(ohne Titel)'} | {facts} | Status: {status}\n{text}".rstrip()


def search_model(current: dict) -> str:
    provider = settings.provider_of(current)
    return SEARCH_MODELS.get(provider) or settings.model_of(current)


async def claude_search(query: str) -> list[dict]:
    """[{"listing": row, "why": reason}] best first. Raises llm.BudgetExceeded / llm.LLMError (German messages)."""
    query = " ".join(query.split())[:QUERY_MAX]
    listings = db.list_listings(limit=CATALOGUE_MAX)
    if not query or not listings:
        return []
    by_id = {listing["id"]: listing for listing in listings}
    current = db.get_settings()
    catalogue = "\n\n".join(catalogue_entry(listing) for listing in listings)
    data = await learning._ask_json(
        current, purpose="search", listing_id=None, system=SYSTEM,
        user=f"Suchanfrage: {query}\n\nKatalog ({len(listings)} Inserate):\n\n{catalogue}",
        schema=SCHEMA, shape=SHAPE, max_tokens=4000, effort="low", timeout=90.0, model=search_model(current))
    results, seen = [], set()
    for match in data.get("matches") or []:
        if not isinstance(match, dict):
            continue
        try:
            listing_id = int(match.get("id"))
        except (TypeError, ValueError):
            continue
        if listing_id in by_id and listing_id not in seen:
            seen.add(listing_id)
            results.append({"listing": by_id[listing_id], "why": " ".join(str(match.get("why") or "").split())[:200]})
    return results[:RESULTS_MAX]
