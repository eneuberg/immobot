"""Claude drafting: build the prompt, call the API, compute the cost and store the draft.

create_and_store_draft() is the single entry point for the Telegram bot and the dashboard.

Two providers (settings.PROVIDERS), both through the Anthropic SDK:
- Anthropic: structured outputs (JSON schema) and server-side refusal fallbacks.
- DeutschlandGPT: its Anthropic-compatible endpoint. It ignores the JSON schema on most models, so the
  system prompt asks for a delimiter format and parse_reply() reads that tolerantly. No betas there.
API keys come from vault.py and are read on every call, so a new key works without a restart.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import anthropic
import httpx

from . import db, images, media, vault
from .portals import provider_label, safe_url
from .settings import (
    DEFAULT_SYSTEM_PROMPT, DRAFT_KINDS, EFFORTS, PRICES, PROVIDERS, as_bool, as_float, as_int, cost, fmt_eur, fmt_money,
    model_of, provider_of, to_usd,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 8000  # thinking + reply; a draft is a few hundred tokens
DEFAULT_EFFORT = "low"
# Server-side refusal fallback (Anthropic only): on a policy decline the API re-runs the request on
# Anthropic's recommended fallback model. Only these models support the "default" form.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
MODEL_LIST_MAX = 500
MAX_IMAGES_LIMIT = 20  # upper bound of the llm_max_images setting

# Kinds that rework the latest stored draft.
VARIANT_KINDS = ("regenerate", "shorter", "formal", "custom")
INSTRUCTION_MAX_CHARS = 500
DESCRIPTION_MAX_CHARS = 12000

IMAGE_MAX_BYTES = 10 * 1024 * 1024
IMAGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_image_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here

DRAFT_SCHEMA = {
    "type": "object",
    "properties": {
        "draft": {"type": "string", "description": "Der fertige Nachrichtentext an die Vermieterseite."},
        "hint": {"type": "string", "description": "Kurzer Hinweis für die suchende Person, oder leer."},
    },
    "required": ["draft", "hint"],
    "additionalProperties": False,
}

# For providers without structured outputs (DeutschlandGPT): appended to the system prompt.
DRAFT_MARK, HINT_MARK = "<<<ENTWURF>>>", "<<<HINWEIS>>>"
DELIMITER_INSTRUCTION = (
    "Antworte exakt in diesem Format, ohne Zeichen davor oder danach:\n"
    + DRAFT_MARK + "\n(Nachrichtentext)\n" + HINT_MARK + "\n(kurzer Hinweis oder leer)"
)

_clients: dict[str, anthropic.AsyncAnthropic] = {}  # provider -> client for its current key, see _get_client()


class LLMError(Exception):
    """Generation failed; str(exc) is a short German message safe to show the user."""


class BudgetExceeded(LLMError):
    pass


@dataclass
class DraftResult:
    text: str
    hint: str | None
    model: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float  # counts toward budget and totals
    provider: str = "anthropic"
    currency: str = "USD"
    cost_native: float | None = None  # what the provider bills, in `currency` (None: same as cost_usd)


def max_images(settings: dict) -> int:
    """How many stored listing pictures go to the model (setting llm_max_images, 0-20)."""
    return min(max(as_int(settings.get("llm_max_images"), 10), 0), MAX_IMAGES_LIMIT)


def no_key_message(provider: str) -> str:
    return "Kein " + PROVIDERS[provider]["label"] + "-API-Key hinterlegt – im Dashboard unter Zugänge eintragen."


def _get_client(provider: str) -> anthropic.AsyncAnthropic:
    """Client for the provider's current API key; recreated when the key was changed in the dashboard."""
    info = PROVIDERS[provider]
    api_key = vault.get(info["credential"])
    if not api_key:
        raise LLMError(no_key_message(provider))
    client = _clients.get(provider)
    if client is None or client.api_key != api_key:
        # The old client (if any) is left to the garbage collector: a running request may still use it.
        client = anthropic.AsyncAnthropic(api_key=api_key, base_url=info["base_url"], timeout=120.0, max_retries=2)
        _clients[provider] = client
    return client


