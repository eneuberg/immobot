"""Claude drafting: build the prompt, call the API, compute the cost and store the draft.

create_and_store_draft() is the single entry point for the Telegram bot and the dashboard.
"""

import base64
import io
import json
import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import anthropic
import httpx
from PIL import Image

from . import db
from .portals import provider_label, safe_url
from .settings import (
    DEFAULT_MODEL, DEFAULT_SYSTEM_PROMPT, DRAFT_KINDS, EFFORTS, MODELS, as_bool, as_float, as_int, cost_usd, fmt_usd,
)

log = logging.getLogger(__name__)

MAX_TOKENS = 8000  # thinking + reply; a draft is a few hundred tokens
DEFAULT_EFFORT = "low"
# Server-side refusal fallback: on a policy decline the API re-runs the request on Anthropic's
# recommended fallback model. Claude Haiku 5.5 has no server-side fallback.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}

# Kinds that rework the latest stored draft.
VARIANT_KINDS = ("regenerate", "shorter", "formal", "custom")
INSTRUCTION_MAX_CHARS = 500
DESCRIPTION_MAX_CHARS = 12000

IMAGE_MAX_BYTES = 10 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000  # refuse decompression bombs before decoding
IMAGE_MAX_SIDE = 1024
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

_client: anthropic.AsyncAnthropic | None = None


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
    cost_usd: float


def configure(api_key: str) -> None:
    global _client
    _client = anthropic.AsyncAnthropic(api_key=api_key, timeout=120.0, max_retries=2)


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
    lines = [name + ": " + str(value).strip() for name, value in fields if value and str(value).strip()]
    description = (listing.get("description") or "").strip()[:DESCRIPTION_MAX_CHARS]
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
        jpeg = _shrink_to_jpeg(await _download(url))
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


def _shrink_to_jpeg(data: bytes) -> bytes:
    with Image.open(io.BytesIO(data)) as src:
        if src.width * src.height > IMAGE_MAX_PIXELS:
            raise ValueError("image has too many pixels")
        src.draft("RGB", (IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))  # cheap downscale while decoding JPEGs
        img = src.convert("RGB")
    img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


# --- Claude call ------------------------------------------------------------

async def generate_draft(
    listing: dict, settings: dict, kind: str, previous_text: str | None = None, instruction: str | None = None
) -> DraftResult:
    """Ask Claude for one draft. No database writes."""
    if _client is None:
        raise LLMError("Claude ist nicht konfiguriert.")
    model = settings.get("model") if settings.get("model") in MODELS else DEFAULT_MODEL
    effort = settings.get("effort") if settings.get("effort") in EFFORTS else DEFAULT_EFFORT

    content = []
    if as_bool(settings.get("send_image")):
        image = await fetch_image_block(listing.get("image_url"))
        if image:
            content.append(image)
    content.append({"type": "text", "text": build_user_text(listing, kind, previous_text, instruction)})

    params = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": build_system_prompt(settings),
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": DRAFT_SCHEMA}},
    }
    try:
        if model in FALLBACK_MODELS:
            response = await _client.beta.messages.create(**params, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            response = await _client.messages.create(**params)
    except anthropic.RateLimitError:
        log.warning("claude rate limit (model=%s)", model)
        raise LLMError("Claude-Rate-Limit erreicht – bitte in einer Minute nochmal versuchen.")
    except anthropic.APIStatusError as exc:
        log.warning("claude API error %s (model=%s, request_id=%s): %s", exc.status_code, model, exc.request_id, exc.message)
        if exc.status_code in (401, 403):
            raise LLMError("Claude-API-Key ungültig oder ohne Berechtigung.")
        if exc.status_code >= 500:
            raise LLMError("Claude ist gerade überlastet – bitte gleich nochmal versuchen.")
        raise LLMError("Claude-Anfrage abgelehnt (HTTP " + str(exc.status_code) + ") – Details im Log.")
    except anthropic.APITimeoutError:
        log.warning("claude timeout (model=%s)", model)
        raise LLMError("Claude hat nicht rechtzeitig geantwortet – bitte nochmal versuchen.")
    except anthropic.APIConnectionError:
        log.warning("claude connection error (model=%s)", model)
        raise LLMError("Keine Verbindung zu Claude – bitte später nochmal versuchen.")

    return _to_result(response, model)


def _to_result(response, requested_model: str) -> DraftResult:
    # Check stop_reason before reading content: a refusal has empty or partial content.
    if response.stop_reason == "refusal":
        category = getattr(getattr(response, "stop_details", None), "category", None)
        log.warning("claude refused the draft (model=%s, category=%s)", response.model, category)
        raise LLMError("Claude hat diesen Entwurf abgelehnt.")
    if response.stop_reason == "max_tokens":
        log.warning("claude hit max_tokens (model=%s)", response.model)
        raise LLMError("Die Antwort wurde abgeschnitten – bitte nochmal versuchen.")

    text = "".join(block.text for block in response.content if block.type == "text")
    try:
        data = json.loads(text)
        draft = data["draft"].strip()
        hint = (data.get("hint") or "").strip() or None
    except (ValueError, KeyError, TypeError, AttributeError):
        log.warning("claude returned unparseable output (model=%s, stop_reason=%s)", response.model, response.stop_reason)
        raise LLMError("Claude hat keine lesbare Antwort geliefert.")
    if not draft:
        raise LLMError("Claude hat einen leeren Entwurf geliefert.")

    # response.model is the model that actually answered (differs after a server-side fallback).
    model = response.model or requested_model
    usage = response.usage
    input_tokens = usage.input_tokens or 0
    output_tokens = usage.output_tokens or 0
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", None) or 0
    return DraftResult(
        text=draft,
        hint=hint,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        cost_usd=cost_usd(model, input_tokens, output_tokens, cache_read, cache_write),
    )


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
    budget = as_float(settings.get("monthly_budget_usd"), 0.0)
    if budget > 0 and db.cost_this_month() >= budget:
        raise BudgetExceeded("Monatsbudget von " + fmt_usd(budget) + " erreicht – im Dashboard erhöhen.")

    previous_text = None
    if kind in VARIANT_KINDS:
        previous = db.latest_draft(listing_id)
        previous_text = previous["text"] if previous else None

    result = await generate_draft(listing, settings, kind, previous_text, instruction)
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
    )
    log.info("draft listing=%s kind=%s model=%s tokens=%s/%s cost=%s",
             listing_id, kind, result.model, result.input_tokens, result.output_tokens, fmt_usd(result.cost_usd))

    # Re-read the status: it may have changed (e.g. "sent") while Claude was writing.
    if kind != "test":
        current = db.get_listing(listing_id)
        if current and current["status"] == "new":
            db.set_listing_status(listing_id, "drafted")

    return next(d for d in db.list_drafts(listing_id) if d["id"] == draft_id)
