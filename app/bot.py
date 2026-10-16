"""Telegram side of immobot.

Fredy posts every new listing to the chat itself. A few seconds later immobot posts a small
follow-up message under it with buttons, and on demand turns that message into a Claude draft.
immobot is the only process polling updates for this bot token (long polling, no webhook).

Security: only updates from the configured chat are processed (vault "telegram_chat_id", read on
every update), everything else is dropped silently. Only users on the allowlist "telegram_allowed_user_ids"
may press buttons or send replies there (others get a short refusal for a button press, everything else is
ignored). The list is required: while it is empty, or can't be decrypted, nobody may (a button press then
says how to fill it). "/pair CODE" is handled completely inside the guard (_guard -> _pair) and never reaches
the other handlers, so pairing works with an empty list and fills it; strangers with a wrong code get no
answer at all. All listing and draft text is HTML-escaped. The bot token is never logged.

Listings with a gallery (pictures from Fredy, or ImmoScout24 with the own fetch switched on, see media.py) get
a "🖼 Bilder" button: it sends the stored pictures as one album
(uploaded bytes, max 10) under the listing's message. Every listing gets a "🔗 Dashboard" link when the
dashboard's public address is known (IMMOBOT_PUBLIC_URL, else remembered from dashboard requests).
Draft messages have two layouts (draft_keyboard):
- editor first, whenever the "📝 Bearbeiten" button can be used: "📝 Bearbeiten" (a login_url button to the draft
  editor, editor_access.py) and "📨 Inserat öffnen", "✅ Gesendet" / "❌ Uninteressant", pictures / dashboard.
  Shorter, more formal, new and own wishes happen in the editor (its quick buttons and critique field).
- full, the fallback while the public address is unknown or Telegram rejected the button: "🔁 Neu" / "✂️ Kürzer" /
  "🎩 Förmlicher", "✏️ Anpassen" (reply with a wish), "📨 Inserat öffnen", sent / skip, pictures / dashboard.
Telegram rejects a message with the login_url button while the dashboard domain isn't linked to the bot (@BotFather
/setdomain): it is then sent with the full layout and meta EDITOR_BUTTON_ERROR_META says why (shown on the status
and settings pages). The full layout keeps trying the button, so the first message Telegram accepts with it clears
the note and the next drafts get the editor layout again. Buttons of older messages ("new", "short", "formal",
"edit") keep working in either layout. "✍️ Anschreiben" shows the existing draft if there already is one.

Token and chat id come from vault.py. Without a token the bot stays off ("nicht konfiguriert"); without
a chat id it polls anyway so that "/pair" can connect a chat. reload() restarts it after a change.

Message layout lives in pure functions (header, followup_view, draft_view, ...) so it can be
unit-tested without Telegram.
"""

