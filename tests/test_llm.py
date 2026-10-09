import asyncio
import base64
import io
import json
import logging
from types import SimpleNamespace

import anthropic
import httpx
import httpx2
import pytest
from PIL import Image

from app import llm
from app.settings import DEFAULT_MODEL, DEFAULT_SETTINGS, MODELS, cost_usd

LISTING = {
    "fredy_id": "abc123",
    "provider": "immoscout",
    "title": "Helle 2-Zimmer-Wohnung mit Balkon",
    "price": "780 €",
    "size": "54 m²",
    "address": "Musterstr. 12, Berlin",
    "description": "Bitte Codewort {Linde} in der Anfrage nennen. {0} {name}",
    "image_url": "https://img.example/photo.jpg?token=SECRETQUERY",
    "url": "https://www.immobilienscout24.de/expose/123",
}


def settings_with(**overrides) -> dict:
    return {**DEFAULT_SETTINGS, "send_image": "0", **overrides}


# --- fakes ------------------------------------------------------------------

def fake_usage(input_tokens=1000, output_tokens=500, cache_read=None, cache_write=None):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def fake_response(draft="Guten Tag, ...", hint="", *, text=None, model="claude-opus-5-5", stop_reason="end_turn", usage=None):
    if text is None:
        text = json.dumps({"draft": draft, "hint": hint})
    return SimpleNamespace(
        model=model,
        stop_reason=stop_reason,
        stop_details=None,
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
        usage=usage or fake_usage(),
    )


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


class FakeClient:
    def __init__(self, response=None, error=None):
        self.messages = FakeMessages(response, error)
        self.beta = SimpleNamespace(messages=FakeMessages(response, error))


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient(fake_response())
    monkeypatch.setattr(llm, "_client", client)
    return client


def make_png(width, height) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", (width, height), (200, 100, 50, 255)).save(out, format="PNG")
    return out.getvalue()


# --- prompt building --------------------------------------------------------

def test_system_prompt_has_all_sections():
    prompt = llm.build_system_prompt(settings_with(
        profile="Name: Erika {Muster}", wishes="- Balkon {bitte}", style="Locker {%s}", target_words="120",
    ))
    assert prompt.startswith(DEFAULT_SETTINGS["system_prompt"][:40])
    assert "## Profil der suchenden Person\nName: Erika {Muster}" in prompt
    assert "## Wünsche\n- Balkon {bitte}" in prompt
    assert "## Schreibstil\nLocker {%s}\nZiellänge: ca. 120 Wörter." in prompt


def test_system_prompt_with_braces_in_custom_system_prompt():
    prompt = llm.build_system_prompt(settings_with(system_prompt="Du bist {role} und {0}."))
    assert prompt.startswith("Du bist {role} und {0}.")


def test_system_prompt_blank_system_prompt_falls_back_to_default():
    prompt = llm.build_system_prompt(settings_with(system_prompt="   "))
    assert prompt.startswith(DEFAULT_SETTINGS["system_prompt"])


def test_user_text_initial_contains_listing_block_and_task():
    text = llm.build_user_text(LISTING, "initial")
    assert text.startswith("<inserat>\n")
    inserat = text.split("</inserat>")[0]
    for expected in ["Titel: Helle 2-Zimmer-Wohnung", "Preis: 780 €", "Größe: 54 m²", "Adresse: Musterstr. 12",
                     "Portal: ImmoScout24", "Link: https://www.immobilienscout24.de/expose/123",
                     "Beschreibung:\nBitte Codewort {Linde} in der Anfrage nennen. {0} {name}"]:
        assert expected in inserat
    assert "<bisheriger_entwurf>" not in text
    assert "## Aufgabe\nSchreib die erste Kontaktanfrage" in text


def test_user_text_skips_missing_fields():
    text = llm.build_user_text({"title": "Nur Titel", "price": None}, "test")
    assert "Preis:" not in text and "Portal:" not in text
    assert "Beschreibung:\n(keine Beschreibung)" in text


