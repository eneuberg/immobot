"""Telegram side of immobot.

Fredy posts every new listing to the chat itself. A few seconds later immobot posts a small
follow-up message under it with buttons, and on demand turns that message into a Claude draft.
immobot is the only process polling updates for this bot token (long polling, no webhook).

Security: only updates from config.telegram_chat_id are processed, everything else is dropped
silently. All listing and draft text is HTML-escaped. The bot token is never logged.

Message layout lives in pure functions (header, followup_view, draft_view, ...) so it can be
unit-tested without Telegram.
"""

import asyncio
import html
import logging
import re
from datetime import timedelta

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from . import db, llm, portals, settings

log = logging.getLogger(__name__)

MAX_MESSAGE_LEN = 4096  # Telegram's limit per message
TITLE_MAX = 80
FACT_MAX = 80
HINT_MAX = 300
NOTE_MAX = 300
INSTRUCTION_MAX = 500
FREDY_SEND_GAP_S = 1.2  # Fredy's Telegram adapter sends ~1 message/s; we want to appear after it
SEND_GAP_S = 1.1  # our own pace when posting several follow-ups
START_RETRY_S = 60

TRUNCATED_NOTE = "… (gekürzt – vollständig im Dashboard)"
GENERIC_ERROR = "Fehler beim Generieren – bitte nochmal versuchen."
NOT_FOUND = "Inserat nicht gefunden."
BUSY = "⏳ läuft schon …"
START_REPLY = "immobot läuft ✅ – unter Fredys Inseraten erscheinen meine Buttons."
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# Button action -> drafts.kind
GENERATE_KINDS = {"gen": "initial", "new": "regenerate", "short": "shorter", "formal": "formal"}
ACTIONS = set(GENERATE_KINDS) | {"edit", "sent", "skip", "undo"}
KIND_LABELS = {
    "initial": "Entwurf",
    "regenerate": "Neu geschrieben",
    "shorter": "Kürzer",
    "formal": "Förmlicher",
    "custom": "Angepasst",
    "test": "Test",
}

# --- module state ------------------------------------------------------------
_app: Application | None = None  # set only while the bot is running
_chat_id: int | None = None
_token: str = ""  # kept only to scrub it from error messages
_start_retry: asyncio.Task | None = None
_tasks: set[asyncio.Task] = set()  # running announce tasks (strong refs so they aren't GC'd)
_locks: dict[int, asyncio.Lock] = {}  # one per listing, so a double tap can't generate twice
_state: dict[str, str | None] = {"last_update_at": None, "last_error": None}


# --- message building (pure) -------------------------------------------------

def _esc(value) -> str:
    return html.escape(str(value), quote=False)


def _shorten(value, limit: int) -> str:
    """One line, at most `limit` characters."""
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _tg_len(text: str) -> int:
    """Length in UTF-16 code units, as Telegram counts. Measuring the raw HTML overestimates, which is safe."""
    return len(text.encode("utf-16-le")) // 2


def _title(listing: dict) -> str:
    return _shorten(listing.get("title") or f"Inserat #{listing.get('id')}", TITLE_MAX)


def _note(note: str | None) -> str:
    return f"\n\n⚠️ {_esc(_shorten(note, NOTE_MAX))}" if note else ""


def _button(label: str, action: str, listing_id: int) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=f"{action}:{listing_id}")


def header(listing: dict) -> str:
    """'🏠 <b>Title</b>' / 'price · size · address' / provider label. Escaped HTML."""
    facts = [_shorten(listing[k], FACT_MAX) for k in ("price", "size", "address") if str(listing.get(k) or "").strip()]
    lines = [f"🏠 <b>{_esc(_title(listing))}</b>"]
    if facts:
        lines.append(" · ".join(_esc(fact) for fact in facts))
    lines.append(_esc(portals.provider_label(listing.get("provider"))))
    return "\n".join(lines)


