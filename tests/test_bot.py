"""Tests for app/bot.py. No network: Telegram is replaced by small fakes, the LLM by a stub."""

import asyncio
from types import SimpleNamespace

import pytest
from telegram import ForceReply
from telegram.error import BadRequest, InvalidToken
from telegram.ext import ApplicationHandlerStop

from app import bot, llm, portals
from tests.conftest import TEST_SECRETS

CHAT = 42
TOKEN = TEST_SECRETS["TELEGRAM_BOT_TOKEN"]


# --- fakes -------------------------------------------------------------------

class FakeBot:
    def __init__(self):
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
    def __init__(self, data, chat_id=CHAT):
        self.data = data
        self.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
        self.answers: list[str | None] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)


def callback_update(query):
    return SimpleNamespace(callback_query=query, message=None)


def text_update(text, chat_id=CHAT, message_id=900, reply_to=None):
    reply = SimpleNamespace(message_id=reply_to) if reply_to is not None else None
    message = SimpleNamespace(text=text, message_id=message_id, chat=SimpleNamespace(id=chat_id), reply_to_message=reply)
    return SimpleNamespace(callback_query=None, message=message)


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
def tg(fresh_db, monkeypatch):
    """Fake Telegram bot + our chat id; returns the fake bot."""
    monkeypatch.setattr(bot, "_chat_id", CHAT)
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
    assert text.endswith("<i>Entwurf · Claude Opus 5.5 · 4,2 ct</i>")
    assert "gekürzt" not in text


def test_draft_view_unknown_model_shows_raw_name():
    draft = {"text": "x", "hint": None, "model": "claude-foo", "cost_usd": 0, "kind": "custom"}
    text, _ = bot.draft_view(listing_dict(), draft)
    assert "⚠️" not in text
    assert "<i>Angepasst · claude-foo · 0,0 ct</i>" in text


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
    assert callback_data(markup) == [["gen:12", "skip:12"]]
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
    assert len(rows) == 3
    assert [b.callback_data for b in rows[1]] == ["edit:12"]


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


def test_guard_drops_foreign_updates(fresh_db, monkeypatch):
    monkeypatch.setattr(bot, "_chat_id", CHAT)
    monkeypatch.setitem(bot._state, "last_update_at", None)
    with pytest.raises(ApplicationHandlerStop):
        asyncio.run(bot._guard(text_update("hi", chat_id=43), None))
    assert bot.status()["last_update_at"] is not None
    asyncio.run(bot._guard(text_update("hi", chat_id=CHAT), None))  # passes through


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
    assert -1 in app.handlers and len(app.handlers[0]) == 3
    assert app.error_handlers


def test_start_failure_is_recorded_without_token_and_stop_is_tolerant(fresh_db, monkeypatch, caplog):
    from telegram.ext import Application

    async def failing_initialize(self):
        raise InvalidToken(f"The token `{TOKEN}` was rejected by the server.")

    monkeypatch.setattr(Application, "initialize", failing_initialize)
    monkeypatch.setitem(bot._state, "last_error", None)
    monkeypatch.setattr(bot, "_token", "")  # start() sets these; restore them afterwards
    monkeypatch.setattr(bot, "_chat_id", None)
    config = SimpleNamespace(telegram_bot_token=TOKEN, telegram_chat_id=CHAT)

    async def main():
        await bot.start(config)  # must not raise
        state = bot.status()
        await bot.stop()  # cancels the retry, must not raise
        return state

    state = asyncio.run(main())
    assert state["running"] is False
    assert state["last_error"] and "InvalidToken" in state["last_error"]
    assert TOKEN not in state["last_error"]
    assert TOKEN not in caplog.text
    assert bot.status()["running"] is False
