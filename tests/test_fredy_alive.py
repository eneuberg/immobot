"""app/fredy_alive.py: is_active per listing from Fredy's MCP server (list_listings), with a fake Fredy.

The fake answers like Fredy 29.2.1 does: lib/mcp/mcpHttpRoute.js (Bearer token, session header), the MCP SDK's
StreamableHTTPServerTransport (SSE answers "event: message / data: …", 406 without the right Accept header) and
lib/mcp/mcpNormalizer.js normalizeListListings (the Markdown table, pipes in cells escaped as "\\|")."""

import asyncio
import json
import logging
import math

import httpx
import pytest

from app import fredy_alive, vault

TOKEN = "fredy_" + "ab12" * 16  # same shape as Fredy's userStorage.generateMcpToken
BASE = "https://fredy.example.com"
SESSION = "6f1c2a9e-0000-4000-8000-000000000001"


def row(listing_id, active, title="Schöne 2-Zimmer-Wohnung", address="Musterstraße 1, 12345 Musterstadt"):
    return {"id": listing_id, "title": title, "address": address, "price": 950, "dealType": "rent", "size": 54,
            "provider": "immoscout", "is_active": active, "status": None, "job_name": "Wohnung Mitte"}


def cell(value):
    """mcpNormalizer.cell."""
    return "–" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def list_listings_markdown(rows, total, page, page_size):
    """mcpNormalizer.normalizeListListings, line for line."""
    max_page = max(1, math.ceil(total / page_size))
    more = page < max_page
    md = "**Tool:** list_listings | **Status:** OK\n\n"
    md += f"Found **{total}** listing(s). Showing page {page} of {max_page} ({len(rows)} on this page)."
    if more:
        md += f" More pages available - use page={page + 1} to continue."
    md += "\n\n"
    if rows:
        md += "| ID | Title | Address | Price | Deal | Size | Provider | Internet | Active | Status | Listed | Found | Job |\n"
        md += "|----|-------|---------|-------|------|------|----------|----------|--------|--------|--------|-------|-----|\n"
        for r in rows:
            md += (f"| {cell(r['id'])} | {cell(r['title'])} | {cell(r['address'])} | {cell(r['price'])} | renting | "
                   f"{cell(r['size'])} | {cell(r['provider'])} | – | {'yes' if r['is_active'] else 'no'} | "
                   f"{cell(r['status'])} | – | 2026-11-13 10:42:05 | {cell(r['job_name'])} |\n")
        md += "\nUse **get_listing** with an ID for full details (description, link, image).\n"
    else:
        md += "No listings found.\n"
    md += f"\n**Page:** {page}/{max_page} | **Has more:** {'yes' if more else 'no'}"
    return md


def sse(message):
    return httpx.Response(200, text=f"event: message\ndata: {json.dumps(message)}\n\n",
                          headers={"content-type": "text/event-stream", "mcp-session-id": SESSION})