def followup_view(listing: dict, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    keyboard = InlineKeyboardMarkup([[
        _button("✍️ Anschreiben", "gen", listing["id"]),
        _button("❌ Uninteressant", "skip", listing["id"]),
    ]])
    return header(listing) + _note(note), keyboard


def generating_text(listing: dict) -> str:
    return header(listing) + "\n\n⏳ schreibe Entwurf …"


def draft_keyboard(listing: dict) -> InlineKeyboardMarkup:
    lid = listing["id"]
    row2 = [_button("✏️ Anpassen", "edit", lid)]
    link = portals.contact_url(listing.get("provider"), listing.get("url"))
    if link:
        row2.append(InlineKeyboardButton("📨 Inserat öffnen ↗", url=link))
    return InlineKeyboardMarkup([
        [_button("🔁 Neu", "new", lid), _button("✂️ Kürzer", "short", lid), _button("🎩 Förmlicher", "formal", lid)],
        row2,
        [_button("✅ Gesendet", "sent", lid), _button("❌ Uninteressant", "skip", lid)],
    ])


def _fit_escaped(text: str, budget: int) -> str:
    """The longest escaped prefix of `text` whose Telegram length fits into `budget`."""
    pieces, used = [], 0
    for char in text:
        piece = _esc(char)
        used += _tg_len(piece)
        if used > budget:
            break
        pieces.append(piece)
    return "".join(pieces).rstrip()


def draft_view(listing: dict, draft: dict, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Header, optional hint, the draft in <pre> (tap to copy) and a meta line. Never over 4096 chars."""
    top = header(listing)
    if draft.get("hint"):
        top += f"\n⚠️ {_esc(_shorten(draft['hint'], HINT_MAX))}"
    model = draft.get("model") or ""
    kind = draft.get("kind") or ""
    meta = [KIND_LABELS.get(kind, kind), settings.MODELS.get(model, {}).get("label", model), settings.fmt_usd(draft.get("cost_usd"))]
    bottom = f"\n<i>{_esc(' · '.join(part for part in meta if part))}</i>" + _note(note)

    body = draft.get("text") or "–"
    text = f"{top}\n\n<pre>{_esc(body)}</pre>{bottom}"
    if _tg_len(text) > MAX_MESSAGE_LEN:
        marker = f"\n{TRUNCATED_NOTE}"
        budget = MAX_MESSAGE_LEN - _tg_len(f"{top}\n\n<pre></pre>{marker}{bottom}")
        text = f"{top}\n\n<pre>{_fit_escaped(body, budget)}</pre>{marker}{bottom}"
    return text, draft_keyboard(listing)


def collapsed_view(listing: dict, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    label = "✅ Gesendet" if listing.get("status") == "sent" else "❌ Uninteressant"
    keyboard = InlineKeyboardMarkup([[_button("↩️ Rückgängig", "undo", listing["id"])]])
    return f"{label} · {_esc(_title(listing))}" + _note(note), keyboard


def current_view(listing: dict, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """What the listing's message should show for its current status (reads the latest non-test draft)."""
    if listing.get("status") in ("sent", "dismissed"):
        return collapsed_view(listing, note)
    draft = db.latest_draft(listing["id"])
    return draft_view(listing, draft, note) if draft else followup_view(listing, note)


# --- input checks (pure) -----------------------------------------------------

_CALLBACK_RE = re.compile(r"([a-z]{1,10}):([1-9][0-9]{0,11})")


def parse_callback(data) -> tuple[str, int] | None:
    """'gen:12' -> ('gen', 12). Anything malformed or unknown -> None."""
    if not isinstance(data, str):
        return None
    match = _CALLBACK_RE.fullmatch(data)
    if match is None or match.group(1) not in ACTIONS:
        return None
    return match.group(1), int(match.group(2))


def is_allowed_chat(update, chat_id: int | None) -> bool:
    """True only for messages / button presses in our own chat."""
    if chat_id is None:
        return False
    if update.callback_query is not None:
        message = update.callback_query.message
        return message is not None and message.chat.id == chat_id
    if update.message is not None:
        return update.message.chat.id == chat_id
    return False


# --- errors ------------------------------------------------------------------

def _describe(exc: BaseException) -> str:
    """'Type: message', token scrubbed, one line."""
    text = f"{type(exc).__name__}: {exc}"
    if _token:
        text = text.replace(_token, "***")
    return _shorten(text, 300)


def _record_error(exc: BaseException) -> None:
    text = _describe(exc)
    _state["last_error"] = f"{db.now_iso()} {text}"
    log.warning("Telegram: %s", text)


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    _record_error(context.error)


# --- Telegram calls ----------------------------------------------------------

async def _send(tg_bot, chat_id: int, text: str, keyboard) -> object:
    kwargs = dict(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=keyboard, link_preview_options=NO_PREVIEW)
    try:
        return await tg_bot.send_message(**kwargs)
    except RetryAfter as exc:  # flood control: wait as told, then try once more
        wait = exc.retry_after
        await asyncio.sleep(wait.total_seconds() if isinstance(wait, timedelta) else wait)
        return await tg_bot.send_message(**kwargs)


async def _edit(tg_bot, listing: dict, text: str, keyboard) -> None:
    try:
        await tg_bot.edit_message_text(
            chat_id=_chat_id,
            message_id=listing["tg_message_id"],
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            link_preview_options=NO_PREVIEW,
        )
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _show(tg_bot, listing: dict, note: str | None = None) -> None:
    await _edit(tg_bot, listing, *current_view(listing, note))


async def _answer(query, text: str | None = None) -> None:
    try:
        await query.answer(text)
    except TelegramError as exc:  # e.g. "query is too old" after a restart; the action still runs
        log.info("Could not answer callback query: %s", _describe(exc))


async def _delete_quietly(tg_bot, message_id: int) -> None:
    try:
        await tg_bot.delete_message(chat_id=_chat_id, message_id=message_id)
    except TelegramError:
        pass


def _lock(listing_id: int) -> asyncio.Lock:
    return _locks.setdefault(listing_id, asyncio.Lock())


# --- actions -----------------------------------------------------------------

async def _generate(tg_bot, listing: dict, kind: str, instruction: str | None = None) -> None:
    """Show '⏳ schreibe …', generate a draft, show it. On failure restore the previous view plus a warning."""
    listing_id = listing["id"]
    await _edit(tg_bot, listing, generating_text(listing), None)
    try:
        draft = await llm.create_and_store_draft(listing_id, kind, instruction)
    except llm.LLMError as exc:  # includes BudgetExceeded; message is safe to show
        await _show(tg_bot, listing, note=str(exc) or GENERIC_ERROR)
        return
    except Exception:
        log.exception("Draft generation failed for listing %s", listing_id)
        await _show(tg_bot, listing, note=GENERIC_ERROR)
        return
    # Same rule as llm.create_and_store_draft: only "new" becomes "drafted" (a status changed meanwhile stays).
    fresh = db.get_listing(listing_id) or listing
    if fresh.get("status") == "new":
        db.set_listing_status(listing_id, "drafted")
    await _edit(tg_bot, listing, *draft_view(fresh, draft))


async def _ask_instruction(tg_bot, listing: dict) -> None:
    prompt = await tg_bot.send_message(
        chat_id=_chat_id,
        text=f"✏️ Was soll am Entwurf für „{_esc(_title(listing))}“ anders sein?",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="z. B. kürzer, Homeoffice erwähnen"),
    )
    db.add_pending_prompt(prompt.message_id, listing["id"])


# --- handlers ----------------------------------------------------------------

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs first for every update: note the activity, drop everything that isn't from our chat."""
    _state["last_update_at"] = db.now_iso()
    if not is_allowed_chat(update, _chat_id):
        raise ApplicationHandlerStop


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_REPLY)


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parsed = parse_callback(query.data)
    if parsed is None:
        await _answer(query)
        return
    action, listing_id = parsed
    listing = db.get_listing(listing_id)
    if listing is None or not listing.get("tg_message_id"):
        await _answer(query, NOT_FOUND)
        return

    lock = _lock(listing_id)
    if lock.locked():
        await _answer(query, BUSY)
        return
    async with lock:
        await _answer(query)
        tg_bot = context.bot
        if action in GENERATE_KINDS:
            await _generate(tg_bot, listing, GENERATE_KINDS[action])
        elif action == "edit":
            await _ask_instruction(tg_bot, listing)
        elif action in ("sent", "skip"):
            db.set_listing_status(listing_id, "sent" if action == "sent" else "dismissed")
            await _show(tg_bot, db.get_listing(listing_id))
        elif action == "undo":
            db.set_listing_status(listing_id, "drafted" if db.latest_draft(listing_id) else "new")
            await _show(tg_bot, db.get_listing(listing_id))