import asyncio
import html
import ipaddress
import logging
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from telegram import (
    Bot, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions, LoginUrl,
    ReplyParameters, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, NetworkError, RetryAfter, TelegramError, TimedOut
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

from . import db, editor_access, llm, media, portals, rent, settings, textfmt, vault

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
PAIR_OK = "✅ immobot ist jetzt mit diesem Chat verbunden."
PAIR_FAILED = "❌ Code ungültig oder abgelaufen."
PAIR_NEEDS_USER = ("⚠️ Bitte von einem normalen Telegram-Konto aus koppeln (nicht anonym, nicht als Kanal oder Bot). "
                   "Der Code bleibt gültig.")
NOT_OWNER = "Nur für den Besitzer dieses Bots."
NO_USERS_YET = "🔒 Noch keine erlaubten User – im Dashboard unter Zugänge eintragen oder mit /pair verbinden."
DRAFT_EXISTS = "Es gibt schon einen Entwurf – hier ist er."
LOADING_PICTURES = "⏳ lade Bilder …"
PICTURES_ALREADY_SENT = "Bilder sind schon oben ↑"
NO_PICTURES_HERE = "Für dieses Inserat gibt es keine Bildergalerie."
ALBUM_MAX = 10  # Telegram's limit per album
MANUAL_RETRY_S = 3.0  # pause before sending a hand-added listing's message again after a timeout
CAPTION_MAX = 1024  # Telegram's limit per caption
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
EDITOR_BUTTON_ERROR_META = "editor_login_url_error"  # "<ISO time> <German reason>" while Telegram rejects the button

# Button action -> drafts.kind
GENERATE_KINDS = {"gen": "initial", "new": "regenerate", "short": "shorter", "formal": "formal"}
ACTIONS = set(GENERATE_KINDS) | {"edit", "sent", "skip", "undo", "img"}
KIND_LABELS = {
    "initial": "Entwurf",
    "regenerate": "Neu geschrieben",
    "shorter": "Kürzer",
    "formal": "Förmlicher",
    "custom": "Angepasst",
    "test": "Test",
}
FINAL_LABEL = "✅ Endversion aus dem Editor"

# --- module state ------------------------------------------------------------
_app: Application | None = None  # set only while the bot is running
_token: str = ""  # the token the bot was started with; kept to build the Application and scrub error messages
_reload_lock = asyncio.Lock()
_reload_tasks: set[asyncio.Task] = set()  # background reloads (strong refs; not in _tasks, which stop() cancels)
_start_retry: asyncio.Task | None = None
_tasks: set[asyncio.Task] = set()  # running announce tasks (strong refs so they aren't GC'd)
_locks: dict[int, asyncio.Lock] = {}  # one per listing, so a double tap can't generate twice
_announcing: set[int] = set()  # listings whose follow-up is being sent right now
_sending_albums: set[int] = set()  # listings whose picture album is being prepared right now
_public_base: str | None = None  # dashboard base URL for "🔗 Dashboard" buttons, see init_public_url()
_public_base_from_env = False
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


def facts_of(listing: dict) -> list[str]:
    """['570 € kalt · 690 € warm', '50 m²', 'Address'] - the non-empty ones."""
    facts = [rent.price_text(listing)] + [str(listing.get(k) or "") for k in ("size", "address")]
    return [fact for fact in facts if fact.strip()]


def header(listing: dict) -> str:
    """'🏠 <b>Title</b>' / 'price · size · address' / provider label. Escaped HTML."""
    facts = [_shorten(fact, FACT_MAX) for fact in facts_of(listing)]
    lines = [f"🏠 <b>{_esc(_title(listing))}</b>"]
    if facts:
        lines.append(" · ".join(_esc(fact) for fact in facts))
    lines.append(_esc(portals.provider_label(listing.get("provider"))))
    return "\n".join(lines)


def _links_row(listing: dict) -> list[InlineKeyboardButton]:
    """"🖼 Bilder" (if the listing has a gallery) and "🔗 Dashboard" (if the public address is known)."""
    row = []
    if media.has_gallery(listing):
        row.append(_button("🖼 Bilder", "img", listing["id"]))
    url = dashboard_url(listing["id"])
    if url:
        row.append(InlineKeyboardButton("🔗 Dashboard", url=url))
    return row


def followup_view(listing: dict, note: str | None = None, editor: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """A listing without a draft: "✍️ Anschreiben" (draft right here) and, when the editor button can be used,
    "📝 Bearbeiten" (the editor, which offers "Entwurf erzeugen"), then "❌ Uninteressant". editor=False: without it."""
    lid = listing["id"]
    edit = editor_button(lid) if editor and editor_button_error() is None else None
    if edit:
        rows = [[_button("✍️ Anschreiben", "gen", lid), edit], [_button("❌ Uninteressant", "skip", lid)]]
    else:
        rows = [[_button("✍️ Anschreiben", "gen", lid), _button("❌ Uninteressant", "skip", lid)]]
    if _links_row(listing):
        rows.append(_links_row(listing))
    return header(listing) + _note(note), InlineKeyboardMarkup(rows)


def generating_text(listing: dict) -> str:
    return header(listing) + "\n\n⏳ schreibe Entwurf …"


def draft_keyboard(listing: dict, editor: bool = True) -> InlineKeyboardMarkup:
    """The buttons under a draft, in one of two layouts (module docstring):
    - editor first, when the "📝 Bearbeiten" button can be used (public address known, Telegram didn't reject it
      last time): "📝 Bearbeiten" + "📨 Inserat öffnen" · sent/skip · pictures/dashboard;
    - full otherwise: variants · "✏️ Anpassen" + "📨 Inserat öffnen" · sent/skip · pictures/dashboard. While Telegram
      rejects the button it is still tried here ("📝 Bearbeiten" + "✏️ Anpassen", then "📨 Inserat öffnen" in its own
      row), so the layout comes back once the domain is linked.
    editor=False: the full layout without the button (the retry after Telegram rejected it)."""
    lid = listing["id"]
    edit = editor_button(lid) if editor else None
    link = portals.contact_url(listing.get("provider"), listing.get("url"))
    open_listing = [InlineKeyboardButton("📨 Inserat öffnen ↗", url=link)] if link else []
    status_row = [_button("✅ Gesendet", "sent", lid), _button("❌ Uninteressant", "skip", lid)]
    if edit and editor_button_error() is None:
        rows = [[edit] + open_listing, status_row]
    else:
        rows = [[_button("🔁 Neu", "new", lid), _button("✂️ Kürzer", "short", lid), _button("🎩 Förmlicher", "formal", lid)]]
        if edit:
            rows.append([edit, _button("✏️ Anpassen", "edit", lid)])
            if open_listing:
                rows.append(open_listing)
        else:
            rows.append([_button("✏️ Anpassen", "edit", lid)] + open_listing)
        rows.append(status_row)
    if _links_row(listing):
        rows.append(_links_row(listing))
    return InlineKeyboardMarkup(rows)


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


def _hint(hint: str) -> str:
    """Claude's hint as Telegram HTML: its lines (without blank ones), at most HINT_MAX characters, Markdown converted.
    Cut before converting, so the cut can't leave a tag open: a "**" whose partner was cut off stays as it is."""
    lines = (" ".join(line.split()) for line in str(hint).splitlines())
    text = "\n".join(line for line in lines if line)
    if len(text) > HINT_MAX:
        text = text[: HINT_MAX - 1].rstrip() + "…"
    return textfmt.telegram_html(text)


def draft_view(listing: dict, draft: dict, note: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """Header, optional hint, the draft in <pre> (tap to copy) and a meta line. Never over 4096 chars."""
    top = header(listing)
    if draft.get("hint"):
        top += f"\n⚠️ {_hint(draft['hint'])}"
    provider, kind = draft.get("provider") or settings.DEFAULT_PROVIDER, draft.get("kind") or ""
    if kind == "final":  # the Endversion from the editor: your text, so no model or cost
        meta = [FINAL_LABEL]
    else:
        meta = [KIND_LABELS.get(kind, kind), settings.model_label(provider, draft.get("model")),
                settings.provider_label(provider), settings.draft_cost_text(draft)]
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
    """What the listing's message should show for its current status: the latest non-test draft, or the Endversion
    of a finished editor session when no newer draft came after it (so copying from Telegram copies your text)."""
    if listing.get("status") in ("sent", "dismissed"):
        return collapsed_view(listing, note)
    draft = db.latest_draft(listing["id"])
    if draft is None:
        return followup_view(listing, note)
    session = db.latest_session(listing["id"])
    if (session and session["status"] == "done" and (session.get("final_text") or "").strip()
            and (session.get("last_draft_id") or 0) >= draft["id"]):
        draft = {**draft, "text": session["final_text"], "kind": "final", "hint": None}
    return draft_view(listing, draft, note)


def album_caption(rows: list[dict], total: int, link: str | None) -> str:
    """'📷 11 Fotos · Grundriss, Balkon, … (+1 weitere)' plus a dashboard link line; ≤ 1024 characters."""
    head = f"📷 {total} Foto{'' if total == 1 else 's'}"
    more = f" (+{total - len(rows)} weitere)" if total > len(rows) else ""
    tail = more + (f"\n🔗 {link}" if link else "")
    names = ""
    for caption in dict.fromkeys(_shorten(r["caption"], 60) for r in rows if r.get("caption")):
        candidate = f"{names}, {caption}" if names else caption
        if _tg_len(f"{head} · {candidate}, …{tail}") > CAPTION_MAX:
            names += ", …"
            break
        names = candidate
    return f"{head} · {names}{tail}" if names else head + tail


def album_media(pictures: list[tuple[dict, bytes]], caption: str) -> list[InputMediaPhoto]:
    """Up to ALBUM_MAX photos from JPEG bytes (uploaded, never URLs); the caption goes on the first one."""
    return [InputMediaPhoto(media=jpeg, caption=caption if index == 0 else None)
            for index, (row, jpeg) in enumerate(pictures[:ALBUM_MAX])]


# --- dashboard links ---------------------------------------------------------

def valid_public_url(url) -> str | None:
    """'https://immobot.example.de' (scheme://host[:port]) if Telegram accepts it as a button link, else None.
    Telegram rejects links to localhost and bare IP addresses, which would make the whole message fail."""
    url = portals.safe_url(url)
    if url is None:
        return None
    parts = urlsplit(url)
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    if "." not in host or host.endswith(".localhost"):
        return None
    return f"{parts.scheme.lower()}://{parts.netloc.lower()}"


def init_public_url(env_url: str | None) -> None:
    """At startup: IMMOBOT_PUBLIC_URL if set and usable (fixed), else the address remembered earlier."""
    global _public_base, _public_base_from_env
    fixed = valid_public_url(env_url)
    if env_url and not fixed:
        log.warning("IMMOBOT_PUBLIC_URL is not a usable public http(s) address - ignored")
    _public_base_from_env = fixed is not None
    _public_base = fixed or valid_public_url(db.get_meta("public_base_url"))


def remember_public_url(url) -> None:
    """The dashboard calls this with scheme://host of authenticated requests (and the webhook's)."""
    global _public_base
    url = valid_public_url(url)
    if url and not _public_base_from_env and url != _public_base:
        _public_base = url
        db.set_meta("public_base_url", url)


def public_url_info() -> tuple[str | None, str]:
    """(base URL or None, where it comes from) for the settings page."""
    if _public_base is None:
        return None, "unknown"
    return _public_base, "env" if _public_base_from_env else "remembered"


def dashboard_url(listing_id: int) -> str | None:
    return f"{_public_base}/listing/{int(listing_id)}" if _public_base else None


def editor_url(listing_id: int) -> str | None:
    """Signed link to the listing's draft editor (token valid 7 days, see editor_access.py), or None while the
    public address is unknown. Only usable as a login_url: opening it needs Telegram's login data too."""
    return f"{_public_base}{editor_access.make_editor_path(listing_id)}" if _public_base else None


def editor_button(listing_id: int) -> InlineKeyboardButton | None:
    """"📝 Bearbeiten" as a login_url button to the draft editor: Telegram appends the user's signed login data when
    it is tapped (editor_access.start_editor_browser_session checks them). None while the public address is unknown.
    Needs the dashboard domain linked to the bot (@BotFather /setdomain), else Telegram rejects the message."""
    url = editor_url(listing_id)
    return InlineKeyboardButton("📝 Bearbeiten", login_url=LoginUrl(url=url)) if url else None


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


def is_pair_command(update, bot_username: str | None) -> bool:
    """A plain message starting with "/pair" or "/pair@<this bot>" (case-insensitive like Telegram commands)."""
    message = update.message
    if message is None or not isinstance(message.text, str) or not message.text.strip():
        return False
    command, _, mention = message.text.split(maxsplit=1)[0].partition("@")
    if mention and mention.lower() != (bot_username or "").lower():
        return False  # addressed to another bot in the group
    return command.lower() == "/pair"


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


def is_allowed_user(update, allowed: set[int] | None) -> bool:
    """The sender / presser must be on the allowlist. None (undecryptable) or empty (not set yet) = nobody."""
    if not allowed:
        return False
    user = update.effective_user
    return user is not None and user.id in allowed


def allowed_user_ids() -> set[int] | None:
    """The Telegram user allowlist (dashboard value, else env). Empty set = not set yet: nobody may use the bot's
    buttons until IDs are entered or a pairing adds one. None = a saved list can't be decrypted (SESSION_SECRET
    changed): fail closed the same way until the list is entered again or a new pairing is done."""
    return vault.allowed_user_ids()


def configured_chat_id() -> int | None:
    """The chat immobot works in (dashboard value, else env). Read fresh, so a change applies at once."""
    raw = vault.get("telegram_chat_id")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


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

def _has_login_url(keyboard) -> bool:
    return isinstance(keyboard, InlineKeyboardMarkup) and any(
        button.login_url is not None for row in keyboard.inline_keyboard for button in row)


def without_login_url(keyboard: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """The keyboard without login_url buttons ("📝 Bearbeiten"); rows left empty are dropped."""
    rows = [[button for button in row if button.login_url is None] for row in keyboard.inline_keyboard]
    return InlineKeyboardMarkup([row for row in rows if row])


def _editor_button_rejected(exc: BadRequest) -> bool:
    """Telegram's answer for a login_url whose domain isn't linked to the bot is "BOT_DOMAIN_INVALID"; other URL
    problems of the button read similar. Matched loosely."""
    text = str(exc).lower()
    return any(word in text for word in ("url", "domain", "login"))


def _note_editor_button_error(exc: BadRequest) -> None:
    if "domain" in str(exc).lower():
        reason = "Telegram lehnt den Editor-Button ab: Domain nicht mit dem Bot verknüpft (@BotFather → /setdomain)."
    else:
        reason = f"Telegram lehnt den Editor-Button ab ({_shorten(_describe(exc), 120)})."
    db.set_meta(EDITOR_BUTTON_ERROR_META, f"{db.now_iso()} {reason}")
    log.warning("Telegram rejected the editor button, sent without it: %s", _describe(exc))


def editor_button_error() -> tuple[str, str] | None:
    """(ISO time, German reason) of the last rejection of the "📝 Bearbeiten" button, or None while it works."""
    value = db.get_meta(EDITOR_BUTTON_ERROR_META)
    if not value:
        return None
    at, _, reason = value.partition(" ")
    return at, reason


async def _with_editor_fallback(call, keyboard, fallback=None):
    """call(keyboard). If Telegram rejects a keyboard with the login_url "📝 Bearbeiten" button, note why and call once
    more with fallback() (a keyboard without the button), or else the same keyboard minus the button. A message
    accepted with the button clears the note."""
    if not _has_login_url(keyboard):
        return await call(keyboard)
    try:
        result = await call(keyboard)
    except BadRequest as exc:
        if not _editor_button_rejected(exc):
            raise
        _note_editor_button_error(exc)
        return await call(fallback() if fallback is not None else without_login_url(keyboard))
    if db.get_meta(EDITOR_BUTTON_ERROR_META) is not None:
        db.delete_meta(EDITOR_BUTTON_ERROR_META)
    return result


async def _send(tg_bot, chat_id: int, text: str, keyboard) -> object:
    async def send(markup):
        kwargs = dict(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, reply_markup=markup,
                      link_preview_options=NO_PREVIEW)
        try:
            return await tg_bot.send_message(**kwargs)
        except RetryAfter as exc:  # flood control: wait as told, then try once more
            wait = exc.retry_after
            await asyncio.sleep(wait.total_seconds() if isinstance(wait, timedelta) else wait)
            return await tg_bot.send_message(**kwargs)

    return await _with_editor_fallback(send, keyboard)


async def _edit(tg_bot, listing: dict, text: str, keyboard) -> None:
    async def edit(markup):
        try:
            await tg_bot.edit_message_text(
                chat_id=configured_chat_id(),
                message_id=listing["tg_message_id"],
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=markup,
                link_preview_options=NO_PREVIEW,
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    # Rejected, the editor layout would lose every way to change the draft: the retry gets the full layout instead.
    await _with_editor_fallback(edit, keyboard, lambda: _keyboard_without_editor(listing))


def _keyboard_without_editor(listing: dict) -> InlineKeyboardMarkup:
    if db.latest_draft(listing["id"]) is None:
        return followup_view(listing, editor=False)[1]
    return draft_keyboard(listing, editor=False)


async def _show(tg_bot, listing: dict, note: str | None = None) -> None:
    await _edit(tg_bot, listing, *current_view(listing, note))


async def _answer(query, text: str | None = None) -> None:
    try:
        await query.answer(text)
    except TelegramError as exc:  # e.g. "query is too old" after a restart; the action still runs
        log.info("Could not answer callback query: %s", _describe(exc))


async def _delete_quietly(tg_bot, message_id: int) -> None:
    try:
        await tg_bot.delete_message(chat_id=configured_chat_id(), message_id=message_id)
    except TelegramError:
        pass


def _lock(listing_id: int) -> asyncio.Lock:
    return _locks.setdefault(listing_id, asyncio.Lock())


def has_draft(listing_id: int) -> bool:
    """A non-test draft or a finished editor session exists: "✍️ Anschreiben" shows it instead of writing one."""
    return db.latest_draft(listing_id) is not None or db.latest_session(listing_id, "done") is not None


# --- actions -----------------------------------------------------------------

async def _generate(tg_bot, listing: dict, kind: str, instruction: str | None = None) -> None:
    """Show '⏳ schreibe …', generate a draft, show it. On failure restore the previous view plus a warning."""
    listing_id = listing["id"]
    # Keep the buttons while generating: if the final edit never happens (restart, network), the message stays usable.
    await _edit(tg_bot, listing, generating_text(listing), current_view(listing)[1])
    try:
        draft = await llm.create_and_store_draft(listing_id, kind, instruction, source="telegram")
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
    try:
        await _edit(tg_bot, fresh, *draft_view(fresh, draft))
    except TelegramError:  # the draft is paid for and stored – try once more before giving up
        await asyncio.sleep(2)
        await _edit(tg_bot, fresh, *draft_view(fresh, draft))


async def _ask_instruction(tg_bot, listing: dict) -> None:
    prompt = await tg_bot.send_message(
        chat_id=configured_chat_id(),
        text=f"✏️ Was soll am Entwurf für „{_esc(_title(listing))}“ anders sein?",
        parse_mode=ParseMode.HTML,
        reply_markup=ForceReply(input_field_placeholder="z. B. kürzer, Homeoffice erwähnen"),
    )
    db.add_pending_prompt(prompt.message_id, listing["id"])


# --- handlers ----------------------------------------------------------------

async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs first for every update: note the activity, handle "/pair" right here (also with an empty allowlist:
    that is how it gets filled), and drop everything that isn't from our chat and from an allowed user."""
    _state["last_update_at"] = db.now_iso()
    in_our_chat = is_allowed_chat(update, configured_chat_id())
    allowed = allowed_user_ids()
    trusted = in_our_chat and is_allowed_user(update, allowed)
    if is_pair_command(update, context.bot.username):
        await _pair(update, context, trusted)
        raise ApplicationHandlerStop  # pairing traffic never reaches the other handlers
    if trusted:
        return
    if in_our_chat and update.callback_query is not None:
        # A button press has to be answered anyway; the answer is a short popup only the presser sees.
        await _answer(update.callback_query, NO_USERS_YET if allowed == set() else NOT_OWNER)
    raise ApplicationHandlerStop


async def _on_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_REPLY)


def pairing_detail(chat, user) -> str:
    """'Chat „Wohnung“ (group, -100123) · User Erik (@erik, 1001)' for the audit log and notices.
    Names come from Telegram (untrusted): plain text here, escaped in the dashboard, shortened."""
    chat_name = _shorten(chat.effective_name or "–", 60)
    user_name = _shorten(user.full_name or "–", 60)
    handle = f"@{user.username}, " if user.username else ""
    return f"Chat „{chat_name}“ ({chat.type}, {chat.id}) · User {user_name} ({handle}{user.id})"


async def _pair(update: Update, context: ContextTypes.DEFAULT_TYPE, trusted: bool) -> None:
    """"/pair CODE" with the code from the dashboard: from now on immobot works in this chat, and the sender
    is added to the allowlist. Called only from _guard."""
    message = update.message
    parts = message.text.split()
    code = parts[1] if len(parts) > 1 else ""
    if not vault.check_pairing_code(code):  # wrong codes count; the pairing is cancelled after too many
        if trusted:
            await message.reply_text(PAIR_FAILED)
        return  # strangers get no answer: don't reveal whether a pairing is pending
    user = message.from_user
    if message.sender_chat is not None or user is None or user.is_bot:  # anonymous admin, channel or bot
        await message.reply_text(PAIR_NEEDS_USER)
        return
    vault.cancel_pairing()  # used up (no await since the check: a second "/pair" can't use it too)
    old_chat, chat = configured_chat_id(), message.chat
    detail = pairing_detail(chat, user)
    vault.set("telegram_chat_id", str(chat.id))
    db.add_credential_event("telegram_chat_id", "pair", "Telegram", detail)
    allowed = allowed_user_ids() or set()  # an undecryptable list is replaced
    if user.id not in allowed:
        vault.set("telegram_allowed_user_ids", ",".join(str(i) for i in sorted(allowed | {user.id})))
        db.add_credential_event("telegram_allowed_user_ids", "pair", "Telegram", detail)
    log.info("Telegram chat connected via pairing (chat %s, user %s)", chat.id, user.id)
    await message.reply_text(PAIR_OK)
    notice = change_notice("telegram_chat_id", "pair", "Telegram", detail)
    await _notify_quietly(context.bot, chat.id, notice)  # the new chat sees exactly who paired what
    if old_chat is not None and old_chat != chat.id:  # and the previous chat learns it was replaced
        await _notify_quietly(context.bot, old_chat, notice)
    schedule_announce(db.unannounced_listing_ids())  # listings that arrived while no chat was connected


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
    if action == "img":  # not under the generation lock: pictures may be sent while a draft is being written
        await _send_pictures(context.bot, listing, query)
        return

    lock = _lock(listing_id)
    if lock.locked():
        await _answer(query, BUSY)
        return
    async with lock:
        await _answer(query)
        tg_bot = context.bot
        if action == "gen" and has_draft(listing_id):  # written in the dashboard or editor meanwhile: no second one
            collapsed = listing.get("status") in ("sent", "dismissed")  # a stale message: just show the state
            await _show(tg_bot, listing, note=None if collapsed else DRAFT_EXISTS)
        elif action in GENERATE_KINDS:
            await _generate(tg_bot, listing, GENERATE_KINDS[action])
        elif action == "edit":
            await _ask_instruction(tg_bot, listing)
        elif action in ("sent", "skip"):
            db.set_listing_status(listing_id, "sent" if action == "sent" else "dismissed")
            await _show(tg_bot, db.get_listing(listing_id))
        elif action == "undo":
            db.set_listing_status(listing_id, "drafted" if db.latest_draft(listing_id) else "new")
            await _show(tg_bot, db.get_listing(listing_id))


async def _send_pictures(tg_bot, listing: dict, query) -> None:
    """"🖼 Bilder": the stored pictures (fetched on first use) as one album, as a reply to the listing's message."""
    listing_id = listing["id"]
    if not media.has_gallery(listing):
        await _answer(query, NO_PICTURES_HERE)
        return
    if listing.get("album_message_id"):
        await _answer(query, PICTURES_ALREADY_SENT)
        return
    if listing_id in _sending_albums:
        await _answer(query, BUSY)
        return
    _sending_albums.add(listing_id)
    try:
        await _answer(query, LOADING_PICTURES)
        pictures = media.read_pictures(listing_id, await media.ensure_media(listing_id, retry=True))
        fresh = db.get_listing(listing_id) or listing
        if not pictures:
            await _show(tg_bot, fresh, note="Bilder: " + (fresh.get("media_error") or "keine gefunden."))
            return
        caption = album_caption([row for row, _ in pictures[:ALBUM_MAX]], len(pictures), dashboard_url(listing_id))
        reply = ReplyParameters(message_id=listing["tg_message_id"], allow_sending_without_reply=True)
        chat = configured_chat_id()
        if len(pictures) == 1:
            message = await tg_bot.send_photo(chat_id=chat, photo=pictures[0][1], caption=caption, reply_parameters=reply)
        else:
            messages = await tg_bot.send_media_group(chat_id=chat, media=album_media(pictures, caption),
                                                     reply_parameters=reply)
            message = messages[0]
        db.set_album_sent(listing_id, message.message_id)
    except TelegramError as exc:
        _record_error(exc)
        await _show(tg_bot, db.get_listing(listing_id) or listing, note="Bilder konnten nicht gesendet werden – bitte nochmal.")
    finally:
        _sending_albums.discard(listing_id)


async def _on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A text reply to one of our '✏️ Was soll anders sein?' prompts -> custom draft."""
    message = update.message
    # The guard already filters; checked again because prompts are looked up by bare message id.
    if not is_allowed_chat(update, configured_chat_id()) or not is_allowed_user(update, allowed_user_ids()):
        return
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
        if listing is None or listing.get("tg_message_id") or listing_id in _announcing:
            continue  # unknown, already announced, or another task is on it
        _announcing.add(listing_id)
        try:
            if index < media.NEW_DETAILS_MAX:  # warm rent; usually fetched meanwhile (web.fredy_webhook)
                await media.ensure_details(listing_id)
                listing = db.get_listing(listing_id) or listing
            message = await _send(tg_bot, chat_id, *followup_view(listing))
            db.set_listing_tg_message(listing_id, message.message_id)
        except TelegramError as exc:
            _record_error(exc)
        finally:
            _announcing.discard(listing_id)


def manual_album_caption(listing: dict, rows: list[dict], total: int) -> str:
    """Plain-text caption of a hand-added listing's album: title, facts and portal, then the picture list."""
    facts = " · ".join(_shorten(fact, FACT_MAX) for fact in facts_of(listing))
    head = "\n".join(filter(None, [f"🏠 {_shorten(_title(listing), 200)}", facts, "ImmoScout24 · per Link hinzugefügt"]))
    pictures = album_caption(rows, total, None)
    caption = f"{head}\n{pictures}"
    if _tg_len(caption) > CAPTION_MAX:
        caption = f"{head}\n📷 {total} Foto{'' if total == 1 else 's'}"
    return caption if _tg_len(caption) <= CAPTION_MAX else caption[: CAPTION_MAX // 2]


async def post_manual(tg_bot, chat_id: int, listing_id: int, write_draft: bool) -> None:
    """A listing added by hand: there is no Fredy message, so the pictures go first (one album, up to ALBUM_MAX, like
    Fredy's preview), then the usual message with the buttons; with write_draft Claude writes the first draft right
    away (shown in that message)."""
    listing = db.get_listing(listing_id)
    if listing is None or listing.get("tg_message_id") or listing_id in _announcing:
        return
    _announcing.add(listing_id)
    try:
        # a retry (bot restart: unannounced listings) doesn't send the album twice
        pictures = [] if listing.get("album_message_id") else media.read_pictures(listing_id, media.stored_pictures(listing_id))
        if pictures:
            caption = manual_album_caption(listing, [row for row, _ in pictures[:ALBUM_MAX]], len(pictures))
            try:
                if len(pictures) == 1:
                    album = await tg_bot.send_photo(chat_id=chat_id, photo=pictures[0][1], caption=caption)
                else:
                    album = (await tg_bot.send_media_group(chat_id=chat_id, media=album_media(pictures, caption)))[0]
                db.set_album_sent(listing_id, album.message_id)
            except TelegramError as exc:  # the message with the buttons still matters more
                _record_error(exc)
        view = current_view(db.get_listing(listing_id) or listing)
        try:
            message = await _send(tg_bot, chat_id, *view)
        except (TimedOut, NetworkError):  # seen right after a big album upload: once more after a pause
            await asyncio.sleep(MANUAL_RETRY_S)
            message = await _send(tg_bot, chat_id, *view)
        db.set_listing_tg_message(listing_id, message.message_id)
    except TelegramError as exc:
        _record_error(exc)
        return
    finally:
        _announcing.discard(listing_id)
    if write_draft and not has_draft(listing_id):
        async with _lock(listing_id):
            fresh = db.get_listing(listing_id)
            if fresh and not has_draft(listing_id):
                await _generate(tg_bot, fresh, "initial")


def schedule_post_manual(listing_id: int, write_draft: bool) -> bool:
    """Non-blocking post_manual in the background. False if there is no running bot or no chat."""
    chat = configured_chat_id()
    if _app is None or chat is None:
        log.warning("Telegram bot or chat missing – hand-added listing %s not posted", listing_id)
        return False
    task = asyncio.create_task(post_manual(_app.bot, chat, listing_id, write_draft))
    _tasks.add(task)
    task.add_done_callback(_task_done)
    return True


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
    chat = configured_chat_id()
    if chat is None:
        log.warning("No Telegram chat connected – no follow-up for %d listing(s)", len(listing_ids))
        return
    task = asyncio.create_task(announce(_app.bot, chat, list(listing_ids)))
    _tasks.add(task)
    task.add_done_callback(_task_done)


async def _refresh(tg_bot, listing_id: int) -> None:
    async with _lock(listing_id):  # after a generation that is running right now
        listing = db.get_listing(listing_id)
        if listing is not None and listing.get("tg_message_id") and configured_chat_id() is not None:
            await _show(tg_bot, listing)


def schedule_refresh(listing_id: int) -> None:
    """Non-blocking: let the listing's Telegram message show its current state (e.g. "✅ Gesendet" after the editor
    marked it sent). No-op without a running bot; errors end up on the status page. Call from the event loop."""
    if _app is None:
        return
    task = asyncio.create_task(_refresh(_app.bot, listing_id))
    _tasks.add(task)
    task.add_done_callback(_task_done)


# --- credential notices & connection test -------------------------------------

def change_notice(name: str, action: str, via: str, detail: str | None = None) -> str:
    """'🔐 Zugang geändert: Anthropic-API-Key – gespeichert (Dashboard, 11.10. 14:03)', plus an optional
    detail line (e.g. who paired which chat). Never contains a secret value."""
    when = datetime.now(settings.LOCAL_TZ).strftime("%d.%m. %H:%M")
    label = vault.LABELS.get(name, name)
    text = f"🔐 Zugang geändert: {label} – {vault.ACTION_LABELS.get(action, action)} ({via}, {when})"
    return text + ("\n" + detail if detail else "")


async def _notify_quietly(tg_bot, chat: int, text: str) -> None:
    try:
        await tg_bot.send_message(chat_id=chat, text=text, link_preview_options=NO_PREVIEW)
    except Exception as exc:  # a notice must never break the change it reports
        _record_error(exc)


async def notify(text: str, chat: int | None) -> None:
    """Send a plain-text notice with the running bot. No-op without a running bot or chat; never raises."""
    if _app is not None and chat is not None:
        await _notify_quietly(_app.bot, chat, text)


class CheckFailed(Exception):
    """str(exc) is a short German message, safe to show."""


async def check_connection() -> str:
    """getMe with the current token, then getChat for the configured chat. Returns a German OK message,
    raises CheckFailed. Uses its own short-lived Bot, so it also works while polling is down."""
    token = vault.get("telegram_bot_token")
    if not token:
        raise CheckFailed("Kein Bot-Token hinterlegt.")
    chat = configured_chat_id()
    try:
        async with Bot(token) as tg_bot:  # entering calls getMe
            name = "@" + str(tg_bot.username)
            if chat is None:
                return f"Bot {name} erreichbar. Noch kein Chat verbunden."
            info = await tg_bot.get_chat(chat)
    # "from None": PTB's InvalidToken message contains the token.
    except InvalidToken:
        raise CheckFailed("Telegram lehnt den Bot-Token ab.") from None
    except (BadRequest, Forbidden):
        raise CheckFailed("Chat nicht erreichbar – stimmt die Chat-ID und ist der Bot Mitglied im Chat?") from None
    except TelegramError as exc:
        raise CheckFailed(f"Telegram nicht erreichbar ({type(exc).__name__}).") from None
    return f"Bot {name} erreichbar, Chat „{info.effective_name or chat}“ gefunden."


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
    missed = db.unannounced_listing_ids()
    if missed:  # listings that arrived while the bot was down or restarting
        log.info("Catching up on %d listing(s) without follow-up", len(missed))
        schedule_announce(missed)
    return True


async def _retry_start() -> None:
    while True:
        await asyncio.sleep(START_RETRY_S)
        if await _try_start():
            return


async def start() -> None:
    """Start long polling with the token from the vault; without a token do nothing ("nicht konfiguriert").
    Never raises: on failure the error is recorded and start is retried in the background."""
    global _token, _start_retry
    _token = vault.get("telegram_bot_token") or ""
    if not _token:
        log.info("No Telegram bot token configured – bot not started")
        return
    if not await _try_start():
        _start_retry = asyncio.create_task(_retry_start())


async def reload() -> None:
    """Restart with the current vault values (after the bot token changed in the dashboard)."""
    async with _reload_lock:
        await stop()
        await start()


def schedule_reload() -> None:
    """reload() in the background, so a dashboard request doesn't wait for it: stopping waits for running
    handlers such as a Claude call. Errors end up on the status page. Call from the event loop."""
    task = asyncio.create_task(reload())
    _reload_tasks.add(task)
    task.add_done_callback(_reload_done)


def _reload_done(task: asyncio.Task) -> None:
    _reload_tasks.discard(task)
    if not task.cancelled() and task.exception() is not None:
        _record_error(task.exception())


def username() -> str | None:
    """The bot's @username without "@" (from getMe at start), or None while the bot isn't running."""
    return _app.bot.username if _app is not None else None


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
