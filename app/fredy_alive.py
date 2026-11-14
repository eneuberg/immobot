"""Which listings Fredy still considers online: is_active per Fredy listing id, read from Fredy's MCP server.

Fredy probes every listing it stores (lib/services/crons/listing-alive-cron.js, every 4 h, one request at a
time per portal) and sets `listings.is_active = 0` when the ad is gone. It sends no notification for that: the
HTTP adapter only knows new listings and `event: "priceChange"`. Its REST API (/api/listings) needs a browser
session (cookie login), so the one machine-to-machine way in is the MCP endpoint `POST <fredy>/api/mcp`
(Streamable HTTP, JSON-RPC), which takes a static per-user token `Authorization: Bearer fredy_<64 hex>`
(Fredy: Administration → Benutzer → "Token anzeigen"; lib/mcp/mcpAuthentication.js authenticateRequest).

One run = one MCP session: initialize, notifications/initialized, `list_listings` pages of 1000, DELETE. The
tool answers in Markdown (lib/mcp/mcpNormalizer.js normalizeListListings): a table whose "ID" column is Fredy's
listing id (= immobot's `fredy_id`, the `id` of the webhook payload) and whose "Active" column is "yes"/"no".
Listings hidden in Fredy (manually_deleted) and listings Fredy has purged are not in the table: they come back
as "unknown" (missing from the result), never as offline.

immobot never asks ImmoScout24 or Kleinanzeigen itself; this only reads what Fredy already knows. Every request
goes to Fredy's address from the setting "fredy_base_url". Logs carry status codes and exception class names,
never the token or response bodies.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable

import httpx

from . import db, vault

log = logging.getLogger(__name__)

EVERY_S = 1800  # Fredy re-probes every 4 h, so asking more often only repeats the same answer
FIRST_RUN_DELAY_S = 120  # not during startup (Telegram, backfills)
TIMEOUT_S = 30.0
PAGE_SIZE = 1000  # Fredy's maximum for list_listings
MAX_PAGES = 20  # 20 000 listings; more would mean something is off
PROTOCOL_VERSION = "2025-06-18"  # supported by Fredy's @modelcontextprotocol/sdk 1.31
CREDENTIAL = "fredy_mcp_token"

_transport: httpx.AsyncBaseTransport | None = None  # tests inject an httpx.MockTransport here
_UNESCAPED_PIPE = re.compile(r"(?<!\\)\|")
_HAS_MORE = re.compile(r"\*\*Has more:\*\*\s*(yes|no)", re.IGNORECASE)


class FredyError(Exception):
    """str(exc) is a short, secret-free reason for the log."""


# --- parsing ---------------------------------------------------------------------

def _cells(line: str) -> list[str]:
    """'| a | b \\| c |' -> ['a', 'b | c']. Fredy escapes pipes inside cells as '\\|'."""
    line = line.strip()
    if not (line.startswith("|") and line.endswith("|")):
        return []
    return [cell.strip().replace("\\|", "|") for cell in _UNESCAPED_PIPE.split(line[1:-1])]


def parse_listings_table(markdown: str) -> dict[str, bool]:
    """{listing id: active} from one list_listings answer. Columns are found by their header ("ID", "Active"),
    so added or reordered columns don't break it. Rows with an unexpected Active value are skipped."""
    states: dict[str, bool] = {}
    id_col = active_col = None
    width = 0
    for line in markdown.splitlines():
        cells = _cells(line)
        if not cells:
            continue
        if id_col is None:
            if "ID" in cells and "Active" in cells:
                id_col, active_col, width = cells.index("ID"), cells.index("Active"), len(cells)
            continue
        if len(cells) != width or set("".join(cells)) <= set("-: "):  # separator row or a broken line
            continue
        listing_id, active = cells[id_col], cells[active_col].lower()
        if listing_id and listing_id != "–" and active in ("yes", "no"):
            states[listing_id] = active == "yes"
    return states


def has_more(markdown: str) -> bool:
    match = _HAS_MORE.search(markdown)
    return bool(match) and match.group(1).lower() == "yes"


