"""Jobs that run by themselves while immobot is up: listings are aussortiert (settings.DISCARD_REASONS) automatically.

- auto_dismiss: a listing "Gesehen" auto_dismiss_hours ago (setting, default 48, 0 = off) without a draft since is
  aussortiert "uninteressant" by the job (discard_auto). Unseen "Neu" listings never are, and neither is one the
  person brought back with ↩️ Zurückholen. The dashboard can filter those ("davon automatisch").
- stale: anything not aussortiert yet that arrived more than settings.STALE_DAYS ago becomes "veraltet" (final; its
  status stays visible as "war: …").
- apply_fredy_states: Fredy reports which ads are still online (called from the Fredy side); gone ones become
  "offline" (final), ones back online lose it again (db.mark_offline / db.mark_online).

Every change updates the listing's Telegram message (an edit, so no notification), but only for listings of the last
TELEGRAM_DAYS days: older messages scrolled away long ago.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import bot, db, settings

log = logging.getLogger(__name__)

EVERY_S = 1800  # how often the checks run
TELEGRAM_DAYS = 14  # only messages of listings this recent are updated in Telegram (older ones scrolled away)


def auto_dismiss_hours() -> int:
    return max(settings.as_int(db.get_settings().get("auto_dismiss_hours"), 48), 0)


def _refresh_recent(ids: list[int], now: datetime | None = None) -> None:
    """Update the Telegram messages of those listings received within TELEGRAM_DAYS, one after the other."""
    recent = ((now or datetime.now(timezone.utc)) - timedelta(days=TELEGRAM_DAYS)).isoformat(timespec="seconds")
    recent_ids = [i for i in ids if ((db.get_listing(i) or {}).get("received_at") or "") >= recent]
    if recent_ids:
        bot.schedule_refresh_many(recent_ids)


def run_auto_dismiss(now: datetime | None = None) -> list[int]:
    """One check of auto_dismiss. Returns the ids aussortiert "uninteressant"; their recent Telegram messages are
    updated one by one."""
    hours = auto_dismiss_hours()
    if not hours:
        return []
    now = now or datetime.now(timezone.utc)
    ids = db.auto_dismiss((now - timedelta(hours=hours)).isoformat(timespec="seconds"))
    if ids:
        log.info("%d listing(s) seen %d h ago without a draft: aussortiert (uninteressant)", len(ids), hours)
        _refresh_recent(ids, now)
    return ids


def run_stale(now: datetime | None = None) -> list[int]:
    """One check of stale: listings older than settings.STALE_DAYS -> "veraltet". Returns their ids."""
    now = now or datetime.now(timezone.utc)
    ids = db.discard_stale((now - timedelta(days=settings.STALE_DAYS)).isoformat(timespec="seconds"))
    if ids:
        log.info("%d listing(s) older than %d days: aussortiert (veraltet)", len(ids), settings.STALE_DAYS)
        _refresh_recent(ids, now)
    return ids


def apply_fredy_states(states: dict[str, bool]) -> None:
    """Fredy's view of which ads are still online: {fredy_id: is_active}. Inactive ones are aussortiert "offline"
    (replacing an "uninteressant", which comes back if the ad does), active ones that were offline lose it. Unknown
    fredy_ids are ignored. Telegram messages of recent changed listings are updated; call from the event loop
    (like bot.schedule_refresh_many)."""
    gone = [fredy_id for fredy_id, active in states.items() if not active]
    back = [fredy_id for fredy_id, active in states.items() if active]
    changed = db.mark_offline(gone) + db.mark_online(back)
    if changed:
        log.info("Fredy: %d listing(s) changed online state", len(changed))
        _refresh_recent(changed)


def run_once(now: datetime | None = None) -> None:
    run_stale(now)  # first: a listing too old for auto_dismiss to matter is "veraltet", not "uninteressant"
    run_auto_dismiss(now)


async def loop() -> None:
    while True:
        try:
            run_once()
        except Exception:  # keep checking; the next run may work
            log.exception("Automatic sorting out failed")
        await asyncio.sleep(EVERY_S)
