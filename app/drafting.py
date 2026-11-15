"""The first draft of a listing, written in the background while the editor shows a progress bar.

"✍️ Anschreiben" (listing page, Telegram's editor button, the editor itself) opens the editor at once; the draft is
started by a POST (the listing page's form, or editor.js posting /editor/{id}/draft as soon as the page shows that
there is no draft yet), never by a plain GET. One job per listing: a second tap while it runs gets the same job.
Jobs live in memory (a restart forgets them; the editor then simply offers to start again).

The bar expects the mean duration of the last initial drafts (drafts.duration_ms, measured around the generation in
llm.create_and_store_draft), DEFAULT_EXPECTED_MS while there are fewer than MIN_SAMPLES of them.
"""

import asyncio
import logging
import time

from . import bot, db, llm

log = logging.getLogger(__name__)

GENERIC_ERROR = "Unerwarteter Fehler – Details im Log."
DEFAULT_EXPECTED_MS = 40_000
MIN_SAMPLES = 3
SAMPLES = 20
EXPECTED_RANGE_MS = (5_000, 180_000)  # one odd value (a hang, a stub) can't make the bar absurd
SLOW_AFTER_MS = 15_000  # "Dauert länger als gewöhnlich" this long after the expected time
ERROR_KEEP_S = 600  # an error is shown (and nothing restarts by itself) for this long, then the editor starts anew

# listing id -> {"state": "running" | "done" | "error", "error": text or None, "started": monotonic, "ended": …}
_jobs: dict[int, dict] = {}
_tasks: set[asyncio.Task] = set()  # strong refs, so running jobs aren't garbage-collected


def expected_ms() -> int:
    durations = db.recent_draft_durations("initial", SAMPLES)
    if len(durations) < MIN_SAMPLES:
        return DEFAULT_EXPECTED_MS
    low, high = EXPECTED_RANGE_MS
    return min(high, max(low, round(sum(durations) / len(durations))))


def job(listing_id: int) -> dict | None:
    """The listing's job, without a "done" one (the draft is there) and an error older than ERROR_KEEP_S."""
    current = _jobs.get(listing_id)
    if current is None:
        return None
    if current["state"] == "error" and time.monotonic() - current["ended"] > ERROR_KEEP_S:
        _jobs.pop(listing_id, None)
        return None
    return current


def status(listing_id: int) -> dict:
    """{"state": "running" | "done" | "error" | "idle", "error", "elapsed_ms", "expected_ms", "slow_after_ms"} for
    the editor page and its polling. "done": a draft exists."""
    current = job(listing_id)
    if current is not None and current["state"] != "done":
        state, error = current["state"], current["error"]
        elapsed = (current.get("ended") or time.monotonic()) - current["started"]
    else:
        state, error, elapsed = ("done" if db.latest_draft(listing_id) else "idle"), None, 0
    return {"state": state, "error": error, "elapsed_ms": round(elapsed * 1000), "expected_ms": expected_ms(),
            "slow_after_ms": SLOW_AFTER_MS}


def start(listing_id: int) -> None:
    """Start the first draft unless there is one or a job for it is running (a double tap reuses that job)."""
    current = job(listing_id)
    if current is not None and current["state"] == "running":
        return
    if db.latest_draft(listing_id) is not None:
        _jobs.pop(listing_id, None)
        return
    _jobs[listing_id] = {"state": "running", "error": None, "started": time.monotonic(), "ended": None}
    task = asyncio.create_task(_write(listing_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def forget(listing_id: int) -> None:
    """The editor shows the draft now: a finished job isn't needed any more."""
    if (_jobs.get(listing_id) or {}).get("state") == "done":
        _jobs.pop(listing_id, None)


async def _write(listing_id: int) -> None:
    state, error = "done", None
    try:
        async with bot.listing_lock(listing_id):  # not while Telegram writes one ("✍️ Anschreiben" in the fallback)
            if db.latest_draft(listing_id) is None:
                await llm.create_and_store_draft(listing_id, "initial", source="editor", record_feedback=False)
        bot.schedule_refresh(listing_id)  # the Telegram message shows the draft too
    except llm.LLMError as exc:  # budget, limits, API problems: short German texts
        state, error = "error", str(exc) or GENERIC_ERROR
    except Exception:
        log.exception("Background draft failed for listing %s", listing_id)
        state, error = "error", GENERIC_ERROR
    _jobs[listing_id] = {**_jobs.get(listing_id, {"started": time.monotonic()}), "state": state, "error": error,
                         "ended": time.monotonic()}