@pytest.mark.parametrize("kind", ["regenerate", "shorter", "formal", "custom"])
def test_user_text_variants_include_previous_draft(kind):
    previous = "Sehr geehrte Frau {X}, ich interessiere mich ... Codewort Linde."
    text = llm.build_user_text(LISTING, kind, previous, "Erwähne {mein} Fahrrad")
    assert "<bisheriger_entwurf>\n" + previous + "\n</bisheriger_entwurf>" in text
    assert text.index("</inserat>") < text.index("<bisheriger_entwurf>") < text.index("## Aufgabe")


def test_user_text_variant_tasks():
    previous = " ".join(["Wort"] * 100)
    assert "60 %" in llm.build_user_text(LISTING, "shorter", previous)
    assert "ca. 60 Wörter" in llm.build_user_text(LISTING, "shorter", previous)
    assert "förmlicher" in llm.build_user_text(LISTING, "formal", previous)
    assert "deutlich vom bisherigen Entwurf" in llm.build_user_text(LISTING, "regenerate", previous)
    custom = llm.build_user_text(LISTING, "custom", previous, "Erwähne {mein} Fahrrad")
    assert "<anweisung>\nErwähne {mein} Fahrrad\n</anweisung>" in custom
    assert "Überarbeite den bisherigen Entwurf" in custom


@pytest.mark.parametrize("kind", ["regenerate", "shorter", "formal", "custom"])
def test_user_text_variant_without_previous_writes_fresh_draft(kind):
    text = llm.build_user_text(LISTING, kind, None, "Erwähne mein Fahrrad")
    assert "<bisheriger_entwurf>" not in text
    assert "Schreib die erste Kontaktanfrage" in text
    if kind == "custom":
        assert "<anweisung>\nErwähne mein Fahrrad\n</anweisung>" in text


def test_user_text_initial_ignores_previous_text():
    text = llm.build_user_text(LISTING, "initial", "alter Entwurf")
    assert "alter Entwurf" not in text


# --- response parsing and cost ----------------------------------------------

def test_cost_and_tokens_from_usage():
    usage = fake_usage(input_tokens=2000, output_tokens=800, cache_read=300, cache_write=None)
    result = llm._to_result(fake_response("Hallo", "  ", model="claude-sonnet-5-5", usage=usage), "claude-opus-5-5")
    assert result.text == "Hallo"
    assert result.hint is None
    assert result.model == "claude-sonnet-5-5"  # the model that answered, not the requested one
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens, result.cache_write_tokens) == (2000, 800, 300, 0)
    expected = (2000 * 2.00 + 800 * 10.00 + 300 * 0.20) / 1_000_000
    assert result.cost_usd == pytest.approx(expected)
    assert result.cost_usd == pytest.approx(cost_usd("claude-sonnet-5-5", 2000, 800, 300, 0))


def test_hint_is_kept_when_present():
    result = llm._to_result(fake_response("Hallo", "Codewort Linde nennen"), DEFAULT_MODEL)
    assert result.hint == "Codewort Linde nennen"


def test_refusal_raises_before_reading_content():
    response = fake_response(text="", stop_reason="refusal")
    response.content = []
    with pytest.raises(llm.LLMError, match="abgelehnt"):
        llm._to_result(response, DEFAULT_MODEL)


def test_max_tokens_raises():
    with pytest.raises(llm.LLMError, match="abgeschnitten"):
        llm._to_result(fake_response(text='{"draft": "Hal', stop_reason="max_tokens"), DEFAULT_MODEL)


@pytest.mark.parametrize("text", ["not json", "[]", '{"hint": "x"}', '{"draft": "   ", "hint": ""}'])
def test_bad_output_raises(text):
    with pytest.raises(llm.LLMError):
        llm._to_result(fake_response(text=text), DEFAULT_MODEL)


# --- request parameters -----------------------------------------------------

def test_generate_draft_opus_uses_beta_fallback_and_structured_output(fake_client):
    result = asyncio.run(llm.generate_draft(LISTING, settings_with(model="claude-opus-5-5", effort="medium"), "initial"))
    assert result.text == "Guten Tag, ..."
    assert fake_client.messages.calls == []
    (call,) = fake_client.beta.messages.calls
    assert call["model"] == "claude-opus-5-5"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["max_tokens"] == llm.MAX_TOKENS
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": llm.DRAFT_SCHEMA}
    assert call["system"].startswith(DEFAULT_SETTINGS["system_prompt"][:40])
    (message,) = call["messages"]
    assert message["role"] == "user"
    assert [block["type"] for block in message["content"]] == ["text"]  # send_image off