def _api_error(provider: str, exc: Exception, context: str) -> LLMError:
    """Map an SDK exception to a short German message naming the provider (details go to the log only)."""
    label = PROVIDERS[provider]["label"]
    if isinstance(exc, anthropic.RateLimitError):
        log.warning("%s rate limit (%s)", provider, context)
        return LLMError(label + "-Rate-Limit erreicht – bitte in einer Minute nochmal versuchen.")
    if isinstance(exc, anthropic.APIStatusError):
        log.warning("%s API error %s (%s, request_id=%s): %s", provider, exc.status_code, context, exc.request_id, exc.message)
        if exc.status_code in (401, 403):
            return LLMError(label + "-API-Key ungültig oder ohne Berechtigung.")
        if exc.status_code == 402:
            return LLMError(label + "-Guthaben oder Key-Limit aufgebraucht – im " + label + "-Dashboard aufladen.")
        if exc.status_code >= 500:
            return LLMError(label + " ist gerade überlastet oder gestört (HTTP " + str(exc.status_code)
                            + ") – bitte gleich nochmal versuchen.")
        return LLMError(label + "-Anfrage abgelehnt (HTTP " + str(exc.status_code) + ") – Details im Log.")
    if isinstance(exc, anthropic.APITimeoutError):
        log.warning("%s timeout (%s)", provider, context)
        return LLMError(label + " hat nicht rechtzeitig geantwortet – bitte nochmal versuchen.")
    log.warning("%s connection error (%s)", provider, context)
    return LLMError("Keine Verbindung zu " + label + " – bitte später nochmal versuchen.")


async def check_api_key(provider: str) -> str:
    """Cheapest authenticated call (list one model). Returns a short German OK message, raises LLMError."""
    client = _get_client(provider).with_options(timeout=15.0, max_retries=0)
    try:
        await client.models.list(limit=1)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        raise _api_error(provider, exc, "key check") from None
    return PROVIDERS[provider]["label"] + "-API-Key funktioniert."


async def list_models(provider: str) -> list[str]:
    """All model ids the provider offers for this key (live). Raises LLMError."""
    client = _get_client(provider).with_options(timeout=20.0, max_retries=1)
    ids: list[str] = []
    try:
        async for model in client.models.list(limit=1000):
            ids.append(model.id)
            if len(ids) >= MODEL_LIST_MAX:
                break
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        raise _api_error(provider, exc, "model list") from None
    return ids


def _models_key(provider: str) -> str:
    return "models_cache:" + provider


async def refresh_models(provider: str) -> list[str]:
    """Fetch the live model list and cache it in meta (also a failure, so the page can say why).
    Raises LLMError after caching the failure."""
    try:
        ids = await list_models(provider)
    except Exception as exc:
        if isinstance(exc, LLMError):
            message = str(exc)
        else:  # e.g. an unexpected response shape; type only, the message might quote the response
            log.warning("model list for %s failed: %s", provider, type(exc).__name__)
            message = "Unerwarteter Fehler beim Laden der Modellliste – Details im Log."
        db.set_meta(_models_key(provider), json.dumps({"fetched_at": db.now_iso(), "models": [], "error": message}))
        raise LLMError(message) from None
    db.set_meta(_models_key(provider), json.dumps({"fetched_at": db.now_iso(), "models": ids, "error": None}))
    return ids


def cached_models(provider: str) -> dict | None:
    """{"fetched_at", "models", "error"} of the last fetch, or None if there was none (or it's unreadable)."""
    raw = db.get_meta(_models_key(provider))
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return None
    return data


# --- prompt -----------------------------------------------------------------
# Settings and listing texts are user-editable or foreign data and may contain braces,
# so everything is joined with plain string concatenation (never str.format).

