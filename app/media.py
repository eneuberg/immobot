"""Listing pictures: where they come from, the local copies and the polite downloader.

Sources, in this order (ensure_media):
1. Fredy: the webhook's optional per-listing `images` (Fredy PR #489, or a patched Fredy). The URLs are
   stored when the listing arrives and downloaded on first use; immobot then never asks ImmoScout24.
2. ImmoScout24: if Fredy sends none, the listing is from ImmoScout24 and the setting "is24_fetch" is on,
   immobot fetches the exposé itself (is24.py): details text and pictures.
3. Otherwise there is no gallery; drafts use Fredy's single imageUrl as before (llm.py).

Local copies: DATA_DIR/media/<listing id>/<position>.jpg (JPEG, max 1024 px), rows in listing_media
(`file` is "" until a picture is downloaded). Downloads are polite: one keep-alive connection, one picture
after the other with a short pause, size and type limits, and the first refusal (403/429/not an image)
ends the run. Nothing is fetched again automatically - after a failure only on a user action, at most once a
minute. Used by drafts (llm.py), the "🖼 Bilder" album (bot.py) and the dashboard gallery (web.py).
"""

import asyncio
import ipaddress
import logging
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from . import db, images, is24, settings
from .portals import safe_url

log = logging.getLogger(__name__)

IMAGE_TIMEOUT_S = 15.0
DOWNLOAD_GAP_S = 0.25  # pause between two pictures
MAX_PICTURES = 30  # stored per listing
MAX_IMAGE_BYTES = 10 * 1024 * 1024
DETAILS_GAP_S = 2.0  # between two exposé requests for a batch of new listings
NEW_DETAILS_MAX = 10  # of one Fredy batch fetched right away; the rest by the slow backfill
BACKFILL_GAP_S = 30.0  # between two exposé requests in the background (older listings, rest of a big batch)
IS24_PAUSE_S = 3600  # no automatic exposé requests for this long after ImmoScout24 refused one
REFUSALS = ("(HTTP 429)", "(HTTP 403)")
RATE_LIMITED = "(HTTP 429)"
RETRY_AFTER_S = 60  # a failed download can be retried by the user after this long
KEEP_DAYS = 60  # pictures of listings received longer ago are deleted at startup
LONG_DESCRIPTION = 500  # Fredy's text is long enough (portal details enabled): no exposé fetch just for text
NOT_DOWNLOADED = ""  # listing_media.file of a picture known by URL but not downloaded yet

# Fredy's optional per-listing images (webhook): limits for what is accepted.
FREDY_MAX_ENTRIES = 50
FREDY_URL_MAX = 2000
FREDY_CAPTION_MAX = 200
BROWSER_USER_AGENT = (  # portal CDNs serve browsers; same as llm.py uses for Fredy's single image
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".intranet", ".corp", ".arpa")
_TLD = re.compile(r"[a-z]{2,63}|xn--[a-z0-9-]{1,59}")

_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here
_media_root: Path | None = None
_tasks: set[asyncio.Task] = set()  # background exposé fetches (kept referenced)
_backfill_task: asyncio.Task | None = None
_on_details = None  # see schedule_backfill
_paused_until = 0.0  # time.monotonic(); see is24_paused
_locks: dict[int, asyncio.Lock] = {}  # one per listing, so concurrent triggers fetch only once


MANUAL_PREFIX = "manual-"  # fredy_id of listings added by hand ("Inserat per Link hinzufügen")


class DownloadError(Exception):
    """str(exc) is a short German message, safe to show."""


def init(data_dir: str) -> None:
    global _media_root
    _media_root = Path(data_dir) / "media"


def media_root() -> Path:
    if _media_root is None:
        raise RuntimeError("media.init() was not called")
    return _media_root


# --- sources -----------------------------------------------------------------

def is24_fetch_enabled() -> bool:
    """Setting "Bilder & Details selbst von ImmoScout24 laden"."""
    return settings.as_bool(db.get_settings().get("is24_fetch"))