def test_generate_draft_haiku_has_no_fallback(fake_client):
    asyncio.run(llm.generate_draft(LISTING, settings_with(model="claude-haiku-5-5"), "initial"))
    assert fake_client.beta.messages.calls == []
    (call,) = fake_client.messages.calls
    assert call["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in call and "betas" not in call


def test_generate_draft_unknown_model_and_effort_use_defaults(fake_client):
    asyncio.run(llm.generate_draft(LISTING, settings_with(model="gpt-9", effort="extreme"), "initial"))
    (call,) = fake_client.beta.messages.calls
    assert call["model"] == DEFAULT_MODEL
    assert call["output_config"]["effort"] == "low"


def test_generate_draft_without_configure(monkeypatch):
    monkeypatch.setattr(llm, "_client", None)
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.generate_draft(LISTING, settings_with(), "initial"))


def _status_error(cls, status):
    response = httpx2.Response(status, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls("error", response=response, body=None)


@pytest.mark.parametrize("error, message", [
    (_status_error(anthropic.RateLimitError, 429), "Rate-Limit"),
    (_status_error(anthropic.AuthenticationError, 401), "API-Key"),
    (_status_error(anthropic.OverloadedError, 529), "überlastet"),
    (_status_error(anthropic.BadRequestError, 400), "HTTP 400"),
    (anthropic.APITimeoutError(request=httpx2.Request("POST", "https://api.anthropic.com")), "rechtzeitig"),
    (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")), "Verbindung"),
])
def test_sdk_errors_become_llm_errors(monkeypatch, error, message):
    monkeypatch.setattr(llm, "_client", FakeClient(error=error))
    with pytest.raises(llm.LLMError, match=message):
        asyncio.run(llm.generate_draft(LISTING, settings_with(), "initial"))


def test_request_through_real_sdk(monkeypatch):
    """Run the real AsyncAnthropic client against a mock transport and inspect the HTTP request."""
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [
                {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-5"}},
                {"type": "text", "text": json.dumps({"draft": "Guten Tag", "hint": "Codewort Linde"})},
            ],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 1200, "output_tokens": 300, "cache_read_input_tokens": None,
                      "cache_creation_input_tokens": 0},
        })

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            client = anthropic.AsyncAnthropic(api_key="sk-test", http_client=http_client, max_retries=0)
            monkeypatch.setattr(llm, "_client", client)
            return await llm.generate_draft(LISTING, settings_with(effort="low"), "initial")

    result = asyncio.run(run())
    assert captured["url"].startswith("https://api.anthropic.com/v1/messages")
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    body = captured["body"]
    assert body["fallbacks"] == "default"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": llm.DRAFT_SCHEMA}}
    assert "budget_tokens" not in json.dumps(body)
    assert result.text == "Guten Tag"
    assert result.hint == "Codewort Linde"
    assert result.model == "claude-opus-5"
    assert result.cost_usd == pytest.approx(cost_usd("claude-opus-5", 1200, 300))


def test_haiku_request_through_real_sdk(monkeypatch):
    captured = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx2.Response(200, json={
            "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
            "content": [{"type": "text", "text": json.dumps({"draft": "Hallo", "hint": ""})}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 900, "output_tokens": 250},
        })

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as http_client:
            client = anthropic.AsyncAnthropic(api_key="sk-test", http_client=http_client, max_retries=0)
            monkeypatch.setattr(llm, "_client", client)
            return await llm.generate_draft(LISTING, settings_with(model="claude-haiku-5-5"), "initial")

    result = asyncio.run(run())
    assert "anthropic-beta" not in captured["headers"]
    assert "fallbacks" not in captured["body"]
    assert captured["body"]["output_config"]["format"]["type"] == "json_schema"
    assert result.model == "claude-haiku-5-5"
    assert result.hint is None
    assert result.cost_usd == pytest.approx((900 * 0.10 + 250 * 0.50) / 1_000_000)


# --- image ------------------------------------------------------------------