def build_system_prompt(settings: dict) -> str:
    style = (settings.get("style") or "").strip()
    target_words = as_int(settings.get("target_words"), 150)
    if target_words > 0:
        style = (style + "\n" if style else "") + "Ziellänge: ca. " + str(target_words) + " Wörter."
    sections = [
        (settings.get("system_prompt") or "").strip() or DEFAULT_SYSTEM_PROMPT,
        "## Profil der suchenden Person\n" + ((settings.get("profile") or "").strip() or "(keine Angaben)"),
        "## Wünsche\n" + ((settings.get("wishes") or "").strip() or "(keine Angaben)"),
        "## Schreibstil\n" + (style or "(keine Angaben)"),
    ]
    return "\n\n".join(sections)


def build_user_text(listing: dict, kind: str, previous_text: str | None = None, instruction: str | None = None) -> str:
    fields = [
        ("Titel", listing.get("title")),
        ("Preis", listing.get("price")),
        ("Größe", listing.get("size")),
        ("Adresse", listing.get("address")),
        ("Portal", provider_label(listing.get("provider")) if listing.get("provider") else None),
        ("Link", listing.get("url")),
    ]
    if listing.get("contact_name"):
        fields.append(("Ansprechperson", listing["contact_name"]))
    lines = [name + ": " + str(value).strip() for name, value in fields if value and str(value).strip()]
    # The full ImmoScout24 exposé text when we have it (media.py), else Fredy's description.
    description = (listing.get("details") or listing.get("description") or "").strip()[:DESCRIPTION_MAX_CHARS]
    lines.append("Beschreibung:\n" + (description or "(keine Beschreibung)"))
    parts = ["<inserat>\n" + "\n".join(lines) + "\n</inserat>"]

    previous_text = (previous_text or "").strip() or None
    if kind in VARIANT_KINDS and previous_text:
        parts.append("<bisheriger_entwurf>\n" + previous_text + "\n</bisheriger_entwurf>")
    parts.append("## Aufgabe\n" + _task(kind, previous_text, (instruction or "").strip()))
    return "\n\n".join(parts)


def _task(kind: str, previous_text: str | None, instruction: str) -> str:
    anweisung = "<anweisung>\n" + instruction + "\n</anweisung>"
    keep = "Alles, was das Inserat ausdrücklich verlangt (z. B. Codewort, bestimmte Angaben, Betreff), bleibt erhalten."
    first = "Schreib die erste Kontaktanfrage auf dieses Inserat."

    if previous_text:
        if kind == "regenerate":
            return ("Schreib eine neue Version der Kontaktanfrage, die sich deutlich vom bisherigen Entwurf unterscheidet "
                    "(anderer Einstieg, anderer Aufbau, andere Formulierungen). Die Regeln oben gelten weiter.")
        if kind == "shorter":
            words = len(previous_text.split())
            return ("Kürze den bisherigen Entwurf auf etwa 60 % seiner Länge (bisher ca. " + str(words) + " Wörter, Ziel ca. "
                    + str(round(words * 0.6)) + " Wörter). " + keep
                    + " Die Bitte um einen Besichtigungstermin sowie Gruß und Kontaktdaten bleiben ebenfalls.")
        if kind == "formal":
            return ("Formuliere den bisherigen Entwurf förmlicher und distanzierter: Sie-Form, sachlich-höflicher Ton, "
                    "keine lockeren oder umgangssprachlichen Wendungen. Inhalt und Länge bleiben ungefähr gleich. " + keep)
        if kind == "custom":
            return ("Überarbeite den bisherigen Entwurf nach dieser Anweisung der suchenden Person:\n" + anweisung
                    + "\nÄndere nur, was die Anweisung verlangt; der Rest bleibt möglichst wie er ist.")

    # initial, test, or a variant without a stored draft: write a fresh one.
    if kind == "shorter":
        return first + " Halte sie deutlich kürzer als die Ziellänge."
    if kind == "formal":
        return first + " Formuliere besonders förmlich und distanziert (Sie-Form)."
    if kind == "custom" and instruction:
        return first + " Berücksichtige dabei diese Anweisung der suchenden Person:\n" + anweisung
    return first


