"""The first draft of a listing, written in the background while the editor shows a progress bar.

"✍️ Anschreiben" (listing page, Telegram's editor button, the editor itself) opens the editor at once; the draft is
started by a POST (the listing page's form, or editor.js posting /editor/{id}/draft as soon as the page shows that
there is no draft yet), never by a plain GET. One job per listing: a second tap while it runs gets the same job.
Jobs live in memory (a restart forgets them; the editor then simply offers to start again).

The bar expects the mean duration of the last initial drafts (drafts.duration_ms, measured around the generation in
llm.create_and_store_draft), DEFAULT_EXPECTED_MS while there are fewer than MIN_SAMPLES of them.

The editor's Claude actions on an existing letter run the same way (start_editor_job, editor_status): "Überarbeiten
lassen" and the quick buttons (learning.revise_in_editor / quick_in_editor; the bar expects the editor's earlier
revisions, drafts.duration_ms of source "editor") and "Nur fragen" (llm.answer_question; expected from the last
answers since this start). One editor job per listing; it runs under bot.listing_lock like the first draft. A
finished or failed job is shown once (editor_status / take_editor_job), then forgotten.
"""

import asyncio
import logging
import secrets
import time
from collections import deque

from . import bot, db, learning, llm

log = logging.getLogger(__name__)

GENERIC_ERROR = "Unerwarteter Fehler – Details im Log."
DEFAULT_EXPECTED_MS = 40_000
MIN_SAMPLES = 3
SAMPLES = 20
EXPECTED_RANGE_MS = (5_000, 180_000)  # one odd value (a hang, a stub) can't make the bar absurd
SLOW_AFTER_MS = 15_000  # "Dauert länger als gewöhnlich" this long after the expected time
ERROR_KEEP_S = 600  # an error is shown (and nothing restarts by itself) for this long, then the editor starts anew

# This server process. Jobs live in memory, so a restart (a deploy) loses them: every status answer says which
# process gave it, and editor.js starts a lost job again by itself instead of showing an error.
BOOT = secrets.token_hex(6)

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
            "slow_after_ms": SLOW_AFTER_MS, "boot": BOOT}


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


# --- the editor's actions on an existing letter ---------------------------------------------------------------

REVISION_KINDS = ("custom",) + tuple(learning.QUICK_LABELS)  # "Überarbeiten lassen", "Kürzer", "Förmlicher", "Neu"
ASK = "ask"
DEFAULT_REVISION_MS = 35_000
DEFAULT_ASK_MS = 15_000
DONE_MESSAGES = {"custom": "revised", "shorter": "shorter", "formal": "formal", "regenerate": "regenerate"}

# listing id -> {"state", "error", "started", "ended", "kind": REVISION_KINDS or ASK, "critique": the critique or
# question (put back into the field after an error), "answer": Claude's answer to a question}
_editor_jobs: dict[int, dict] = {}
_ask_durations: deque[int] = deque(maxlen=SAMPLES)  # ms of the answers since this start (questions are no drafts)


def _bounded_mean(durations: list[int], default: int) -> int:
    if len(durations) < MIN_SAMPLES:
        return default
    low, high = EXPECTED_RANGE_MS
    return min(high, max(low, round(sum(durations) / len(durations))))


def editor_expected_ms(kind: str) -> int:
    if kind == ASK:
        return _bounded_mean(list(_ask_durations), DEFAULT_ASK_MS)
    return _bounded_mean(db.recent_draft_durations(REVISION_KINDS, SAMPLES, source="editor"), DEFAULT_REVISION_MS)


def editor_job(listing_id: int) -> dict | None:
    return _editor_jobs.get(listing_id)


def editor_running(listing_id: int) -> bool:
    return (_editor_jobs.get(listing_id) or {}).get("state") == "running"


def editor_status(listing_id: int, take: bool = True) -> dict:
    """{"state": "running" | "done" | "error" | "idle", "kind", "error", "answer", "elapsed_ms", "expected_ms",
    "slow_after_ms", "message", "boot"} for the editor's polling. take: a finished or failed job is forgotten once reported
    (the page shows the result now)."""
    current = _editor_jobs.get(listing_id)
    if current is None:
        return {"state": "idle", "kind": None, "error": None, "answer": None, "elapsed_ms": 0,
                "expected_ms": editor_expected_ms("custom"), "slow_after_ms": SLOW_AFTER_MS, "message": None,
                "boot": BOOT}
    if take and current["state"] != "running":
        _editor_jobs.pop(listing_id, None)
    elapsed = (current.get("ended") or time.monotonic()) - current["started"]
    return {"state": current["state"], "kind": current["kind"], "error": current["error"],
            "answer": current.get("answer"), "elapsed_ms": round(elapsed * 1000),
            "expected_ms": editor_expected_ms(current["kind"]), "slow_after_ms": SLOW_AFTER_MS,
            "message": DONE_MESSAGES.get(current["kind"]), "critique": current.get("critique"), "boot": BOOT}


def take_editor_job(listing_id: int) -> dict | None:
    """For the editor page: the running job (stays), or a finished / failed one (shown now, so forgotten)."""
    return editor_status(listing_id) if listing_id in _editor_jobs else None


def start_editor_job(listing_id: int, session_id: int, kind: str, text: str, critique: str = "") -> bool:
    """Start a revision (kind in REVISION_KINDS; text = the letter with the hand edits, critique for "custom") or a
    question (kind ASK; critique = the question) in the background. False when one is running already (a double
    tap, another tab): that one goes on and is what the page shows."""
    if editor_running(listing_id):
        return False
    _editor_jobs[listing_id] = {"state": "running", "error": None, "started": time.monotonic(), "ended": None,
                                "kind": kind, "critique": critique, "answer": None}
    task = asyncio.create_task(_run_editor_job(listing_id, session_id, kind, text, critique))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _run_editor_job(listing_id: int, session_id: int, kind: str, text: str, critique: str) -> None:
    state, error, answer = "done", None, None
    started = time.monotonic()
    try:
        async with bot.listing_lock(listing_id):  # not while Telegram writes a draft for this listing
            if kind == ASK:
                answer = await ask(listing_id, critique, text)
                _ask_durations.appendleft(round((time.monotonic() - started) * 1000))
            elif kind == "custom":
                await learning.revise_in_editor(session_id, text, critique)
            else:
                await learning.quick_in_editor(session_id, text, kind)
    except llm.LLMError as exc:  # budget, limits, session state, API problems: short German texts
        state, error = "error", str(exc) or GENERIC_ERROR
    except Exception:
        log.exception("Editor job %s failed for listing %s", kind, listing_id)
        state, error = "error", GENERIC_ERROR
    _editor_jobs[listing_id] = {**_editor_jobs.get(listing_id, {"started": started, "kind": kind}), "state": state,
                                "error": error, "answer": answer, "ended": time.monotonic()}


async def ask(listing_id: int, question: str, letter: str) -> str:
    """"Nur fragen": Claude's short answer from the ad, the profile and the letter. Counts toward the editor's daily
    limit, but is no feedback, no draft and no revision: the letter stays as it is and learning never sees it."""
    question = (question or "").strip()
    if not question:
        raise learning.LearningError("Bitte schreiben, was du wissen willst.")
    listing = db.get_listing(listing_id)
    if listing is None:
        raise learning.LearningError("Inserat nicht gefunden.")
    learning.check_editor_limit(listing_id)
    return await llm.answer_question(listing, db.get_settings(), question, letter)