def test_image_is_downloaded_and_shrunk(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, content=make_png(3000, 1500), headers={"content-type": "image/png"})

    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(handler))
    block = asyncio.run(llm.fetch_image_block("https://img.example/photo.png"))
    assert block["type"] == "image"
    assert block["source"]["media_type"] == "image/jpeg"
    with Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"]))) as img:
        assert img.format == "JPEG"
        assert img.mode == "RGB"
        assert img.size == (1024, 512)
    assert seen["ua"].startswith("Mozilla/5.0")


def test_small_image_is_not_enlarged(monkeypatch):
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(200, content=make_png(300, 200))))
    block = asyncio.run(llm.fetch_image_block("https://img.example/small.png"))
    with Image.open(io.BytesIO(base64.standard_b64decode(block["source"]["data"]))) as img:
        assert img.size == (300, 200)


def test_image_failure_is_logged_without_query_string(monkeypatch, caplog):
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(404)))
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        assert asyncio.run(llm.fetch_image_block(LISTING["image_url"])) is None
    assert "img.example" in caplog.text and "HTTP 404" in caplog.text
    assert "SECRETQUERY" not in caplog.text


@pytest.mark.parametrize("content", [b"not an image", b"\x89PNG broken"])
def test_broken_image_is_skipped(monkeypatch, content):
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(200, content=content)))
    assert asyncio.run(llm.fetch_image_block("https://img.example/x.png")) is None


def test_too_large_image_is_skipped(monkeypatch, caplog):
    big = b"\0" * (llm.IMAGE_MAX_BYTES + 1)
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(200, content=big)))
    with caplog.at_level(logging.WARNING, logger="app.llm"):
        assert asyncio.run(llm.fetch_image_block("https://img.example/x.png")) is None
    assert "larger than 10 MB" in caplog.text


def test_unsafe_image_url_is_not_fetched(monkeypatch):
    def handler(request):
        raise AssertionError("must not be fetched")

    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(handler))
    assert asyncio.run(llm.fetch_image_block("javascript:alert(1)")) is None
    assert asyncio.run(llm.fetch_image_block(None)) is None


def test_image_block_goes_before_text(monkeypatch, fake_client):
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(200, content=make_png(50, 50))))
    asyncio.run(llm.generate_draft(LISTING, settings_with(send_image="1"), "initial"))
    (call,) = fake_client.beta.messages.calls
    assert [block["type"] for block in call["messages"][0]["content"]] == ["image", "text"]


def test_failed_image_still_generates_draft(monkeypatch, fake_client):
    monkeypatch.setattr(llm, "_image_transport", httpx.MockTransport(lambda r: httpx.Response(500)))
    result = asyncio.run(llm.generate_draft(LISTING, settings_with(send_image="1"), "initial"))
    assert result.text
    (call,) = fake_client.beta.messages.calls
    assert [block["type"] for block in call["messages"][0]["content"]] == ["text"]


# --- create_and_store_draft -------------------------------------------------

@pytest.fixture
def listing_id(fresh_db):
    lid, created = fresh_db.insert_listing(LISTING)
    assert created
    return lid


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def generate(listing, settings, kind, previous_text=None, instruction=None):
        calls.append({"listing": listing, "kind": kind, "previous_text": previous_text, "instruction": instruction})
        return llm.DraftResult(
            text="Entwurf " + str(len(calls)), hint="Codewort" if kind == "initial" else None, model="claude-opus-5-5",
            input_tokens=1000, output_tokens=400, cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.012,
        )

    monkeypatch.setattr(llm, "generate_draft", generate)
    return calls


def test_create_and_store_draft_stores_row_and_sets_status(fresh_db, listing_id, fake_generate):
    row = asyncio.run(llm.create_and_store_draft(listing_id, "initial"))
    assert row["listing_id"] == listing_id
    assert row["kind"] == "initial"
    assert row["text"] == "Entwurf 1"
    assert row["hint"] == "Codewort"
    assert row["model"] == "claude-opus-5-5"
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 400)
    assert row["cost_usd"] == pytest.approx(0.012)
    assert row["instruction"] is None
    assert fresh_db.list_drafts(listing_id) == [row]
    assert fresh_db.get_listing(listing_id)["status"] == "drafted"
    assert fake_generate[0]["previous_text"] is None