# --- listing image ----------------------------------------------------------

async def fetch_image_block(image_url: str | None) -> dict | None:
    """Download the listing image and return it as a base64 JPEG content block (max 1024 px).

    Any failure is logged and returns None, so the draft is generated without the image.
    """
    url = safe_url(image_url)
    if url is None:
        return None
    try:
        jpeg = images.to_jpeg(await _download(url))
    except Exception as exc:  # the image is optional; never fail the draft because of it
        if isinstance(exc, httpx.HTTPStatusError):
            reason = "HTTP " + str(exc.response.status_code)
        elif isinstance(exc, ValueError):
            reason = str(exc)
        else:
            reason = type(exc).__name__
        log.warning("listing image skipped (host=%s): %s", urlsplit(url).hostname, reason)
        return None
    data = base64.standard_b64encode(jpeg).decode("ascii")
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


async def _download(url: str) -> bytes:
    async with httpx.AsyncClient(
        timeout=10.0, follow_redirects=True, headers={"User-Agent": IMAGE_USER_AGENT}, transport=_image_transport
    ) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > IMAGE_MAX_BYTES:
                    raise ValueError("image larger than 10 MB")
                chunks.append(chunk)
    return b"".join(chunks)


# --- Claude call ------------------------------------------------------------

def picture_blocks(pictures: list[tuple[dict, bytes]]) -> list[dict]:
    """Stored listing pictures as content blocks, each labelled "Bild 1: Balkon" right before the image."""
    blocks = []
    for number, (row, jpeg) in enumerate(pictures, start=1):
        label = "Bild " + str(number) + (": " + row["caption"] if row.get("caption") else "")
        blocks.append({"type": "text", "text": label})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                   "data": base64.standard_b64encode(jpeg).decode("ascii")}})
    return blocks


async def generate_draft(
    listing: dict, settings: dict, kind: str, previous_text: str | None = None, instruction: str | None = None,
    pictures: list[tuple[dict, bytes]] | None = None,
) -> DraftResult:
    """Ask Claude (through the configured provider) for one draft. No database writes.
    `pictures`: the stored ImmoScout24 pictures (row, JPEG), already limited to llm_max_images (may be empty).
    None means there are none for this listing, and Fredy's single image is used instead."""
    provider = provider_of(settings)
    client = _get_client(provider)
    model = model_of(settings)
    effort = settings.get("effort") if settings.get("effort") in EFFORTS else DEFAULT_EFFORT

    content = []
    if as_bool(settings.get("send_image")):
        if pictures is not None:
            content += picture_blocks(pictures[:max_images(settings)])
        else:
            image = await fetch_image_block(listing.get("image_url"))
            if image:
                content.append(image)
    content.append({"type": "text", "text": build_user_text(listing, kind, previous_text, instruction)})

    structured = provider == "anthropic"  # DeutschlandGPT ignores output_config.format on most models
    system = build_system_prompt(settings)
    output_config: dict = {"effort": effort}
    if structured:
        output_config["format"] = {"type": "json_schema", "schema": DRAFT_SCHEMA}
    else:
        system += "\n\n" + DELIMITER_INSTRUCTION
    params = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": output_config,
    }
    try:
        if provider == "anthropic" and model in FALLBACK_MODELS:
            response = await client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            response = await client.messages.create(**params)
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as exc:
        raise _api_error(provider, exc, "model=" + model) from None

    return _to_result(response, model, provider, as_float(settings.get("eur_usd_rate"), 1.10))