def source_of(listing: dict) -> str | None:
    """"fredy" (pictures from the webhook), "is24" (own fetch allowed), or None (Fredy's single image only)."""
    if listing.get("media_source") == "fredy":
        return "fredy"
    if listing.get("media_source") == "is24" and str(listing.get("fredy_id") or "").startswith(MANUAL_PREFIX):
        return "is24"  # added by hand: its exposé was fetched on purpose, whatever the setting says
    if is24.is_supported(listing) and is24_fetch_enabled():
        return "is24"
    return None


def has_gallery(listing: dict) -> bool:
    """Whether "🖼 Bilder" / the dashboard gallery make sense for this listing."""
    return source_of(listing) is not None


def has_long_description(listing: dict) -> bool:
    return len((listing.get("description") or "").strip()) > LONG_DESCRIPTION


# --- Fredy's images (webhook) -------------------------------------------------

def fredy_image_url_ok(url) -> bool:
    """A picture URL from Fredy: https to a public host name on the default port. No IP literals, no
    localhost/.local/.internal names, no credentials, at most 2000 characters."""
    if not isinstance(url, str) or len(url) > FREDY_URL_MAX:
        return False
    url = safe_url(url)  # http(s) only, a host, no whitespace/control characters, no "user@"
    if url is None:
        return False
    parts = urlsplit(url)
    host = parts.hostname.lower().rstrip(".")
    if parts.scheme.lower() != "https" or parts.port not in (None, 443):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    if host == "localhost" or host.endswith(_PRIVATE_SUFFIXES) or "." not in host:
        return False
    return bool(_TLD.fullmatch(host.rsplit(".", 1)[1]))  # a real top-level domain, not "127.1" or "0x7f.1"


def parse_fredy_images(raw) -> list[dict]:
    """Fredy's optional `images`: [{url, caption?, floorPlan?}] (plain URL strings are accepted too).
    Invalid entries are dropped, never the webhook. Floor plans first, no duplicate URLs, at most
    MAX_PICTURES. Returns [{position, caption, url, floor_plan}]."""
    if not isinstance(raw, list):
        return []
    pictures, seen = [], set()
    for entry in raw[:FREDY_MAX_ENTRIES]:
        if isinstance(entry, str):
            entry = {"url": entry}
        if not isinstance(entry, dict) or not fredy_image_url_ok(entry.get("url")) or entry["url"] in seen:
            continue
        seen.add(entry["url"])
        caption = entry.get("caption")
        caption = " ".join(caption.split())[:FREDY_CAPTION_MAX] if isinstance(caption, str) else ""
        pictures.append({"caption": caption, "url": entry["url"].strip(), "floor_plan": entry.get("floorPlan") is True})
    pictures.sort(key=lambda p: not p["floor_plan"])  # stable
    pictures = pictures[:MAX_PICTURES]
    for position, picture in enumerate(pictures, start=1):
        picture["position"] = position
    return pictures


def store_fredy_images(listing_id: int, pictures: list[dict]) -> bool:
    """Remember Fredy's picture URLs for a listing that has no pictures yet (they are downloaded on first
    use). A listing sent again, or one with pictures from ImmoScout24 already, is left alone. True if stored."""
    if not pictures or db.list_media(listing_id):
        return False
    db.replace_media(listing_id, [{"position": p["position"], "caption": p["caption"], "source_url": p["url"],
                                   "file": NOT_DOWNLOADED, "floor_plan": p["floor_plan"]} for p in pictures])
    db.set_media_source(listing_id, "fredy")
    db.set_media_state(listing_id, None, None)  # a failed ImmoScout24 attempt doesn't block these
    return True


# --- downloading ---------------------------------------------------------------

