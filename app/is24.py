"""ImmoScout24: exposé details and the full picture gallery of a listing.

Fredy passes one image and a short text per listing. For ImmoScout24 listings immobot fetches the exposé
once from the IS24 app API (the same request Fredy makes) and downloads every picture, then works only
from the local copies (DATA_DIR/media/<listing id>/<position>.jpg, max 1024 px JPEG):
- drafts get the details text and up to `llm_max_images` pictures (llm.py),
- the "🖼 Bilder" button sends them as a Telegram album (bot.py),
- the dashboard shows the gallery (web.py).

Politeness: one GET for the exposé; pictures one at a time over one keep-alive connection with a short
pause; only the size variant IS24 serves itself (no custom resize parameters, so the CDN cache hits);
only from *.immobilienscout24.de; the first refusal (403/429/not an image) ends the run. Nothing is
fetched again automatically - after a failure only when the user asks (button), at most once a minute.
"""

import asyncio
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db, images
from .portals import safe_url

log = logging.getLogger(__name__)

API_URL = "https://api.mobile.immobilienscout24.de/expose/{}"
APP_USER_AGENT = "ImmoScout_28.1_26.5.2_._"  # what Fredy sends to the same API
IMAGE_HOST_SUFFIX = ".immobilienscout24.de"
EXPOSE_TIMEOUT_S = 15.0
IMAGE_TIMEOUT_S = 15.0
DOWNLOAD_GAP_S = 0.25  # pause between two pictures
MAX_PICTURES = 30
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DETAILS_MAX_CHARS = 20_000
RETRY_AFTER_S = 60  # a failed fetch can be retried by the user after this long
KEEP_DAYS = 60  # pictures of listings received longer ago are deleted at startup
FLOORPLAN_WORDS = ("grundriss", "floorplan", "floor plan")

_EXPOSE_PATH = re.compile(r"/expose/([0-9]{1,15})/?")
_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here
_media_root: Path | None = None
_locks: dict[int, asyncio.Lock] = {}  # one per listing, so concurrent triggers fetch only once


class Is24Error(Exception):
    """str(exc) is a short German message, safe to show."""


def init(data_dir: str) -> None:
    global _media_root
    _media_root = Path(data_dir) / "media"


def media_root() -> Path:
    if _media_root is None:
        raise RuntimeError("is24.init() was not called")
    return _media_root


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


# --- pictures ----------------------------------------------------------------

async def _get_image(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code in (403, 429):
                raise Is24Error(f"Bilder-Server lehnt ab (HTTP {response.status_code}) – abgebrochen.")
            if response.status_code != 200:
                raise Is24Error(f"Bild nicht abrufbar (HTTP {response.status_code}) – abgebrochen.")
            if not response.headers.get("content-type", "").startswith("image/"):
                raise Is24Error("Bilder-Server liefert kein Bild – abgebrochen.")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise Is24Error("Bild größer als 10 MB – abgebrochen.")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise Is24Error(f"Bild nicht erreichbar ({type(exc).__name__}) – abgebrochen.") from None
    return b"".join(chunks)


async def download_pictures(listing_id: int, pictures: list[dict]) -> tuple[list[dict], str | None]:
    """Download one picture after the other over one connection, store them as JPEG files.
    Returns (stored rows, error or None); the first failure ends the run, earlier pictures are kept."""
    folder = media_root() / str(int(listing_id))
    folder.mkdir(parents=True, exist_ok=True)
    stored: list[dict] = []
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S, follow_redirects=False,
                                 headers={"User-Agent": APP_USER_AGENT}, transport=_transport) as client:
        for index, picture in enumerate(pictures):
            if index:
                await asyncio.sleep(DOWNLOAD_GAP_S)
            if not allowed_image_url(picture["url"]):  # parse_expose only keeps allowed URLs; double check
                return stored, "Bild-URL außerhalb von ImmoScout24 – abgebrochen."
            try:
                jpeg = images.to_jpeg(await _get_image(client, picture["url"]))
            except Is24Error as exc:
                return stored, str(exc)
            except Exception as exc:  # undecodable image data
                return stored, f"Bild {picture['position']} nicht lesbar ({type(exc).__name__}) – abgebrochen."
            path = folder / f"{picture['position']}.jpg"
            path.with_suffix(".tmp").write_bytes(jpeg)
            path.with_suffix(".tmp").replace(path)
            stored.append({"position": picture["position"], "caption": picture["caption"],
                           "source_url": picture["url"], "file": path.name})
    return stored, None