def _to_result(response, requested_model: str, provider: str = "anthropic", eur_usd_rate: float = 1.10) -> DraftResult:
    # Check stop_reason before reading content: a refusal has empty or partial content.
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        log.warning("claude refused the draft (model=%s, category=%s)", response.model, category)
        raise LLMError("Claude hat diesen Entwurf abgelehnt.")
    if response.stop_reason == "max_tokens":
        log.warning("claude hit max_tokens (model=%s)", response.model)
        raise LLMError("Die Antwort wurde abgeschnitten – bitte nochmal versuchen.")

    text = "".join(block.text for block in response.content if block.type == "text")  # thinking blocks ignored
    if provider == "anthropic":  # structured outputs: exactly our JSON schema
        try:
            data = json.loads(text)
            draft = data["draft"].strip()
            hint = (data.get("hint") or "").strip() or None
        except (ValueError, KeyError, TypeError, AttributeError):
            log.warning("claude returned unparseable output (model=%s, stop_reason=%s)", response.model, response.stop_reason)
            raise LLMError("Claude hat keine lesbare Antwort geliefert.")
        if not draft:
            raise LLMError("Claude hat einen leeren Entwurf geliefert.")
    else:
        draft, hint, how = parse_reply(text)
        log.info("%s reply parsed as %s (model=%s)", provider, how, response.model)

    # response.model is the model that actually answered (differs after a server-side fallback). A name the
    # price table doesn't know is priced as the requested model.
    model = response.model or requested_model
    usage = response.usage
    input_tokens = usage.input_tokens or 0
    output_tokens = usage.output_tokens or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    priced_as = model if model in PRICES[provider] else requested_model
    currency = PROVIDERS[provider]["currency"]
    native = cost(provider, priced_as, input_tokens, output_tokens, cache_read, cache_write)
    return DraftResult(
        text=draft,
        hint=hint,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=to_usd(native, currency, eur_usd_rate),
        provider=provider,
        currency=currency,
        cost_native=native,
    )


# --- tolerant reply parsing (providers without structured outputs) ------------

_FENCED = re.compile(r"```[a-zA-Z]*[ \t]*\n?(.*?)```", re.S)
_MD_LABEL = re.compile(  # "**draft**", "**Entwurf:**", "## Hinweis", "hint:" at the start of a line
    r"^[ \t]*(?:#{1,6}[ \t]*(draft|entwurf|hint|hinweis)\b[ \t]*:?"
    r"|\*\*[ \t]*(draft|entwurf|hint|hinweis)[ \t]*:?[ \t]*\*\*[ \t]*:?"
    r"|(draft|entwurf|hint|hinweis)[ \t]*:)",
    re.I | re.M,
)
_EMPTY_HINTS = {"", "leer", "-", "–", "keiner", "kein hinweis", "keine", "none", "n/a"}


def _clean_hint(value: str | None) -> str | None:
    value = _strip_fence((value or "").strip())
    return None if value.strip("()[]. ").lower() in _EMPTY_HINTS else value


def _strip_fence(value: str) -> str:
    match = _FENCED.fullmatch(value.strip())
    return match.group(1).strip() if match else value.strip()