class FakeFredy:
    def __init__(self, listings, token=TOKEN, json_answers=False, tool_error=False):
        self.listings, self.token, self.json_answers, self.tool_error = listings, token, json_answers, tool_error
        self.requests: list[httpx.Request] = []
        self.calls: list[dict] = []
        self.deleted = False

    def answer(self, message):
        if self.json_answers:
            return httpx.Response(200, json=message, headers={"mcp-session-id": SESSION})
        return sse(message)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url) == f"{BASE}/api/mcp"
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "Unauthorized. Provide a valid Bearer token."})
        if request.method == "DELETE":
            assert request.headers.get("mcp-session-id") == SESSION
            self.deleted = True
            return httpx.Response(200, json={"ok": True})
        accept = request.headers.get("accept", "")
        if "application/json" not in accept or "text/event-stream" not in accept:
            return httpx.Response(406, json={"jsonrpc": "2.0", "error": {"code": -32000, "message": "Not Acceptable"}})
        body = json.loads(request.content)
        if body["method"] == "initialize":
            assert "mcp-session-id" not in request.headers
            return self.answer({"jsonrpc": "2.0", "id": body["id"], "result": {
                "protocolVersion": body["params"]["protocolVersion"], "capabilities": {"tools": {}},
                "serverInfo": {"name": "fredy-mcp", "version": "1.0.0"}}})
        assert request.headers.get("mcp-session-id") == SESSION
        assert request.headers.get("mcp-protocol-version") == fredy_alive.PROTOCOL_VERSION
        if body["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert body["method"] == "tools/call" and body["params"]["name"] == "list_listings"
        args = body["params"]["arguments"]
        self.calls.append(args)
        if self.tool_error:
            return self.answer({"jsonrpc": "2.0", "id": body["id"], "result": {
                "content": [{"type": "text", "text": "**Tool:** list_listings | **Status:** ERROR\n\nboom"}],
                "isError": True}})
        page, size = args["page"], args["pageSize"]
        rows = self.listings[(page - 1) * size: page * size]
        text = list_listings_markdown(rows, len(self.listings), page, size)
        return self.answer({"jsonrpc": "2.0", "id": body["id"], "result": {"content": [{"type": "text", "text": text}]}})


@pytest.fixture
def fredy(fresh_vault, monkeypatch):
    """Configured immobot (address + token) talking to a fake Fredy; returns a function installing the fake."""
    fresh_vault.db.update_settings({"fredy_base_url": BASE})
    vault.set("fredy_mcp_token", TOKEN)

    def install(server):
        monkeypatch.setattr(fredy_alive, "_transport", httpx.MockTransport(server))
        return server
    return install


LISTINGS = [
    row("CkL-VV8V9h9ibGF8hwv2r", True),
    row("jQEqH4Rgc0aonbuk7EFFs", False, title="Altbau | Balkon | ruhig"),  # pipes in a cell
    row("nfjHYoiaqq99gjs6t_15d", True, address=None),
    row("TDAazFy2pGbdakkJXWZKv", False, title="Endet auf Backslash \\"),
]


# --- parsing ---------------------------------------------------------------------

def test_parse_fredy_table():
    text = list_listings_markdown(LISTINGS, 4, 1, 50)
    assert fredy_alive.parse_listings_table(text) == {
        "CkL-VV8V9h9ibGF8hwv2r": True, "jQEqH4Rgc0aonbuk7EFFs": False,
        "nfjHYoiaqq99gjs6t_15d": True, "TDAazFy2pGbdakkJXWZKv": False}
    assert not fredy_alive.has_more(text)
    assert fredy_alive.has_more(list_listings_markdown(LISTINGS[:2], 4, 1, 2))


def test_parse_finds_columns_by_header_and_skips_odd_rows():
    text = ("| Active | Title | ID |\n|---|---|---|\n| yes | a \\| b | id1 |\n| no | c | id2 |\n"
            "| maybe | d | id3 |\n| yes | broken |\n| no | e | – |\n")
    assert fredy_alive.parse_listings_table(text) == {"id1": True, "id2": False}
    assert fredy_alive.parse_listings_table("**Tool:** list_listings | **Status:** OK\n\nNo listings found.\n") == {}


# --- fetch_states ------------------------------------------------------------------

def test_fetch_states_full_session(fredy):
    server = fredy(FakeFredy(LISTINGS))
    states = asyncio.run(fredy_alive.fetch_states(
        ["CkL-VV8V9h9ibGF8hwv2r", "jQEqH4Rgc0aonbuk7EFFs", "gone-from-fredy", "manual-17"]))
    # only ids Fredy lists; the others stay unknown (not "offline")
    assert states == {"CkL-VV8V9h9ibGF8hwv2r": True, "jQEqH4Rgc0aonbuk7EFFs": False}
    assert [json.loads(r.content)["method"] for r in server.requests if r.method == "POST"] == [
        "initialize", "notifications/initialized", "tools/call"]
    assert server.calls == [{"page": 1, "pageSize": 1000, "sortField": "created_at", "sortDir": "asc"}]
    assert server.deleted  # Fredy keeps sessions in memory until they are deleted


def test_fetch_states_pages_through(fredy, monkeypatch):
    monkeypatch.setattr(fredy_alive, "PAGE_SIZE", 3)
    many = [row(f"id{i}", i % 3 != 0) for i in range(8)]
    server = fredy(FakeFredy(many))
    states = asyncio.run(fredy_alive.fetch_states([r["id"] for r in many]))
    assert states == {r["id"]: r["is_active"] for r in many}
    assert [c["page"] for c in server.calls] == [1, 2, 3]


def test_fetch_states_accepts_plain_json_answers(fredy):
    fredy(FakeFredy(LISTINGS, json_answers=True))
    assert asyncio.run(fredy_alive.fetch_states(["TDAazFy2pGbdakkJXWZKv"])) == {"TDAazFy2pGbdakkJXWZKv": False}


def test_not_configured_means_no_request(fresh_vault):
    # conftest's transport fails the test on any request
    assert asyncio.run(fredy_alive.fetch_states(["a"])) == {}  # neither token nor address
    vault.set("fredy_mcp_token", TOKEN)
    assert not fredy_alive.configured() and asyncio.run(fredy_alive.fetch_states(["a"])) == {}
    fresh_vault.db.update_settings({"fredy_base_url": BASE})
    assert fredy_alive.configured()
    assert asyncio.run(fredy_alive.fetch_states([])) == {}  # nothing asked, nothing sent


@pytest.mark.parametrize("server, expected_log", [
    (FakeFredy(LISTINGS, token="fredy_" + "ff" * 32), "HTTP 401"),
    (FakeFredy(LISTINGS, tool_error=True), "tool error"),
])
def test_errors_give_empty_result_and_logs_without_secrets(fredy, caplog, server, expected_log):
    fredy(server)
    with caplog.at_level(logging.INFO):
        assert asyncio.run(fredy_alive.fetch_states(["CkL-VV8V9h9ibGF8hwv2r"])) == {}
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "app.fredy_alive")
    assert expected_log in ours
    assert TOKEN not in caplog.text and "fredy.example.com" not in ours  # httpx's own URL log is off in main.py


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ValueError("odd")])
def test_network_failures_never_raise(fredy, caplog, failure):
    def broken(request):
        raise failure
    fredy(broken)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(fredy_alive.fetch_states(["x"])) == {}
    assert type(failure).__name__ in caplog.text and TOKEN not in caplog.text