async def _on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A text reply to one of our '✏️ Was soll anders sein?' prompts -> custom draft."""
    message = update.message
    prompt_id = message.reply_to_message.message_id
    listing_id = db.pop_pending_prompt(prompt_id)
    if listing_id is None:
        return  # reply to some other message
    instruction = message.text.strip()[:INSTRUCTION_MAX]
    try:
        async with _lock(listing_id):  # wait if a generation for this listing is running
            listing = db.get_listing(listing_id)
            if listing and listing.get("tg_message_id") and instruction:
                await _generate(context.bot, listing, "custom", instruction)
    finally:
        await _delete_quietly(context.bot, prompt_id)
        await _delete_quietly(context.bot, message.message_id)


# --- follow-up messages ------------------------------------------------------

async def announce(tg_bot, chat_id: int, listing_ids: list[int]) -> None:
    """Post the follow-up message for each listing once Fredy's own messages are out."""
    delay = settings.as_float(db.get_settings().get("followup_delay_s"), 4.0)
    await asyncio.sleep(max(delay, 0.0) + FREDY_SEND_GAP_S * len(listing_ids))
    if settings.as_bool(db.get_settings().get("paused")):
        log.info("Paused – no follow-up for %d listing(s)", len(listing_ids))
        return
    for index, listing_id in enumerate(listing_ids):
        if index:
            await asyncio.sleep(SEND_GAP_S)
        listing = db.get_listing(listing_id)
        if listing is None or listing.get("tg_message_id"):
            continue  # unknown or already announced
        try:
            message = await _send(tg_bot, chat_id, *followup_view(listing))
        except TelegramError as exc:
            _record_error(exc)
            continue
        db.set_listing_tg_message(listing_id, message.message_id)