def _json_object(text: str) -> dict | None:
    """The first JSON object in the text: inside a ``` fence, or from the first "{" to the last "}"."""
    candidates = [m.group(1) for m in _FENCED.finditer(text)]
    if "{" in text and "}" in text:
        candidates.append(text[text.index("{"): text.rindex("}") + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _markdown_sections(text: str) -> tuple[str | None, str | None]:
    """Draft and hint from "**draft** ... **hint** ..." style answers (first section of each kind)."""
    found: dict[str, str] = {}
    matches = list(_MD_LABEL.finditer(text))
    for index, match in enumerate(matches):
        label = next(group for group in match.groups() if group).lower()
        kind = "draft" if label in ("draft", "entwurf") else "hint"
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        found.setdefault(kind, text[match.end():end].strip())
    return found.get("draft"), found.get("hint")


def parse_reply(text: str) -> tuple[str, str | None, str]:
    """(draft, hint, how) from a reply without structured outputs. Tries, in this order: our delimiter
    format, a JSON object (also inside ``` fences or surrounded by text), markdown "**draft**"/"**hint**"
    sections, and finally the whole text as the draft. Only an empty reply is an error."""
    text = (text or "").strip()
    if not text:
        raise LLMError("Claude hat einen leeren Entwurf geliefert.")
    if DRAFT_MARK in text:
        draft, _, hint = text.split(DRAFT_MARK, 1)[1].partition(HINT_MARK)
        if _strip_fence(draft):
            return _strip_fence(draft), _clean_hint(hint), "delimiter"
    data = _json_object(text)
    if data is not None and isinstance(data.get("draft"), str) and data["draft"].strip():
        return data["draft"].strip(), _clean_hint(str(data.get("hint") or "")), "json"
    draft, hint = _markdown_sections(text)
    if draft:
        return _strip_fence(draft), _clean_hint(hint), "markdown"
    plain = text.replace(DRAFT_MARK, "").replace(HINT_MARK, "").strip()
    return plain or text, None, "plain"


# --- entry point ------------------------------------------------------------

async def create_and_store_draft(listing_id: int, kind: str, instruction: str | None = None) -> dict:
    """Generate a draft for a listing, store it and return the stored drafts row.

    Raises BudgetExceeded when the monthly budget is used up, LLMError for everything else
    the user should see (both carry a short German message).
    """
    if kind not in DRAFT_KINDS:
        raise LLMError("Unbekannte Entwurfsart.")
    listing = db.get_listing(listing_id)
    if listing is None:
        raise LLMError("Inserat nicht gefunden.")
    instruction = (instruction or "").strip()[:INSTRUCTION_MAX_CHARS] or None
    if kind == "custom" and not instruction:
        raise LLMError("Keine Anweisung angegeben.")

    settings = db.get_settings()
    budget = as_float(settings.get("monthly_budget_eur"), 0.0)  # euros, like db.cost_this_month()
    if budget > 0 and db.cost_this_month() >= budget:
        raise BudgetExceeded("Monatsbudget von " + fmt_eur(budget) + " erreicht – im Dashboard erhöhen.")

    previous_text = None
    if kind in VARIANT_KINDS:
        previous = db.latest_draft(listing_id)
        previous_text = previous["text"] if previous else None

    # Pictures from Fredy or ImmoScout24 (media.py), fetched once, then from the local copies. [] when there are
    # none or nothing could be fetched - then Fredy's single image is used (pictures=None).
    want_images = as_bool(settings.get("send_image")) and max_images(settings) > 0
    rows = await media.ensure_media(listing_id, want_images=want_images)
    listing = db.get_listing(listing_id) or listing  # may have gained the ImmoScout24 details text
    pictures = media.read_pictures(listing_id, rows[:max_images(settings)]) if rows else None

    result = await generate_draft(listing, settings, kind, previous_text, instruction, pictures)
    draft_id = db.add_draft(
        listing_id,
        kind,
        result.text,
        result.model,
        hint=result.hint,
        instruction=instruction,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cache_read_tokens=result.cache_read_tokens,
        cache_write_tokens=result.cache_write_tokens,
        cost_usd=result.cost_usd,
        provider=result.provider,
        currency=result.currency,
        cost_native=result.cost_native,
    )
    log.info("draft listing=%s kind=%s provider=%s model=%s tokens=%s/%s cost=%s",
             listing_id, kind, result.provider, result.model, result.input_tokens, result.output_tokens,
             fmt_money(result.cost_usd if result.cost_native is None else result.cost_native, result.currency))

    # Re-read the status: it may have changed (e.g. "sent") while Claude was writing.
    if kind != "test":
        current = db.get_listing(listing_id)
        if current and current["status"] == "new":
            db.set_listing_status(listing_id, "drafted")

    return next(d for d in db.list_drafts(listing_id) if d["id"] == draft_id)