def _delete_files(listing_id: int) -> None:
    shutil.rmtree(media_root() / str(int(listing_id)), ignore_errors=True)


async def _fetch_and_store(listing: dict) -> None:
    listing_id, error = listing["id"], None
    try:
        parsed = parse_expose(await fetch_expose(expose_id(listing["url"])))
    except Is24Error as exc:
        error = str(exc)
    except Exception as exc:  # unexpected payload: type only in the log
        log.warning("IS24 exposé for listing %s could not be read: %s", listing_id, type(exc).__name__)
        error = "Exposé konnte nicht gelesen werden."
    else:
        db.set_listing_details(listing_id, parsed["details_text"], parsed["contact_name"])
        _delete_files(listing_id)  # a retry starts from scratch
        db.replace_media(listing_id, [])
        stored, error = await download_pictures(listing_id, parsed["media"])
        db.replace_media(listing_id, stored)
        log.info("IS24 listing %s: %d of %d pictures stored", listing_id, len(stored), len(parsed["media"]))
    db.set_media_state(listing_id, db.now_iso(), error)
    if error:
        log.warning("IS24 listing %s: %s", listing_id, error)
        db.set_meta("is24_last_error", f"{db.now_iso()} Inserat {listing_id}: {error}")


def _needs_fetch(listing: dict, retry: bool) -> bool:
    fetched_at = listing.get("media_fetched_at")
    if not fetched_at:
        return True
    if not (retry and listing.get("media_error")):
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    return age.total_seconds() >= RETRY_AFTER_S


async def ensure_media(listing_id: int, retry: bool = False) -> list[dict]:
    """The stored pictures of an IS24 listing, fetching exposé and pictures on first use. Never raises:
    a failure is recorded in listings.media_error and whatever was stored is returned. retry=True (a user
    action) tries again after a failed attempt. [] for listings from other portals."""
    listing = db.get_listing(listing_id)
    if listing is None or not is_supported(listing):
        return []
    async with _locks.setdefault(listing_id, asyncio.Lock()):
        listing = db.get_listing(listing_id)  # another task may have fetched meanwhile
        if _needs_fetch(listing, retry):
            await _fetch_and_store(listing)
    return db.list_media(listing_id)


def media_file(listing_id: int, position: int) -> Path | None:
    """Path of a stored picture, only if it exists inside DATA_DIR/media/<listing id>/."""
    row = db.get_media(listing_id, position)
    if row is None:
        return None
    folder = (media_root() / str(int(listing_id))).resolve()
    path = (folder / row["file"]).resolve()
    return path if path.parent == folder and path.is_file() else None


def read_pictures(listing_id: int, rows: list[dict]) -> list[tuple[dict, bytes]]:
    """(row, JPEG bytes) for every stored picture whose file exists."""
    result = []
    for row in rows:
        path = media_file(listing_id, row["position"])
        if path is not None:
            result.append((row, path.read_bytes()))
    return result


def cleanup_old_media(days: int = KEEP_DAYS) -> int:
    """Delete the pictures of listings received more than `days` ago. Returns the number of listings."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    listing_ids = db.listings_with_media_received_before(cutoff)
    for listing_id in listing_ids:
        _delete_files(listing_id)
        db.clear_media(listing_id)
    if listing_ids:
        log.info("Deleted the pictures of %d listing(s) older than %d days", len(listing_ids), days)
    return len(listing_ids)
