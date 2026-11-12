"""Jobs that run by themselves while immobot is up: listings nobody acted on go to "uninteressant".

auto_dismiss: a listing still "neu" (no draft, status never touched) auto_dismiss_hours after it arrived (setting,
default 48, 0 = off) is set to uninteressant and marked as dismissed automatically; the dashboard can filter those
and its Telegram message says so (with "↩️ Rückgängig", after which it is never dismissed automatically again).
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import bot, db, settings

log = logging.getLogger(__name__)

EVERY_S = 1800  # how often the check runs
TELEGRAM_DAYS = 14  # only messages of listings this recent are updated in Telegram (older ones scrolled away)


def auto_dismiss_hours() -> int:
    return max(settings.as_int(db.get_settings().get("auto_dismiss_hours"), 48), 0)


def run_auto_dismiss(now: datetime | None = None) -> list[int]:
    """One check. Returns the ids set to uninteressant; their recent Telegram messages are updated one by one."""
    hours = auto_dismiss_hours()
    if not hours:
        return []
    now = now or datetime.now(timezone.utc)
    ids = db.auto_dismiss((now - timedelta(hours=hours)).isoformat(timespec="seconds"))
    if ids:
        log.info("%d listing(s) without a draft for %d h set to uninteressant", len(ids), hours)
        recent = (now - timedelta(days=TELEGRAM_DAYS)).isoformat(timespec="seconds")
        bot.schedule_refresh_many([i for i in ids if ((db.get_listing(i) or {}).get("received_at") or "") >= recent])
    return ids


async def loop() -> None:
    while True:
        try:
            run_auto_dismiss()
        except Exception:  # keep checking; the next run may work
            log.exception("Automatic dismissal failed")
        await asyncio.sleep(EVERY_S)
