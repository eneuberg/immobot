"""Tests for app/bot.py. No network: Telegram is replaced by small fakes, the LLM by a stub."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram import ForceReply
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError
from telegram.ext import ApplicationHandlerStop

from app import bot, llm, portals, vault
from tests.conftest import TEST_SECRETS

CHAT = 42
OWNER = 1001  # Telegram user id of the bot owner in these tests
STRANGER = 2002
TOKEN = TEST_SECRETS["TELEGRAM_BOT_TOKEN"]


# --- fakes -------------------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.username = "immo_test_bot"
        self.sent: list[dict] = []
        self.edits: list[dict] = []
        self.deleted: list[int] = []
        self._next_id = 100

    async def send_message(self, **kwargs):
        self._next_id += 1
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=self._next_id)

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs["message_id"])


class FakeQuery:
    def __init__(self, data, chat_id=CHAT, user_id=OWNER):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.from_user = SimpleNamespace(id=user_id)
        self.answers: list[str | None] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def callback_update(query):
    return SimpleNamespace(callback_query=query, message=None, effective_user=query.from_user)


def text_update(text, chat_id=CHAT, message_id=900, reply_to=None, user_id=OWNER):
    reply = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    user = SimpleNamespace(id=user_id)
    message = SimpleNamespace(text=text, message_id=message_id, chat=SimpleNamespace(id=chat_id), reply_to_message=reply,
                              from_user=user)
    return SimpleNamespace(callback_query=None, message=message, effective_user=user)


def listing_dict(**overrides):
    base = {
        "id": 7,
        "title": "Schöne 2-Zimmer-Wohnung",
        "price": "780 €",
        "size": "54 m²",
        "address": "Musterstr. 12, Berlin",
        "provider": "immoscout",
        "url": "https://www.immobilienscout24.de/expose/123",
        "status": "new",
        "tg_message_id": 555,
    }
    return {**base, **overrides}


def callback_data(markup):
    return [[b.callback_data for b in row] for row in markup.inline_keyboard]


@pytest.fixture
def tg(fresh_vault, monkeypatch):
    """Fake Telegram bot + our chat id (no allowlist); returns the fake bot."""
    monkeypatch.setattr(bot, "configured_chat_id", lambda: CHAT)
    monkeypatch.setattr(bot, "_locks", {})
    return FakeBot()


def add_listing(db, fredy_id="f1", tg_message_id=555, **fields):
    data = {"fredy_id": fredy_id, "title": "Helle Wohnung", "price": "900 €", "size": "60 m²",
            "address": "Hauptstr. 1", "provider": "immoscout", "url": "https://example.org/expose/1", **fields}
    listing_id, _ = db.insert_listing(data)
    if tg_message_id:
        db.set_listing_tg_message(listing_id, tg_message_id)
    return listing_id


def fake_llm(monkeypatch, db, text="Guten Tag,\nich interessiere mich für die Wohnung.", error=None):
    """Replace llm.create_and_store_draft; returns the list of recorded calls."""
    calls = []

    async def create_and_store_draft(listing_id, kind, instruction=None):
        calls.append((listing_id, kind, instruction))
        if error is not None:
            raise error
        db.add_draft(listing_id, kind, text, "claude-opus-5-5", hint="Codewort „Linde“", instruction=instruction, cost_usd=0.042)
        return db.latest_draft(listing_id)

    monkeypatch.setattr(llm, "create_and_store_draft", create_and_store_draft)
    return calls


def no_sleep(monkeypatch):
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return sleeps


# --- message building --------------------------------------------------------

def test_header_escapes_listing_text():
    listing = listing_dict(title="<script>alert(1)</script> & Co", address="A <b>&</b> B")
    text = bot.header(listing)
    assert "<script>" not in text and "<b>&</b>" not in text
    assert "🏠 <b>&lt;script&gt;alert(1)&lt;/script&gt; &amp; Co</b>" in text
    assert "A &lt;b&gt;&amp;&lt;/b&gt; B" in text


def test_header_layout_skips_missing_parts():
    text = bot.header(listing_dict(price=None, size="54 m²", address="  "))
    assert text.split("\n") == ["🏠 <b>Schöne 2-Zimmer-Wohnung</b>", "54 m²", portals.provider_label("immoscout")]
    text = bot.header(listing_dict(price=None, size=None, address=None))
    assert len(text.split("\n")) == 2


def test_header_truncates_long_title():
    text = bot.header(listing_dict(title="Wohnung " * 40))
    title_line = text.split("\n")[0]
    assert len(title_line) < 100 and title_line.endswith("…</b>")


def test_draft_view_contents_and_escaping():
    draft = {"text": "Hallo <Team> & so", "hint": "Codewort <Linde>", "model": "claude-opus-5-5", "cost_usd": 0.042, "kind": "initial"}
    text, _ = bot.draft_view(listing_dict(), draft)
    assert "\n⚠️ Codewort &lt;Linde&gt;" in text
    assert "\n\n<pre>Hallo &lt;Team&gt; &amp; so</pre>" in text
    assert text.endswith("<i>Entwurf · Claude Opus 5.5 · Anthropic · 4,2 ct ($)</i>")
    assert "gekürzt" not in text


def test_draft_view_unknown_model_shows_raw_name():
    draft = {"text": "x", "hint": None, "model": "claude-foo", "cost_usd": 0, "kind": "custom"}
    text, _ = bot.draft_view(listing_dict(), draft)
    assert "⚠️" not in text
    assert "<i>Angepasst · claude-foo · Anthropic · 0,0 ct ($)</i>" in text


def test_draft_view_shows_provider_and_both_currencies():
    draft = {"text": "x", "hint": None, "model": "claude-sonnet-5.5", "kind": "initial", "provider": "deutschlandgpt",
             "currency": "EUR", "cost_native": 0.021, "cost_usd": 0.0231}
    text, _ = bot.draft_view(listing_dict(), draft)
    assert text.endswith("<i>Entwurf · Claude Sonnet 5.5 · DeutschlandGPT · 2,1 ct (€) ≈ 2,3 ct ($)</i>")


@pytest.mark.parametrize("unit", ["a", "&", "ü", "😀"])
def test_draft_view_truncates_to_telegram_limit(unit):
    draft = {"text": unit * 6000, "hint": "h" * 1000, "model": "claude-opus-5-5", "cost_usd": 0.1, "kind": "initial"}
    text, _ = bot.draft_view(listing_dict(title="T" * 500), draft, note="Budget erreicht")
    assert len(text) <= bot.MAX_MESSAGE_LEN
    assert bot._tg_len(text) <= bot.MAX_MESSAGE_LEN
    assert bot._tg_len(text) > bot.MAX_MESSAGE_LEN - 200  # but not cut much more than needed
    assert "</pre>\n… (gekürzt – vollständig im Dashboard)" in text
    assert text.endswith("\n\n⚠️ Budget erreicht")
    assert text.count("<pre>") == 1 and text.count("</pre>") == 1


def test_followup_generating_and_collapsed_views():
    listing = listing_dict(id=12)
    text, markup = bot.followup_view(listing)
    assert text == bot.header(listing)
    assert callback_data(markup) == [["gen:12", "skip:12"], ["img:12"]]  # IS24 listing: pictures button
    assert bot.generating_text(listing).endswith("\n\n⏳ schreibe Entwurf …")

    text, markup = bot.collapsed_view(listing_dict(id=12, status="sent", title="A & B"))
    assert text == "✅ Gesendet · A &amp; B"
    assert callback_data(markup) == [["undo:12"]]
    text, _ = bot.collapsed_view(listing_dict(id=12, status="dismissed"))
    assert text.startswith("❌ Uninteressant · ")


def test_draft_keyboard_layout_with_contact_url(monkeypatch):
    monkeypatch.setattr(portals, "contact_url", lambda provider, url: "https://example.org/contact")
    markup = bot.draft_keyboard(listing_dict(id=12))
    rows = markup.inline_keyboard
    assert [b.text for b in rows[0]] == ["🔁 Neu", "✂️ Kürzer", "🎩 Förmlicher"]
    assert [b.callback_data for b in rows[0]] == ["new:12", "short:12", "formal:12"]
    assert rows[1][0].callback_data == "edit:12"
    assert rows[1][1].text == "📨 Inserat öffnen ↗" and rows[1][1].url == "https://example.org/contact"
    assert [b.callback_data for b in rows[2]] == ["sent:12", "skip:12"]


def test_draft_keyboard_omits_url_button_without_contact_url(monkeypatch):
    monkeypatch.setattr(portals, "contact_url", lambda provider, url: None)
    rows = bot.draft_keyboard(listing_dict(id=12)).inline_keyboard
    assert len(rows) == 4
    assert [b.callback_data for b in rows[1]] == ["edit:12"]
    assert [b.callback_data for b in rows[3]] == ["img:12"]


def test_all_callback_data_fits_telegram_limit():
    listing = listing_dict(id=999_999_999_999)
    markups = [bot.followup_view(listing)[1], bot.draft_keyboard(listing), bot.collapsed_view(listing)[1]]
    for markup in markups:
        for row in markup.inline_keyboard:
            for button in row:
                if button.callback_data:
                    assert len(button.callback_data.encode()) <= 64
                    assert bot.parse_callback(button.callback_data) is not None


# --- input checks ------------------------------------------------------------

@pytest.mark.parametrize("data,expected", [
    ("gen:12", ("gen", 12)),
    ("undo:1", ("undo", 1)),
    ("formal:999999999999", ("formal", 999999999999)),
])
def test_parse_callback_valid(data, expected):
    assert bot.parse_callback(data) == expected


@pytest.mark.parametrize("data", [
    None, 12, "", "gen", "gen:", ":12", "gen:abc", "foo:12", "GEN:12", "gen:12:3", "gen:-1", "gen:0",
    "gen:012", "gen:12\n", " gen:12", "gen:١٢", "gen:" + "9" * 13, "gen;12",
])
def test_parse_callback_rejects_malformed(data):
    assert bot.parse_callback(data) is None


def test_is_allowed_chat():
    assert bot.is_allowed_chat(text_update("hi", chat_id=CHAT), CHAT)
    assert not bot.is_allowed_chat(text_update("hi", chat_id=43), CHAT)
    assert bot.is_allowed_chat(callback_update(FakeQuery("gen:1", chat_id=CHAT)), CHAT)
    assert not bot.is_allowed_chat(callback_update(FakeQuery("gen:1", chat_id=43)), CHAT)
    inline_query = FakeQuery("gen:1")
    inline_query.message = None  # e.g. button on an inline message: no chat to check
    assert not bot.is_allowed_chat(callback_update(inline_query), CHAT)
    assert not bot.is_allowed_chat(SimpleNamespace(callback_query=None, message=None), CHAT)
    assert not bot.is_allowed_chat(text_update("hi", chat_id=CHAT), None)


def run_guard(update, tg=None) -> bool:
    """Run the guard like PTB does; True if the update goes on to the other handlers."""
    try:
        asyncio.run(bot._guard(update, SimpleNamespace(bot=tg or FakeBot())))
    except ApplicationHandlerStop:
        return False
    return True


def test_guard_drops_foreign_updates(fresh_vault, monkeypatch):
    monkeypatch.setattr(bot, "configured_chat_id", lambda: CHAT)
    monkeypatch.setitem(bot._state, "last_update_at", None)
    assert not run_guard(text_update("hi", chat_id=43))
    assert bot.status()["last_update_at"] is not None
    assert run_guard(text_update("hi", chat_id=CHAT))


# --- follow-up announcements -------------------------------------------------

def test_announce_sends_followups_and_stores_message_ids(tg, monkeypatch):
    db = bot.db
    sleeps = no_sleep(monkeypatch)
    first = add_listing(db, "f1", tg_message_id=None, title="Erste <Wohnung>")
    second = add_listing(db, "f2", tg_message_id=None)

    asyncio.run(bot.announce(tg, CHAT, [first, second]))

    assert len(tg.sent) == 2
    assert sleeps[0] == pytest.approx(4 + 2 * bot.FREDY_SEND_GAP_S)
    assert sleeps[1:] == [bot.SEND_GAP_S]
    call = tg.sent[0]
    assert call["chat_id"] == CHAT and call["parse_mode"] == "HTML"
    assert call["link_preview_options"].is_disabled is True
    assert "Erste &lt;Wohnung&gt;" in call["text"]
    assert callback_data(call["reply_markup"]) == [[f"gen:{first}", f"skip:{first}"]]
    assert db.get_listing(first)["tg_message_id"] == 101
    assert db.get_listing(second)["tg_message_id"] == 102


def test_announce_paused_sends_nothing(tg, monkeypatch):
    db = bot.db
    no_sleep(monkeypatch)
    listing_id = add_listing(db, tg_message_id=None)
    db.update_settings({"paused": "1"})
    asyncio.run(bot.announce(tg, CHAT, [listing_id]))
    assert tg.sent == []
    assert db.get_listing(listing_id)["tg_message_id"] is None


def test_announce_skips_unknown_and_already_announced(tg, monkeypatch):
    db = bot.db
    no_sleep(monkeypatch)
    done = add_listing(db, "f1", tg_message_id=77)
    asyncio.run(bot.announce(tg, CHAT, [done, 12345]))
    assert tg.sent == []


def test_schedule_announce_runs_in_background(tg, monkeypatch):
    db = bot.db
    no_sleep(monkeypatch)
    listing_id = add_listing(db, tg_message_id=None)
    monkeypatch.setattr(bot, "_app", SimpleNamespace(bot=tg, running=True))

    async def main():
        bot.schedule_announce([listing_id])
        assert tg.sent == []  # returned without waiting
        assert len(bot._tasks) == 1
        await asyncio.gather(*bot._tasks)

    asyncio.run(main())
    assert len(tg.sent) == 1
    assert db.get_listing(listing_id)["tg_message_id"] == 101
    assert bot._tasks == set()


def test_schedule_announce_without_running_bot_is_noop(monkeypatch):
    monkeypatch.setattr(bot, "_app", None)
    bot.schedule_announce([1, 2])  # no event loop needed, no exception
    assert bot._tasks == set()


# --- button handling ---------------------------------------------------------

def press(tg, data):
    query = FakeQuery(data)
    asyncio.run(bot._on_callback(callback_update(query), SimpleNamespace(bot=tg)))
    return query


def test_gen_shows_generating_then_draft(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db)
    calls = fake_llm(monkeypatch, db, text="Guten Tag <Frau Müller>")

    query = press(tg, f"gen:{listing_id}")

    assert query.answers == [None]
    assert calls == [(listing_id, "initial", None)]
    assert tg.edits[0]["text"].endswith("⏳ schreibe Entwurf …")
    # buttons stay while generating, so the message is still usable if the final edit never happens
    assert tg.edits[0]["reply_markup"].inline_keyboard[0][0].callback_data == f"gen:{listing_id}"
    final = tg.edits[-1]
    assert final["chat_id"] == CHAT and final["message_id"] == 555 and final["parse_mode"] == "HTML"
    assert "<pre>Guten Tag &lt;Frau Müller&gt;</pre>" in final["text"]
    assert final["reply_markup"].inline_keyboard[0][0].callback_data == f"new:{listing_id}"
    assert db.get_listing(listing_id)["status"] == "drafted"


@pytest.mark.parametrize("action,kind", [("new", "regenerate"), ("short", "shorter"), ("formal", "formal")])
def test_variant_buttons_use_matching_kind(tg, monkeypatch, action, kind):
    db = bot.db
    listing_id = add_listing(db)
    calls = fake_llm(monkeypatch, db)
    press(tg, f"{action}:{listing_id}")
    assert calls == [(listing_id, kind, None)]


def test_llm_error_restores_view_with_warning(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db)
    fake_llm(monkeypatch, db, error=llm.BudgetExceeded("Monatsbudget von 15 $ erreicht."))

    press(tg, f"gen:{listing_id}")

    final = tg.edits[-1]
    assert final["text"].endswith("\n\n⚠️ Monatsbudget von 15 $ erreicht.")
    assert callback_data(final["reply_markup"]) == [[f"gen:{listing_id}", f"skip:{listing_id}"]]
    assert db.get_listing(listing_id)["status"] == "new"


def test_unexpected_error_restores_draft_view_with_generic_warning(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db)
    db.add_draft(listing_id, "initial", "Alter Entwurf", "claude-opus-5-5")
    db.set_listing_status(listing_id, "drafted")
    fake_llm(monkeypatch, db, error=RuntimeError("boom"))

    press(tg, f"short:{listing_id}")

    final = tg.edits[-1]
    assert "<pre>Alter Entwurf</pre>" in final["text"]
    assert final["text"].endswith("\n\n⚠️ Fehler beim Generieren – bitte nochmal versuchen.")


def test_double_tap_does_not_generate_twice(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db)
    release = asyncio.Event()
    calls = []

    async def slow_create(listing_id, kind, instruction=None):
        calls.append(kind)
        await release.wait()
        db.add_draft(listing_id, kind, "Text", "claude-opus-5-5")
        return db.latest_draft(listing_id)

    monkeypatch.setattr(llm, "create_and_store_draft", slow_create)
    context = SimpleNamespace(bot=tg)

    async def main():
        first = asyncio.create_task(bot._on_callback(callback_update(FakeQuery(f"gen:{listing_id}")), context))
        while not calls:
            await asyncio.sleep(0)
        second = FakeQuery(f"gen:{listing_id}")
        await bot._on_callback(callback_update(second), context)
        release.set()
        await first
        return second

    second = asyncio.run(main())
    assert calls == ["initial"]
    assert second.answers == ["⏳ läuft schon …"]


def test_sent_skip_and_undo(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db, title="A & B")

    press(tg, f"sent:{listing_id}")
    assert db.get_listing(listing_id)["status"] == "sent"
    assert tg.edits[-1]["text"] == "✅ Gesendet · A &amp; B"
    assert callback_data(tg.edits[-1]["reply_markup"]) == [[f"undo:{listing_id}"]]

    press(tg, f"undo:{listing_id}")  # no draft yet -> back to "new" + follow-up buttons
    assert db.get_listing(listing_id)["status"] == "new"
    assert callback_data(tg.edits[-1]["reply_markup"]) == [[f"gen:{listing_id}", f"skip:{listing_id}"]]

    db.add_draft(listing_id, "test", "Testentwurf", "claude-opus-5-5")  # test drafts don't count
    press(tg, f"skip:{listing_id}")
    assert db.get_listing(listing_id)["status"] == "dismissed"
    assert tg.edits[-1]["text"].startswith("❌ Uninteressant · ")
    press(tg, f"undo:{listing_id}")
    assert db.get_listing(listing_id)["status"] == "new"

    db.add_draft(listing_id, "initial", "Echter Entwurf", "claude-opus-5-5")
    press(tg, f"skip:{listing_id}")
    press(tg, f"undo:{listing_id}")  # with a draft -> back to "drafted" + draft view
    assert db.get_listing(listing_id)["status"] == "drafted"
    assert "<pre>Echter Entwurf</pre>" in tg.edits[-1]["text"]


def test_unknown_listing_or_missing_message_answers_error(tg):
    db = bot.db
    query = press(tg, "gen:12345")
    assert query.answers == ["Inserat nicht gefunden."]
    listing_id = add_listing(db, tg_message_id=None)
    query = press(tg, f"sent:{listing_id}")
    assert query.answers == ["Inserat nicht gefunden."]
    assert tg.edits == []
    assert db.get_listing(listing_id)["status"] == "new"


def test_malformed_callback_is_only_answered(tg):
    query = press(tg, "drop table:1")
    assert query.answers == [None]
    assert tg.edits == [] and tg.sent == []


def test_message_not_modified_is_ignored(tg):
    db = bot.db
    listing_id = add_listing(db)

    async def edit_message_text(**kwargs):
        raise BadRequest("Message is not modified: specified new message content and reply markup are exactly the same")

    tg.edit_message_text = edit_message_text
    db.set_listing_status(listing_id, "sent")
    press(tg, f"sent:{listing_id}")  # no exception


# --- "Anpassen" flow ---------------------------------------------------------

def test_edit_asks_for_instruction_and_reply_generates_custom_draft(tg, monkeypatch):
    db = bot.db
    listing_id = add_listing(db, title="Wohnung <mit> Balkon")
    calls = fake_llm(monkeypatch, db, text="Angepasster Text")

    press(tg, f"edit:{listing_id}")
    prompt = tg.sent[-1]
    assert prompt["text"] == "✏️ Was soll am Entwurf für „Wohnung &lt;mit&gt; Balkon“ anders sein?"
    assert isinstance(prompt["reply_markup"], ForceReply)
    assert prompt["reply_markup"].input_field_placeholder
    prompt_id = 101

    reply = text_update("  Bitte Homeoffice erwähnen " + "x" * 600, message_id=900, reply_to=prompt_id)
    asyncio.run(bot._on_reply(reply, SimpleNamespace(bot=tg)))

    assert len(calls) == 1
    _, kind, instruction = calls[0]
    assert kind == "custom"
    assert instruction.startswith("Bitte Homeoffice erwähnen") and len(instruction) == bot.INSTRUCTION_MAX
    assert tg.edits[0]["text"].endswith("⏳ schreibe Entwurf …") and tg.edits[0]["message_id"] == 555
    assert "<pre>Angepasster Text</pre>" in tg.edits[-1]["text"]
    assert tg.deleted == [prompt_id, 900]
    assert db.pop_pending_prompt(prompt_id) is None


def test_reply_to_other_message_is_ignored(tg, monkeypatch):
    calls = fake_llm(monkeypatch, bot.db)
    asyncio.run(bot._on_reply(text_update("hallo", reply_to=4711), SimpleNamespace(bot=tg)))
    assert calls == [] and tg.edits == [] and tg.deleted == []


@pytest.mark.parametrize("chat_id,user_id", [(43, OWNER), (CHAT, STRANGER)])
def test_reply_from_other_chat_or_user_does_not_touch_prompts(tg, monkeypatch, chat_id, user_id):
    """Prompts are looked up by bare message id, so _on_reply checks chat and allowlist itself."""
    db = bot.db
    listing_id = add_listing(db)
    calls = fake_llm(monkeypatch, db)
    db.add_pending_prompt(101, listing_id)
    bot.vault.set("telegram_allowed_user_ids", str(OWNER))
    asyncio.run(bot._on_reply(text_update("anders", chat_id=chat_id, reply_to=101, user_id=user_id), SimpleNamespace(bot=tg)))
    assert calls == [] and tg.deleted == []
    assert db.pop_pending_prompt(101) == listing_id  # still waiting for the owner


# --- lifecycle / status ------------------------------------------------------

def test_describe_never_contains_token(monkeypatch):
    monkeypatch.setattr(bot, "_token", TOKEN)
    text = bot._describe(InvalidToken(f"The token `{TOKEN}` was rejected by the server."))
    assert TOKEN not in text
    assert text.startswith("InvalidToken: ")


def test_build_application_registers_handlers(monkeypatch):
    monkeypatch.setattr(bot, "_token", TOKEN)
    app = bot._build_application()  # no network
    assert app.concurrent_updates > 1
    assert -1 in app.handlers and len(app.handlers[0]) == 3  # /start, buttons, replies ("/pair" lives in the guard)
    assert app.error_handlers


def test_start_failure_is_recorded_without_token_and_stop_is_tolerant(fresh_vault, monkeypatch, caplog):
    from telegram.ext import Application

    async def failing_initialize(self):
        raise InvalidToken(f"The token `{TOKEN}` was rejected by the server.")

    monkeypatch.setattr(Application, "initialize", failing_initialize)
    monkeypatch.setitem(bot._state, "last_error", None)
    monkeypatch.setattr(bot, "_token", "")  # start() sets it; restore it afterwards
    vault.init(TEST_SECRETS["SESSION_SECRET"], {"telegram_bot_token": TOKEN, "telegram_chat_id": str(CHAT)})

    async def main():
        await bot.start()  # must not raise
        state = bot.status()
        await bot.stop()  # cancels the retry, must not raise
        return state

    state = asyncio.run(main())
    assert state["running"] is False
    assert state["last_error"] and "InvalidToken" in state["last_error"]
    assert TOKEN not in state["last_error"]
    assert TOKEN not in caplog.text
    assert bot.status()["running"] is False


def test_start_without_token_stays_off(fresh_vault, monkeypatch):
    monkeypatch.setattr(bot, "_token", "")

    async def must_not_run():
        raise AssertionError("no token -> no Telegram connection attempt")

    monkeypatch.setattr(bot, "_try_start", must_not_run)
    asyncio.run(bot.start())
    assert bot.status()["running"] is False and bot._start_retry is None


def test_start_without_chat_id_still_polls(fresh_vault, monkeypatch):
    """Pairing needs a running bot, so a missing chat id must not keep it from starting."""
    monkeypatch.setattr(bot, "_token", "")
    started = []

    async def fake_try_start():
        started.append(bot._token)
        return True

    monkeypatch.setattr(bot, "_try_start", fake_try_start)
    fresh_vault.set("telegram_bot_token", "123456:" + "A" * 35)
    asyncio.run(bot.start())
    assert started == ["123456:" + "A" * 35]


def test_reload_restarts_with_current_token(fresh_vault, monkeypatch):
    monkeypatch.setattr(bot, "_token", "")
    calls = []

    async def fake_stop():
        calls.append("stop")

    async def fake_try_start():
        calls.append(("start", bot._token))
        return True

    monkeypatch.setattr(bot, "stop", fake_stop)
    monkeypatch.setattr(bot, "_try_start", fake_try_start)
    fresh_vault.set("telegram_bot_token", "111:" + "b" * 32)
    asyncio.run(bot.reload())
    fresh_vault.set("telegram_bot_token", "222:" + "c" * 32)
    asyncio.run(bot.reload())
    assert calls == ["stop", ("start", "111:" + "b" * 32), "stop", ("start", "222:" + "c" * 32)]


def test_configured_chat_id_is_read_fresh(fresh_vault):
    assert bot.configured_chat_id() is None
    fresh_vault.set("telegram_chat_id", "-100123")
    assert bot.configured_chat_id() == -100123
    fresh_vault.clear("telegram_chat_id")
    assert bot.configured_chat_id() is None


def test_schedule_announce_without_chat_is_noop(fresh_vault, monkeypatch):
    monkeypatch.setattr(bot, "_app", SimpleNamespace(bot=FakeBot(), running=True))
    bot.schedule_announce([1])  # no chat connected: nothing scheduled, no exception
    assert bot._tasks == set()


# --- pairing -----------------------------------------------------------------

def pair_update(text, chat_id, user_id=OWNER, *, is_bot=False, sender_chat=None, chat_type="private"):
    replies = []

    async def reply_text(reply, **kwargs):
        replies.append(reply)

    user = SimpleNamespace(id=user_id, is_bot=is_bot, full_name="Erika <b>Muster</b>", username="erika_m")
    chat = SimpleNamespace(id=chat_id, type=chat_type, effective_name="Wohnungssuche")
    message = SimpleNamespace(text=text, message_id=1, chat=chat, reply_to_message=None, reply_text=reply_text,
                              from_user=user, sender_chat=sender_chat)
    return SimpleNamespace(callback_query=None, message=message, effective_user=user), replies


def send_pair(tg, code, chat_id, user_id=OWNER, command="/pair@immo_test_bot", **kwargs):
    """Send "/pair CODE" through the guard. Returns the replies; asserts it never reaches other handlers."""
    update, replies = pair_update(f"{command} {code}".strip(), chat_id, user_id, **kwargs)
    assert run_guard(update, tg) is False
    return replies


@pytest.mark.parametrize("text,expected", [
    ("/pair abc", True), ("/pair", True), ("/pair@immo_test_bot abc", True), ("/pair@IMMO_TEST_BOT abc", True),
    ("  /pair x", True), ("/PAIR x", True), ("/pair@other_bot abc", False),
    ("/start", False), ("/pairing x", False), ("pair abc", False), ("hallo /pair", False), ("", False), (None, False),
])
def test_is_pair_command(text, expected):
    assert bot.is_pair_command(text_update(text), "immo_test_bot") is expected


def test_is_pair_command_ignores_button_presses():
    assert bot.is_pair_command(callback_update(FakeQuery("gen:1")), "immo_test_bot") is False


def test_pair_traffic_never_reaches_other_handlers(fresh_vault):
    fresh_vault.set("telegram_chat_id", str(CHAT))
    owner_pair, replies = pair_update("/pair 1234abcd", CHAT)
    assert not run_guard(owner_pair) and replies == [bot.PAIR_FAILED]  # even from the owner, even with no pairing
    assert not run_guard(text_update("/pair 1234abcd", chat_id=43, reply_to=101))  # foreign "/pair" replies too
    fresh_vault.start_pairing("immo_test_bot")
    assert not run_guard(text_update("/pair 1234abcd", chat_id=43, reply_to=101))
    assert not run_guard(text_update("hallo", chat_id=43))  # everything else from a foreign chat is still dropped
    assert not run_guard(callback_update(FakeQuery("gen:1", chat_id=43)))
    assert run_guard(text_update("hallo", chat_id=CHAT))  # our own chat as before
    assert not run_guard(text_update("/pair@other_bot x", chat_id=43))  # another bot's command: just dropped
    assert fresh_vault.pairing_pending()  # ... and it didn't count as a wrong code


def test_pair_with_valid_code_connects_chat_and_tells_both_chats(fresh_vault):
    tg = FakeBot()
    fresh_vault.set("telegram_chat_id", str(CHAT))
    code = fresh_vault.start_pairing("immo_test_bot")
    replies = send_pair(tg, code, chat_id=-100777, chat_type="group")
    assert replies == [bot.PAIR_OK]
    assert fresh_vault.get("telegram_chat_id") == "-100777" and bot.configured_chat_id() == -100777
    assert not fresh_vault.pairing_pending()
    detail = "Chat „Wohnungssuche“ (group, -100777) · User Erika <b>Muster</b> (@erika_m, 1001)"
    assert [m["chat_id"] for m in tg.sent] == [-100777, CHAT]  # the new chat and the replaced one
    for notice in tg.sent:
        assert "Telegram-Chat-ID" in notice["text"] and notice["text"].endswith("\n" + detail)
        assert "parse_mode" not in notice  # plain text: Telegram names can't inject markup
    events = bot.db.list_credential_events()
    assert [(e["name"], e["action"], e["detail"]) for e in events] == [
        ("telegram_allowed_user_ids", "pair", detail), ("telegram_chat_id", "pair", detail)]
    assert bot.allowed_user_ids() == {OWNER}
    assert send_pair(tg, code, chat_id=666) == []  # a code works only once; strangers get no answer
    assert fresh_vault.get("telegram_chat_id") == "-100777"


def test_first_pairing_is_announced_in_the_new_chat(fresh_vault):
    tg = FakeBot()
    code = fresh_vault.start_pairing("immo_test_bot")
    assert send_pair(tg, code.upper(), chat_id=CHAT, command="/pair") == [bot.PAIR_OK]  # case-insensitive, bare /pair
    assert fresh_vault.get("telegram_chat_id") == str(CHAT)
    assert [m["chat_id"] for m in tg.sent] == [CHAT]


@pytest.mark.parametrize("kwargs", [{"sender_chat": SimpleNamespace(id=-100777)}, {"is_bot": True}])
def test_pair_refuses_anonymous_and_bot_senders_without_using_the_code(fresh_vault, kwargs):
    tg = FakeBot()
    code = fresh_vault.start_pairing("immo_test_bot")
    assert send_pair(tg, code, chat_id=-100777, **kwargs) == [bot.PAIR_NEEDS_USER]
    assert fresh_vault.get("telegram_chat_id") is None and fresh_vault.get("telegram_allowed_user_ids") is None
    assert fresh_vault.pairing_pending() and tg.sent == []
    assert send_pair(tg, code, chat_id=-100777) == [bot.PAIR_OK]  # a normal account can still use it


def test_pair_wrong_codes_silent_to_strangers_and_cancel_after_five(fresh_vault):
    tg = FakeBot()
    code = fresh_vault.start_pairing("immo_test_bot")
    for _ in range(vault.PAIRING_MAX_FAILURES - 1):
        assert send_pair(tg, "00000000", chat_id=43) == []  # no answer: doesn't reveal a pending pairing
        assert fresh_vault.pairing_pending()
    assert send_pair(tg, "", chat_id=43) == []  # a missing code counts too
    assert not fresh_vault.pairing_pending()
    assert send_pair(tg, code, chat_id=43) == []  # the right code is useless now
    assert fresh_vault.get("telegram_chat_id") is None and tg.sent == []


def test_pair_wrong_code_from_owner_gets_an_answer(fresh_vault):
    fresh_vault.set("telegram_chat_id", str(CHAT))
    fresh_vault.start_pairing("immo_test_bot")
    assert send_pair(FakeBot(), "00000000", chat_id=CHAT) == [bot.PAIR_FAILED]
    assert send_pair(FakeBot(), "00000000", chat_id=CHAT, user_id=STRANGER) == [bot.PAIR_FAILED]  # no allowlist yet
    fresh_vault.set("telegram_allowed_user_ids", str(OWNER))
    assert send_pair(FakeBot(), "00000000", chat_id=CHAT, user_id=STRANGER) == []  # not on the allowlist: silence


def test_pair_expired_code_rejected(fresh_vault, monkeypatch):
    tg = FakeBot()
    code = fresh_vault.start_pairing("immo_test_bot")
    later = vault.time.time() + vault.PAIRING_TTL_S + 1
    monkeypatch.setattr(vault.time, "time", lambda: later)
    assert send_pair(tg, code, chat_id=CHAT) == []
    assert fresh_vault.get("telegram_chat_id") is None


def test_pairing_detail_shortens_names():
    chat = SimpleNamespace(id=-1, type="supergroup", effective_name="G" * 200)
    user = SimpleNamespace(id=5, full_name="U" * 200, username=None)
    text = bot.pairing_detail(chat, user)
    assert len(text) < 200 and "(supergroup, -1)" in text and text.endswith("… (5)")


# --- notices & connection test ------------------------------------------------

def test_change_notice_names_credential_but_no_value(fresh_vault):
    fresh_vault.set("anthropic_api_key", "sk-ant-NOTICE-SECRET")
    text = bot.change_notice("anthropic_api_key", "set", "Dashboard")
    assert text.startswith("🔐 Zugang geändert: Anthropic-API-Key – gespeichert (Dashboard, ")
    assert "NOTICE-SECRET" not in text


def test_notify_uses_running_bot_and_never_raises(monkeypatch):
    tg = FakeBot()
    monkeypatch.setattr(bot, "_app", None)
    asyncio.run(bot.notify("x", CHAT))  # no bot: no-op
    monkeypatch.setattr(bot, "_app", SimpleNamespace(bot=tg, running=True))
    asyncio.run(bot.notify("x", None))  # no chat: no-op
    assert tg.sent == []
    asyncio.run(bot.notify("🔐 test", CHAT))
    assert tg.sent[0]["chat_id"] == CHAT and tg.sent[0]["text"] == "🔐 test"

    async def failing_send(**kwargs):
        raise NetworkError("down")

    tg.send_message = failing_send
    monkeypatch.setitem(bot._state, "last_error", None)
    asyncio.run(bot.notify("x", CHAT))  # recorded, not raised
    assert "NetworkError" in bot.status()["last_error"]


class FakeTelegramBot:
    """Stands in for telegram.Bot in check_connection()."""
    fail_enter = None
    fail_get_chat = None

    def __init__(self, token):
        self.token = token
        self.username = "immo_test_bot"

    async def __aenter__(self):
        if self.fail_enter:
            raise self.fail_enter
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_chat(self, chat_id):
        if self.fail_get_chat:
            raise self.fail_get_chat
        return SimpleNamespace(id=chat_id, effective_name="Wohnungssuche")


def test_check_connection(fresh_vault, monkeypatch):
    monkeypatch.setattr(bot, "Bot", FakeTelegramBot)
    with pytest.raises(bot.CheckFailed, match="Kein Bot-Token"):
        asyncio.run(bot.check_connection())
    fresh_vault.set("telegram_bot_token", TOKEN.split(":")[0] + ":" + "S" * 35)
    assert asyncio.run(bot.check_connection()) == "Bot @immo_test_bot erreichbar. Noch kein Chat verbunden."
    fresh_vault.set("telegram_chat_id", str(CHAT))
    assert asyncio.run(bot.check_connection()) == "Bot @immo_test_bot erreichbar, Chat „Wohnungssuche“ gefunden."

    monkeypatch.setattr(FakeTelegramBot, "fail_get_chat", BadRequest("Chat not found"))
    with pytest.raises(bot.CheckFailed, match="Chat nicht erreichbar"):
        asyncio.run(bot.check_connection())
    monkeypatch.setattr(FakeTelegramBot, "fail_get_chat", Forbidden("bot was kicked"))
    with pytest.raises(bot.CheckFailed, match="Chat nicht erreichbar"):
        asyncio.run(bot.check_connection())
    secret_token = fresh_vault.get("telegram_bot_token")
    monkeypatch.setattr(FakeTelegramBot, "fail_enter", InvalidToken(f"The token `{secret_token}` was rejected"))
    with pytest.raises(bot.CheckFailed) as exc_info:
        asyncio.run(bot.check_connection())
    assert "lehnt den Bot-Token ab" in str(exc_info.value) and secret_token not in str(exc_info.value)
    assert exc_info.value.__cause__ is None and exc_info.value.__suppress_context__
    monkeypatch.setattr(FakeTelegramBot, "fail_enter", NetworkError("connect failed"))
    with pytest.raises(bot.CheckFailed, match="nicht erreichbar"):
        asyncio.run(bot.check_connection())


# --- user allowlist --------------------------------------------------------------

def test_is_allowed_user():
    assert bot.is_allowed_user(text_update("hi", user_id=STRANGER), set())  # empty allowlist: everyone
    assert bot.is_allowed_user(text_update("hi", user_id=OWNER), {OWNER})
    assert not bot.is_allowed_user(text_update("hi", user_id=STRANGER), {OWNER})
    assert bot.is_allowed_user(callback_update(FakeQuery("gen:1", user_id=OWNER)), {OWNER, 5})
    assert not bot.is_allowed_user(callback_update(FakeQuery("gen:1", user_id=STRANGER)), {OWNER})
    assert not bot.is_allowed_user(SimpleNamespace(effective_user=None), {OWNER})
    assert not bot.is_allowed_user(text_update("hi", user_id=OWNER), None)  # undecryptable list: nobody


def test_allowed_user_ids_from_vault_or_env(fresh_vault):
    assert bot.allowed_user_ids() == set()
    vault.init(TEST_SECRETS["SESSION_SECRET"], {"telegram_allowed_user_ids": "5,6"})
    assert bot.allowed_user_ids() == {5, 6}
    vault.set("telegram_allowed_user_ids", f"{OWNER}, 7")
    assert vault.get("telegram_allowed_user_ids") == f"{OWNER},7" and bot.allowed_user_ids() == {OWNER, 7}


def test_undecryptable_allowlist_fails_closed(fresh_vault):
    fresh_vault.set("telegram_chat_id", str(CHAT))
    fresh_vault.set("telegram_allowed_user_ids", str(OWNER))
    vault.init("rotated-session-secret-" + "z" * 40, {"telegram_chat_id": str(CHAT)})
    assert vault.get("telegram_allowed_user_ids") is None  # reads as unset ...
    assert bot.allowed_user_ids() is None  # ... but the bot treats it as "nobody"
    press = FakeQuery("gen:1", user_id=OWNER)
    assert not run_guard(callback_update(press))
    assert press.answers == ["Nur für den Besitzer dieses Bots."]
    assert not run_guard(text_update("hi", user_id=OWNER))
    # A new pairing replaces the unreadable list.
    tg = FakeBot()
    assert send_pair(tg, vault.start_pairing("immo_test_bot"), chat_id=CHAT) == [bot.PAIR_OK]
    assert bot.allowed_user_ids() == {OWNER} and run_guard(text_update("hi", user_id=OWNER))


def test_guard_with_allowlist(fresh_vault):
    fresh_vault.set("telegram_chat_id", str(CHAT))
    fresh_vault.set("telegram_allowed_user_ids", str(OWNER))
    assert run_guard(text_update("hi", user_id=OWNER))
    assert run_guard(callback_update(FakeQuery("gen:1", user_id=OWNER)))

    stranger_press = FakeQuery("gen:1", user_id=STRANGER)
    assert not run_guard(callback_update(stranger_press))
    assert stranger_press.answers == ["Nur für den Besitzer dieses Bots."]
    for update in [text_update("/start", user_id=STRANGER), text_update("anders bitte", reply_to=101, user_id=STRANGER)]:
        assert not run_guard(update)  # messages, "Anpassen" replies and /start: ignored silently
    foreign_press = FakeQuery("gen:1", chat_id=43, user_id=OWNER)  # the owner, but in another chat
    assert not run_guard(callback_update(foreign_press))
    assert foreign_press.answers == []


def test_guard_without_allowlist_keeps_old_behaviour(fresh_vault):
    fresh_vault.set("telegram_chat_id", str(CHAT))
    assert run_guard(callback_update(FakeQuery("gen:1", user_id=STRANGER)))
    assert run_guard(text_update("hi", user_id=STRANGER))


def test_pair_adds_user_to_allowlist_once(fresh_vault):
    tg = FakeBot()
    vault.init(TEST_SECRETS["SESSION_SECRET"], {"telegram_allowed_user_ids": "5"})  # env list is kept
    assert send_pair(tg, vault.start_pairing("immo_test_bot"), chat_id=CHAT, user_id=OWNER) == [bot.PAIR_OK]
    assert vault.get("telegram_allowed_user_ids") == f"5,{OWNER}" and vault.source("telegram_allowed_user_ids") == "dashboard"
    assert send_pair(tg, vault.start_pairing("immo_test_bot"), chat_id=CHAT, user_id=OWNER) == [bot.PAIR_OK]
    assert vault.get("telegram_allowed_user_ids") == f"5,{OWNER}"  # no duplicate
    pair_events = [e["name"] for e in bot.db.list_credential_events() if e["action"] == "pair"]
    assert pair_events.count("telegram_allowed_user_ids") == 1 and pair_events.count("telegram_chat_id") == 2


# --- background reload & username --------------------------------------------------

def test_schedule_reload_runs_in_background_and_records_errors(fresh_vault, monkeypatch):
    calls = []

    async def slow_reload():
        calls.append("reload")
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    monkeypatch.setattr(bot, "reload", slow_reload)
    monkeypatch.setitem(bot._state, "last_error", None)

    async def main():
        bot.schedule_reload()
        assert calls == [] and len(bot._reload_tasks) == 1  # returned at once, task strongly referenced
        await asyncio.gather(*bot._reload_tasks, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(main())
    assert calls == ["reload"] and bot._reload_tasks == set()
    assert "RuntimeError: boom" in bot.status()["last_error"]


def test_reload_tasks_survive_stop(fresh_vault, monkeypatch):
    """stop() cancels announce tasks, but must not cancel the reload that called it."""
    monkeypatch.setattr(bot, "_token", "")
    order = []

    async def fake_try_start():
        order.append("started")
        return True

    monkeypatch.setattr(bot, "_try_start", fake_try_start)
    fresh_vault.set("telegram_bot_token", "111:" + "b" * 32)

    async def main():
        bot.schedule_reload()
        await asyncio.gather(*bot._reload_tasks)

    asyncio.run(main())
    assert order == ["started"]


def test_username(monkeypatch):
    monkeypatch.setattr(bot, "_app", None)
    assert bot.username() is None
    monkeypatch.setattr(bot, "_app", SimpleNamespace(bot=FakeBot(), running=True))
    assert bot.username() == "immo_test_bot"


# --- pictures album & dashboard links ----------------------------------------------

class AlbumBot(FakeBot):
    """FakeBot that also records photo/album uploads."""

    def __init__(self, fail=None):
        super().__init__()
        self.albums: list[dict] = []
        self.photos: list[dict] = []
        self.fail = fail

    async def send_media_group(self, **kwargs):
        if self.fail:
            raise self.fail
        self.albums.append(kwargs)
        return [SimpleNamespace(message_id=700 + i) for i in range(len(kwargs["media"]))]

    async def send_photo(self, **kwargs):
        if self.fail:
            raise self.fail
        self.photos.append(kwargs)
        return SimpleNamespace(message_id=800)


IS24_URL = "https://www.immobilienscout24.de/expose/123456789"


def store_pictures(db, listing_id, captions):
    """Pretend is24 already stored these pictures (rows + JPEG files)."""
    import io

    from PIL import Image

    folder = bot.is24.media_root() / str(listing_id)
    folder.mkdir(parents=True, exist_ok=True)
    rows = []
    for position, caption in enumerate(captions, start=1):
        out = io.BytesIO()
        Image.new("RGB", (40, 30), (position * 10, 100, 100)).save(out, format="JPEG")
        (folder / f"{position}.jpg").write_bytes(out.getvalue())
        rows.append({"position": position, "caption": caption, "source_url": f"https://pictures.immobilienscout24.de/{position}",
                     "file": f"{position}.jpg"})
    db.replace_media(listing_id, rows)
    db.set_media_state(listing_id, db.now_iso(), None)


@pytest.fixture
def album_listing(tg, monkeypatch):
    """An IS24 listing with an immobot message; ensure_media never goes to the network."""
    db = bot.db
    listing_id = add_listing(db, "is1", url=IS24_URL)
    calls = []

    async def ensure_media(lid, retry=False):
        calls.append((lid, retry))
        return db.list_media(lid)

    monkeypatch.setattr(bot.is24, "ensure_media", ensure_media)
    return listing_id, calls


def press_on(tg_bot, data):
    query = FakeQuery(data)
    asyncio.run(bot._on_callback(callback_update(query), SimpleNamespace(bot=tg_bot)))
    return query


def test_album_caption():
    rows = [{"caption": c} for c in ["Grundriss", "Balkon", "", "Balkon", "Bad"]]
    assert bot.album_caption(rows, 5, None) == "📷 5 Fotos · Grundriss, Balkon, Bad"
    assert bot.album_caption(rows[:2], 11, "https://immobot.example.de/listing/7") == (
        "📷 11 Fotos · Grundriss, Balkon (+9 weitere)\n🔗 https://immobot.example.de/listing/7")
    assert bot.album_caption([{"caption": ""}], 1, None) == "📷 1 Foto"
    long_rows = [{"caption": f"Zimmer {i} " + "ü" * 50} for i in range(30)]  # more than ever fit
    caption = bot.album_caption(long_rows, 40, "https://immobot.example.de/listing/7")
    assert bot._tg_len(caption) <= bot.CAPTION_MAX and ", …" in caption and caption.endswith("/listing/7")


def test_album_media_max_ten_caption_on_first():
    pictures = [({"caption": str(i)}, b"\xff\xd8jpeg" + bytes([i])) for i in range(14)]
    media = bot.album_media(pictures, "📷 14 Fotos")
    assert len(media) == 10
    assert media[0].caption == "📷 14 Fotos" and all(m.caption is None for m in media[1:])
    assert all(not isinstance(m.media, str) for m in media)  # uploaded bytes, never URLs


def test_pictures_button_sends_one_album_as_reply(tg, album_listing):
    listing_id, calls = album_listing
    store_pictures(bot.db, listing_id, ["Grundriss", "Balkon"] + [f"Raum {i}" for i in range(10)])
    album_bot = AlbumBot()
    query = press_on(album_bot, f"img:{listing_id}")
    assert query.answers == ["⏳ lade Bilder …"]
    assert calls == [(listing_id, True)]  # a user action may retry a failed fetch
    (album,) = album_bot.albums
    assert album["chat_id"] == CHAT and album["reply_parameters"].message_id == 555
    assert len(album["media"]) == 10
    assert album["media"][0].caption.startswith("📷 12 Fotos · Grundriss, Balkon, Raum 0") and "(+2 weitere)" in album["media"][0].caption
    assert bot.db.get_listing(listing_id)["album_message_id"] == 700
    again = press_on(album_bot, f"img:{listing_id}")  # pressed again: no second album
    assert again.answers == ["Bilder sind schon oben ↑"] and len(album_bot.albums) == 1


def test_pictures_button_single_picture_uses_send_photo(tg, album_listing):
    listing_id, _ = album_listing
    store_pictures(bot.db, listing_id, ["Außenansicht"])
    album_bot = AlbumBot()
    press_on(album_bot, f"img:{listing_id}")
    assert album_bot.albums == [] and len(album_bot.photos) == 1
    photo = album_bot.photos[0]
    assert photo["caption"] == "📷 1 Foto · Außenansicht" and photo["photo"][:2] == b"\xff\xd8"
    assert bot.db.get_listing(listing_id)["album_message_id"] == 800


def test_pictures_button_with_dashboard_link(tg, album_listing, monkeypatch):
    listing_id, _ = album_listing
    monkeypatch.setattr(bot, "_public_base", "https://immobot.example.de")
    store_pictures(bot.db, listing_id, ["Balkon", "Bad"])
    album_bot = AlbumBot()
    press_on(album_bot, f"img:{listing_id}")
    assert album_bot.albums[0]["media"][0].caption.endswith(f"\n🔗 https://immobot.example.de/listing/{listing_id}")


def test_pictures_button_without_pictures_shows_error_and_keeps_buttons(tg, album_listing):
    listing_id, _ = album_listing
    bot.db.set_media_state(listing_id, bot.db.now_iso(), "Exposé nicht abrufbar (HTTP 404).")
    album_bot = AlbumBot()
    press_on(album_bot, f"img:{listing_id}")
    assert album_bot.albums == [] and album_bot.photos == []
    edit = album_bot.edits[-1]
    assert edit["text"].endswith("⚠️ Bilder: Exposé nicht abrufbar (HTTP 404).")
    assert callback_data(edit["reply_markup"]) == [[f"gen:{listing_id}", f"skip:{listing_id}"], [f"img:{listing_id}"]]
    assert bot.db.get_listing(listing_id)["album_message_id"] is None


def test_pictures_button_telegram_error(tg, album_listing, monkeypatch):
    listing_id, _ = album_listing
    store_pictures(bot.db, listing_id, ["Balkon", "Bad"])
    monkeypatch.setitem(bot._state, "last_error", None)
    album_bot = AlbumBot(fail=NetworkError("upload failed"))
    press_on(album_bot, f"img:{listing_id}")
    assert "nicht gesendet" in album_bot.edits[-1]["text"] and "NetworkError" in bot.status()["last_error"]
    assert bot.db.get_listing(listing_id)["album_message_id"] is None  # can be pressed again
    assert bot._sending_albums == set()


def test_pictures_button_for_other_portals(tg, monkeypatch):
    listing_id = add_listing(bot.db)  # https://example.org/expose/1, provider immoscout -> not an IS24 URL
    album_bot = AlbumBot()
    query = press_on(album_bot, f"img:{listing_id}")
    assert query.answers == ["Bilder gibt es nur für ImmoScout24-Inserate."] and album_bot.albums == []


def test_pictures_button_works_while_a_draft_is_generated(tg, album_listing):
    listing_id, _ = album_listing
    store_pictures(bot.db, listing_id, ["Balkon", "Bad"])
    album_bot = AlbumBot()

    async def main():
        async with bot._lock(listing_id):  # a generation is running for this listing
            await bot._on_callback(callback_update(FakeQuery(f"img:{listing_id}")), SimpleNamespace(bot=album_bot))

    asyncio.run(main())
    assert len(album_bot.albums) == 1


def test_keyboards_have_pictures_and_dashboard_buttons(monkeypatch):
    is24_listing = listing_dict(id=12, url=IS24_URL)
    other = listing_dict(id=13, provider="immowelt", url="https://www.immowelt.de/expose/x")
    assert bot.followup_view(other)[1].inline_keyboard[-1][-1].callback_data == "skip:13"  # no extra row
    monkeypatch.setattr(bot, "_public_base", "https://immobot.example.de")
    for markup, extra in [(bot.followup_view(is24_listing)[1], ["img:12"]), (bot.draft_keyboard(is24_listing), ["img:12"]),
                          (bot.followup_view(other)[1], [])]:
        row = markup.inline_keyboard[-1]
        assert [b.callback_data for b in row if b.callback_data] == extra
        assert row[-1].text == "🔗 Dashboard" and row[-1].url.endswith("/listing/" + str(row[-1].url.split("/")[-1]))
        assert all(len(r) <= 3 for r in markup.inline_keyboard)
    assert bot.followup_view(is24_listing)[1].inline_keyboard[-1][-1].url == "https://immobot.example.de/listing/12"


@pytest.mark.parametrize("url,expected", [
    ("https://immobot.example.de", "https://immobot.example.de"),
    ("https://Immobot.Example.de/listing/3?x=1", "https://immobot.example.de"),
    ("http://immobot.example.de:8080", "http://immobot.example.de:8080"),
    ("http://localhost:8000", None), ("http://127.0.0.1:8000", None), ("http://[::1]:8000", None),
    ("http://testserver", None), ("https://app.localhost", None), ("ftp://immobot.example.de", None), ("", None), (None, None),
])
def test_valid_public_url(url, expected):
    assert bot.valid_public_url(url) == expected


def test_public_url_remembered_unless_fixed_by_env(fresh_db):
    bot.init_public_url("")
    assert bot.public_url_info() == (None, "unknown") and bot.dashboard_url(5) is None
    bot.remember_public_url("https://immobot.example.de")
    assert bot.dashboard_url(5) == "https://immobot.example.de/listing/5"
    assert fresh_db.get_meta("public_base_url") == "https://immobot.example.de"
    bot.remember_public_url("http://localhost:8000")  # unusable for Telegram: ignored
    bot.init_public_url("")  # after a restart: remembered
    assert bot.public_url_info() == ("https://immobot.example.de", "remembered")
    bot.init_public_url("https://fixed.example.de/")
    bot.remember_public_url("https://other.example.de")
    assert bot.public_url_info() == ("https://fixed.example.de", "env")
    bot.init_public_url("http://localhost")  # unusable env value: ignored, remembered one used
    assert bot.public_url_info() == ("https://immobot.example.de", "remembered")