def _task_done(task: asyncio.Task) -> None:
    _tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _record_error(task.exception())


def schedule_announce(listing_ids: list[int]) -> None:
    """Non-blocking: start the delayed follow-up in the background. Call from the event loop (async handler)."""
    if not listing_ids:
        return
    if _app is None:
        log.warning("Telegram bot not running – no follow-up for %d listing(s)", len(listing_ids))
        return
    task = asyncio.create_task(announce(_app.bot, _chat_id, list(listing_ids)))
    _tasks.add(task)
    task.add_done_callback(_task_done)


# --- lifecycle ---------------------------------------------------------------

def _build_application() -> Application:
    # concurrent_updates: a running generation must not block other taps (and the double-tap lock relies on it).
    app = Application.builder().token(_token).concurrent_updates(True).build()
    app.add_handler(TypeHandler(Update, _guard), group=-1)
    app.add_handler(CommandHandler("start", _on_start))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY & ~filters.COMMAND, _on_reply))
    app.add_error_handler(_on_error)
    return app


async def _shutdown(app: Application) -> None:
    try:
        if app.updater is not None and app.updater.running:
            await app.updater.stop()
        if app.running:
            await app.stop()
        await app.shutdown()
        await app.bot.shutdown()  # no-op normally; closes HTTP clients if initialize() failed halfway
    except Exception as exc:
        log.warning("Error while stopping the Telegram bot: %s", _describe(exc))


async def _try_start() -> bool:
    global _app
    app = _build_application()
    try:
        await app.initialize()
        await app.start()
        await app.updater.start_polling(
            allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY],
            error_callback=_record_error,  # polling errors: log type/message only, show on status page
        )
    except Exception as exc:
        _record_error(exc)
        log.warning("Telegram bot could not start; retrying in %d s", START_RETRY_S)
        await _shutdown(app)
        return False
    _app = app
    log.info("Telegram bot started (long polling)")
    return True


async def _retry_start() -> None:
    while True:
        await asyncio.sleep(START_RETRY_S)
        if await _try_start():
            return


async def start(config) -> None:
    """Start long polling. Never raises: on failure the error is recorded and start is retried in the background."""
    global _chat_id, _token, _start_retry
    _chat_id = config.telegram_chat_id
    _token = config.telegram_bot_token
    if not await _try_start():
        _start_retry = asyncio.create_task(_retry_start())


async def stop() -> None:
    global _app, _start_retry
    if _start_retry is not None:
        _start_retry.cancel()
        _start_retry = None
    for task in list(_tasks):
        task.cancel()
    app, _app = _app, None
    if app is not None:
        await _shutdown(app)


def status() -> dict:
    return {
        "running": _app is not None and _app.running,
        "last_update_at": _state["last_update_at"],
        "last_error": _state["last_error"],
    }