def test_test_kind_does_not_change_status(fresh_db, listing_id, fake_generate):
    row = asyncio.run(llm.create_and_store_draft(listing_id, "test"))
    assert row["kind"] == "test"
    assert fresh_db.get_listing(listing_id)["status"] == "new"


def test_status_other_than_new_is_kept(fresh_db, listing_id, fake_generate):
    fresh_db.set_listing_status(listing_id, "sent")
    asyncio.run(llm.create_and_store_draft(listing_id, "initial"))
    assert fresh_db.get_listing(listing_id)["status"] == "sent"


def test_variants_get_latest_non_test_draft(fresh_db, listing_id, fake_generate):
    asyncio.run(llm.create_and_store_draft(listing_id, "initial"))   # Entwurf 1
    asyncio.run(llm.create_and_store_draft(listing_id, "test"))      # Entwurf 2, ignored for variants
    row = asyncio.run(llm.create_and_store_draft(listing_id, "custom", "  Erwähne mein Fahrrad " + "x" * 600))
    assert fake_generate[2]["kind"] == "custom"
    assert fake_generate[2]["previous_text"] == "Entwurf 1"
    assert len(row["instruction"]) == llm.INSTRUCTION_MAX_CHARS
    assert row["instruction"].startswith("Erwähne mein Fahrrad")
    assert fake_generate[2]["instruction"] == row["instruction"]
    asyncio.run(llm.create_and_store_draft(listing_id, "shorter"))
    assert fake_generate[3]["previous_text"] == "Entwurf 3"


def test_budget_exceeded(fresh_db, listing_id, fake_generate):
    fresh_db.update_settings({"monthly_budget_usd": "0,05"})
    fresh_db.add_draft(listing_id, "initial", "alt", "claude-opus-5-5", cost_usd=0.05)
    with pytest.raises(llm.BudgetExceeded) as exc_info:
        asyncio.run(llm.create_and_store_draft(listing_id, "regenerate"))
    assert str(exc_info.value) == "Monatsbudget von 5,0 ct erreicht – im Dashboard erhöhen."
    assert isinstance(exc_info.value, llm.LLMError)
    assert fake_generate == []
    assert len(fresh_db.list_drafts(listing_id)) == 1


def test_budget_zero_means_unlimited(fresh_db, listing_id, fake_generate):
    fresh_db.update_settings({"monthly_budget_usd": "0"})
    fresh_db.add_draft(listing_id, "initial", "alt", "claude-opus-5-5", cost_usd=100.0)
    asyncio.run(llm.create_and_store_draft(listing_id, "initial"))
    assert len(fake_generate) == 1


def test_missing_listing_and_bad_input(fresh_db, fake_generate):
    with pytest.raises(llm.LLMError, match="nicht gefunden"):
        asyncio.run(llm.create_and_store_draft(999, "initial"))
    lid, _ = fresh_db.insert_listing(LISTING)
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.create_and_store_draft(lid, "bogus"))
    with pytest.raises(llm.LLMError, match="Anweisung"):
        asyncio.run(llm.create_and_store_draft(lid, "custom", "   "))
    assert fake_generate == []


def test_end_to_end_with_fake_client_does_not_log_profile(fresh_db, listing_id, fake_client, caplog):
    fresh_db.update_settings({"profile": "Name: GEHEIMPROFIL", "send_image": "0"})
    fake_client.beta.messages.response = fake_response("Hallo", "", usage=fake_usage(2000, 1000))
    with caplog.at_level(logging.DEBUG):
        row = asyncio.run(llm.create_and_store_draft(listing_id, "initial"))
    assert row["text"] == "Hallo" and row["hint"] is None
    assert row["cost_usd"] == pytest.approx(cost_usd("claude-opus-5-5", 2000, 1000))
    assert "GEHEIMPROFIL" in fake_client.beta.messages.calls[0]["system"]
    assert "GEHEIMPROFIL" not in caplog.text
    assert "Codewort {Linde}" not in caplog.text
    assert f"draft listing={listing_id} kind=initial model=claude-opus-5-5" in caplog.text


def test_price_table_has_all_selectable_models():
    assert DEFAULT_MODEL in MODELS
    assert llm.FALLBACK_MODELS <= set(MODELS)