def test_unexpected_answer_format(fredy):
    server = FakeFredy(LISTINGS)
    original = server.answer

    def garbled(message):
        if "content" in message.get("result", {}):
            message["result"]["content"][0]["text"] = "Something else entirely"
        return original(message)
    server.answer = garbled
    fredy(server)
    assert asyncio.run(fredy_alive.fetch_states(["CkL-VV8V9h9ibGF8hwv2r"])) == {}


# --- run_once / loop -----------------------------------------------------------------

def test_run_once_applies_only_known_states(fredy):
    fredy(FakeFredy(LISTINGS))
    applied = []
    states = asyncio.run(fredy_alive.run_once(applied.append, lambda: ["jQEqH4Rgc0aonbuk7EFFs", "other"]))
    assert applied == [states] == [{"jQEqH4Rgc0aonbuk7EFFs": False}]
    applied.clear()
    asyncio.run(fredy_alive.run_once(applied.append, lambda: []))  # nothing to check: no request, no apply
    assert applied == []


def test_loop_keeps_going_after_errors(fredy, monkeypatch):
    fredy(FakeFredy(LISTINGS))
    sleeps, applied = [], []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 3:
            raise asyncio.CancelledError

    def apply(states):
        applied.append(states)
        if len(applied) == 1:
            raise RuntimeError("database locked")  # logged, the loop continues
    monkeypatch.setattr(fredy_alive.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fredy_alive.loop(apply, lambda: ["CkL-VV8V9h9ibGF8hwv2r"], every_s=5, first_delay_s=1))
    assert sleeps == [1, 5, 5, 5] and len(applied) == 3


# --- credential ------------------------------------------------------------------------

def test_mcp_token_credential(fresh_vault):
    assert vault.validate("fredy_mcp_token", f"  {TOKEN}\n") == TOKEN
    for bad in ["abc", "fredy_short", "Bearer " + TOKEN, "sk-ant-" + "x" * 40]:
        with pytest.raises(vault.InvalidValue):
            vault.validate("fredy_mcp_token", bad)
    vault.set("fredy_mcp_token", TOKEN)
    status = vault.credential_status("fredy_mcp_token")
    assert status["hint"] == "fred…ab12" and TOKEN not in repr(vault.status())
    assert TOKEN in vault.secret_values()  # scrubbed from texts like the other secrets