async def _get_image(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        async with client.stream("GET", url) as response:
            if response.status_code in (403, 429):
                raise DownloadError(f"Bilder-Server lehnt ab (HTTP {response.status_code}) – abgebrochen.")
            if response.status_code != 200:
                raise DownloadError(f"Bild nicht abrufbar (HTTP {response.status_code}) – abgebrochen.")
            if not response.headers.get("content-type", "").startswith("image/"):
                raise DownloadError("Bilder-Server liefert kein Bild – abgebrochen.")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise DownloadError("Bild größer als 10 MB – abgebrochen.")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise DownloadError(f"Bild nicht erreichbar ({type(exc).__name__}) – abgebrochen.") from None
    return b"".join(chunks)


async def download_pictures(listing_id: int, pictures: list[dict], allowed, user_agent: str) -> tuple[list[dict], str | None]:
    """Download one picture after the other over one connection and store them as JPEG files.
    `allowed(url)` is the source's URL policy. Returns (stored pictures with "file", error or None); the
    first failure ends the run, earlier pictures are kept. Redirects are not followed."""
    folder = media_root() / str(int(listing_id))
    folder.mkdir(parents=True, exist_ok=True)
    stored: list[dict] = []
    async with httpx.AsyncClient(timeout=IMAGE_TIMEOUT_S, follow_redirects=False,
                                 headers={"User-Agent": user_agent}, transport=_transport) as client:
        for index, picture in enumerate(pictures):
            if index:
                await asyncio.sleep(DOWNLOAD_GAP_S)
            if not allowed(picture["url"]):
                return stored, "Bild-URL nicht erlaubt – abgebrochen."
            try:
                jpeg = images.to_jpeg(await _get_image(client, picture["url"]))
            except DownloadError as exc:
                return stored, str(exc)
            except Exception as exc:  # undecodable image data
                return stored, f"Bild {picture['position']} nicht lesbar ({type(exc).__name__}) – abgebrochen."
            path = folder / f"{picture['position']}.jpg"
            path.with_suffix(".tmp").write_bytes(jpeg)
            path.with_suffix(".tmp").replace(path)
            stored.append({**picture, "file": path.name})
    return stored, None


def _delete_files(listing_id: int) -> None:
    shutil.rmtree(media_root() / str(int(listing_id)), ignore_errors=True)


def _record(listing_id: int, source: str, error: str | None) -> None:
    db.set_media_state(listing_id, db.now_iso(), error)
    if error:
        label = "Fredy-Bilder" if source == "fredy" else "ImmoScout24"
        log.warning("%s for listing %s: %s", label, listing_id, error)
        db.set_meta("media_last_error", f"{db.now_iso()} Inserat {listing_id} ({label}): {error}")


def _pending(listing_id: int) -> list[dict]:
    return [{"position": r["position"], "caption": r["caption"], "url": r["source_url"]}
            for r in db.list_media(listing_id) if r["file"] == NOT_DOWNLOADED]


async def _download_pending(listing: dict, source: str) -> None:
    """Download the pictures known by URL but not stored yet (Fredy's, or IS24's after a text-only exposé fetch;
    a retry continues where the last run stopped)."""
    listing_id = listing["id"]
    pending = _pending(listing_id)
    if source == "fredy":
        stored, error = await download_pictures(listing_id, pending, fredy_image_url_ok, BROWSER_USER_AGENT)
    else:
        stored, error = await download_pictures(listing_id, pending, is24.allowed_image_url, is24.APP_USER_AGENT)
    for picture in stored:
        db.set_media_file(listing_id, picture["position"], picture["file"])
    log.info("%s pictures for listing %s: %d of %d stored", source, listing_id, len(stored), len(pending))
    _record(listing_id, source, error)


async def _fetch_is24(listing: dict) -> None:
    """Own ImmoScout24 fetch: exposé (details text) and its pictures."""
    listing_id, error = listing["id"], None
    try:
        parsed = is24.parse_expose(await is24.fetch_expose(is24.expose_id(listing["url"])))
    except is24.Is24Error as exc:
        error = str(exc)
    except Exception as exc:  # unexpected payload: type only in the log
        log.warning("IS24 exposé for listing %s could not be read: %s", listing_id, type(exc).__name__)
        error = "Exposé konnte nicht gelesen werden."
    else:
        await store_is24(listing_id, parsed)
        return
    _record(listing_id, "is24", error)


async def store_is24(listing_id: int, parsed: dict, download: bool = True) -> None:
    """Store a parsed exposé (is24.parse_expose): details text, contact name and its pictures (downloaded now, or
    with download=False only their URLs - downloaded on first use)."""
    db.set_listing_details(listing_id, parsed["details_text"], parsed["contact_name"])
    _delete_files(listing_id)  # a retry starts from scratch
    db.replace_media(listing_id, [])
    db.set_media_source(listing_id, "is24")
    if not download:
        db.replace_media(listing_id, [{"position": p["position"], "caption": p["caption"], "source_url": p["url"],
                                       "file": NOT_DOWNLOADED, "floor_plan": p["is_floorplan"]} for p in parsed["media"]])
        log.info("IS24 listing %s: exposé text stored, %d pictures for later", listing_id, len(parsed["media"]))
        _record(listing_id, "is24", None)
        return
    stored, error = await download_pictures(listing_id, parsed["media"], is24.allowed_image_url, is24.APP_USER_AGENT)
    db.replace_media(listing_id, [{"position": p["position"], "caption": p["caption"], "source_url": p["url"],
                                   "file": p["file"], "floor_plan": p["is_floorplan"]} for p in stored])
    log.info("IS24 listing %s: %d of %d pictures stored", listing_id, len(stored), len(parsed["media"]))
    _record(listing_id, "is24", error)


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


async def ensure_media(listing_id: int, retry: bool = False, want_images: bool = True) -> list[dict]:
    """The downloaded pictures of a listing, getting them on first use from the listing's source (see the
    module docstring). Never raises: a failure is recorded in listings.media_error and whatever was stored
    is returned. retry=True (a user action) tries again after a failure. want_images=False (a draft without
    pictures) downloads nothing; the ImmoScout24 exposé is then only fetched when Fredy's text is short."""
    listing = db.get_listing(listing_id)
    if listing is None:
        return []
    async with _locks.setdefault(listing_id, asyncio.Lock()):
        listing = db.get_listing(listing_id)  # another task may have fetched meanwhile
        source = source_of(listing)
        if source == "fredy" and want_images and _needs_fetch(listing, retry):
            await _download_pending(listing, "fredy")
        elif source == "is24" and (want_images or not has_long_description(listing)) and _needs_fetch(listing, retry):
            await _fetch_is24(listing)
        elif source == "is24" and want_images and listing.get("details") and _pending(listing_id) \
                and (not listing.get("media_error") or _needs_fetch(listing, retry)):
            await _download_pending(listing, "is24")  # the exposé text came first (ensure_details)
    return stored_pictures(listing_id)


def details_missing(listing: dict) -> bool:
    """An ImmoScout24 listing whose exposé text was never fetched (or only refused as too many requests)."""
    if listing.get("details") or source_of(listing) != "is24":
        return False
    return not listing.get("media_fetched_at") or RATE_LIMITED in (listing.get("media_error") or "")


def is24_paused() -> bool:
    """After ImmoScout24 refused an automatic exposé request: no further automatic ones for a while."""
    return time.monotonic() < _paused_until


def _pause_is24(error: str) -> None:
    global _paused_until
    _paused_until = time.monotonic() + IS24_PAUSE_S
    log.warning("ImmoScout24 refuses exposé requests (%s) - no automatic ones for %d min", error, IS24_PAUSE_S // 60)
    db.set_meta("media_last_error", f"{db.now_iso()} ImmoScout24: {error} - automatische Abrufe pausiert")


async def ensure_details(listing_id: int) -> bool:
    """ImmoScout24 listings without exposé text: fetch it (one request; rent, details, picture URLs - the pictures
    themselves only on first use). Used before a new listing is posted, so its message shows the warm rent, and by
    backfill_details. A refusal (429/403) is not recorded on the listing but pauses all automatic requests
    (IS24_PAUSE_S). Never raises; True if the text was stored."""
    listing = db.get_listing(listing_id)
    if listing is None or is24_paused() or not details_missing(listing):
        return False
    async with _locks.setdefault(listing_id, asyncio.Lock()):
        listing = db.get_listing(listing_id)
        if is24_paused() or not details_missing(listing):
            return False
        try:
            parsed = is24.parse_expose(await is24.fetch_expose(is24.expose_id(listing["url"])))
        except is24.Is24Error as exc:
            if any(code in str(exc) for code in REFUSALS):
                _pause_is24(str(exc))
            else:  # gone (404) and the like: recorded, not asked again
                _record(listing_id, "is24", str(exc))
            return False
        except Exception as exc:  # unexpected payload: type only in the log
            log.warning("IS24 exposé for listing %s could not be read: %s", listing_id, type(exc).__name__)
            _record(listing_id, "is24", "Exposé konnte nicht gelesen werden.")
            return False
        await store_is24(listing_id, parsed, download=False)
    return True


async def fetch_details(listing_ids: list[int], gap_s: float = DETAILS_GAP_S) -> None:
    """New listings from Fredy: the exposé text of the first NEW_DETAILS_MAX right away, one after the other
    (bot.announce waits for each one it posts); a bigger batch's rest is left to the slow backfill."""
    for index, listing_id in enumerate(listing_ids[:NEW_DETAILS_MAX]):
        if index:
            await asyncio.sleep(gap_s)
        await ensure_details(listing_id)
    if len(listing_ids) > NEW_DETAILS_MAX:
        schedule_backfill()


def schedule_details(listing_ids: list[int]) -> None:
    """fetch_details in the background. Call from the event loop."""
    if listing_ids:
        task = asyncio.create_task(fetch_details(list(listing_ids)))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)


def schedule_backfill(on_done=None) -> None:
    """Start backfill_details unless it is running. on_done (kept for later runs): called with a listing id after
    its exposé text was stored (bot.schedule_refresh: the Telegram message then shows the warm rent)."""
    global _backfill_task, _on_details
    if on_done is not None:
        _on_details = on_done
    if _backfill_task is None or _backfill_task.done():
        _backfill_task = asyncio.create_task(backfill_details())


async def backfill_details(gap_s: float = BACKFILL_GAP_S) -> int:
    """The exposé text of ImmoScout24 listings that don't have it yet (older ones, the rest of a big Fredy batch,
    ones refused before), newest first, one request every gap_s seconds, last KEEP_DAYS days. After a refusal it
    waits until the pause is over and goes on. Returns the number stored."""
    count = 0
    while is24_fetch_enabled():
        since = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
        paused = False
        for listing_id in db.listing_ids_without_details(since):
            if is24_paused():
                paused = True
                break
            listing = db.get_listing(listing_id)
            if listing is None or not details_missing(listing):
                continue
            if await ensure_details(listing_id):
                count += 1
                if _on_details is not None:
                    _on_details(listing_id)
            await asyncio.sleep(gap_s)
        if not paused and not is24_paused():
            break
        await asyncio.sleep(max(_paused_until - time.monotonic(), 0) + 1)
    if count:
        log.info("Exposé text fetched for %d listing(s) in the background", count)
    return count


# --- local copies --------------------------------------------------------------

def stored_pictures(listing_id: int) -> list[dict]:
    """listing_media rows whose picture is downloaded, in order."""
    return [row for row in db.list_media(listing_id) if row["file"] != NOT_DOWNLOADED]


def media_file(listing_id: int, position: int) -> Path | None:
    """Path of a stored picture, only if it exists inside DATA_DIR/media/<listing id>/."""
    row = db.get_media(listing_id, position)
    if row is None or row["file"] == NOT_DOWNLOADED:
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