def _rpc_message(response: httpx.Response, request_id: int) -> dict:
    """The JSON-RPC answer to `request_id`, from a JSON body or an SSE stream ("event: message / data: {...}")."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        candidates, data = [], []
        for line in response.text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip())
            elif not line.strip() and data:  # end of an event
                candidates.append("\n".join(data))
                data = []
    else:
        candidates = [response.text]
    for raw in candidates:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        for item in message if isinstance(message, list) else [message]:
            if isinstance(item, dict) and item.get("id") == request_id:
                if "error" in item:
                    code = item["error"].get("code") if isinstance(item["error"], dict) else None
                    raise FredyError(f"JSON-RPC error {code}")
                if isinstance(item.get("result"), dict):
                    return item["result"]
    raise FredyError("no JSON-RPC answer")


def _tool_text(result: dict) -> str:
    if result.get("isError"):
        raise FredyError("tool error")
    parts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    if not parts:
        raise FredyError("empty tool answer")
    return "\n".join(parts)


# --- MCP session -------------------------------------------------------------------

class _Session:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client, self.url = client, url
        self.session_id: str | None = None
        self.next_id = 1

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["Mcp-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def _post(self, payload: dict) -> httpx.Response:
        response = await self.client.post(self.url, json=payload, headers=self._headers())
        if response.status_code in (401, 403):
            raise FredyError(f"HTTP {response.status_code} (MCP-Token falsch oder ohne Zugriff?)")
        if response.status_code >= 400:
            raise FredyError(f"HTTP {response.status_code}")
        return response

    async def request(self, method: str, params: dict) -> dict:
        request_id, self.next_id = self.next_id, self.next_id + 1
        response = await self._post({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if self.session_id is None:
            self.session_id = response.headers.get("mcp-session-id")
        return _rpc_message(response, request_id)

    async def open(self) -> None:
        await self.request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "immobot", "version": "1"},
        })
        if not self.session_id:
            raise FredyError("no MCP session id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def close(self) -> None:
        """Fredy keeps a session in memory until it is deleted."""
        if self.session_id:
            try:
                await self.client.delete(self.url, headers=self._headers())
            except httpx.HTTPError:
                pass


async def _all_states(base_url: str, token: str) -> dict[str, bool]:
    url = base_url.rstrip("/") + "/api/mcp"
    async with httpx.AsyncClient(timeout=TIMEOUT_S, follow_redirects=False, transport=_transport,
                                 headers={"Authorization": f"Bearer {token}", "User-Agent": "immobot"}) as client:
        session = _Session(client, url)
        try:
            await session.open()
            states: dict[str, bool] = {}
            for page in range(1, MAX_PAGES + 1):
                result = await session.request("tools/call", {
                    "name": "list_listings",
                    "arguments": {"page": page, "pageSize": PAGE_SIZE, "sortField": "created_at", "sortDir": "asc"},
                })
                text = _tool_text(result)
                found = parse_listings_table(text)
                if not found and "| ID |" not in text and "No listings found" not in text:
                    raise FredyError("unexpected list_listings format")
                states.update(found)
                if not has_more(text):
                    break
            return states
        finally:
            await session.close()


def _config() -> tuple[str, str] | None:
    """(Fredy address, MCP token), or None while one of them is not set up."""
    base_url = (db.get_settings().get("fredy_base_url") or "").strip()
    token = vault.get(CREDENTIAL)
    if not base_url.startswith(("https://", "http://")) or not token:
        return None
    return base_url, token


def configured() -> bool:
    return _config() is not None


async def fetch_states(fredy_ids: list[str]) -> dict[str, bool]:
    """{fredy_id: is_active} for the given ids that Fredy knows. Ids Fredy doesn't list (hidden in Fredy,
    purged, other user's jobs) are left out - unknown, not offline. {} when not configured or on any error
    (logged without secrets); never raises."""
    wanted = {str(i) for i in fredy_ids if i}
    if not wanted:
        return {}
    config = _config()
    if config is None:
        return {}
    try:
        states = await asyncio.wait_for(_all_states(*config), timeout=TIMEOUT_S * 4)
    except FredyError as exc:
        log.warning("Fredy online check failed: %s", exc)
        return {}
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        log.warning("Fredy online check failed: %s", type(exc).__name__)
        return {}
    except Exception as exc:  # never let the loop die on something unexpected
        log.warning("Fredy online check failed unexpectedly: %s", type(exc).__name__)
        return {}
    result = {i: states[i] for i in wanted if i in states}
    log.info("Fredy online check: %d of %d listing(s) known to Fredy, %d offline",
             len(result), len(wanted), sum(not active for active in result.values()))
    return result


async def run_once(apply: Callable[[dict[str, bool]], None], ids_provider: Callable[[], list[str]]) -> dict[str, bool]:
    """One check; returns what was handed to `apply` ({} = nothing to apply, apply not called)."""
    if not configured():
        return {}
    ids = ids_provider()
    states = await fetch_states(ids) if ids else {}
    if states:
        apply(states)
    return states


async def loop(apply: Callable[[dict[str, bool]], None], ids_provider: Callable[[], list[str]],
               every_s: float = EVERY_S, first_delay_s: float = FIRST_RUN_DELAY_S) -> None:
    """Every `every_s`: ids_provider() -> fetch_states -> apply(states). Does nothing while the MCP token or
    the Fredy address is missing (checked on every round, so entering them later is enough)."""
    await asyncio.sleep(first_delay_s)
    while True:
        try:
            await run_once(apply, ids_provider)
        except Exception:  # keep checking; the next run may work
            log.exception("Applying Fredy's online states failed")
        await asyncio.sleep(every_s)
