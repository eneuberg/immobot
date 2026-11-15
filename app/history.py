"""The editor's "Verlauf": every version of a listing's letter, like a commit log - who made it (Claude's first
draft, Claude after a critique, the person by hand), when, the word diff to the version before and Claude's notes.

Nothing extra is stored for it: the versions follow from what the editor keeps anyway. A session (round) starts
with Claude's draft (original_text, its drafts row has the hint); every revision is a feedback row whose
before_text is the person's text at that moment (her hand edits since the version before) and whose draft_id is
Claude's result (text, hint), with her decision ("rejected" puts before_text back). "Fertig" stores final_text
(her last hand edits) and, if a critique was still in its field, a "note". Questions ("Nur fragen") are not
versions and aren't listed. timeline() is oldest first; the page shows it newest first.
"""

import re

from . import db, textdiff

_WORD = re.compile(r"\w+")

# editor.personal_revision's requests, recognised by their start: shown as the level / line that changed
_PERSONAL_STARTS = (
    ("Schreib den ersten Absatz ehrlicher", "Mühe → Ehrlich"),
    ("Gib dir im ersten Absatz mehr Mühe", "Mühe → Volle Mühe"),
    ("Schreib den ersten Absatz in normalem Ton", "Mühe → Normal"),
    ("Greif im ersten Absatz auf, was ich selbst", "deine Zeile aufgegriffen"),
)
DECISIONS = {"accepted": "übernommen", "rejected": "verworfen", "kept": "behalten"}


def personal_label(critique: str) -> str | None:
    """"Mühe → Volle Mühe" for a request editor.personal_revision wrote (only the level / line changed)."""
    for start, label in _PERSONAL_STARTS:
        if (critique or "").startswith(start):
            return label
    return None


def _words(segments, op: str) -> int:
    return sum(len(_WORD.findall(text)) for kind, text in segments if kind == op)


def _entry(who: str, label: str, text: str, prev: str | None, when: str | None, **extra) -> dict:
    """One version: who ("claude" | "you"), what, its text, the trimmed diff from `prev` and the word counts."""
    entry = {"who": who, "label": label, "text": text, "when": when, "quote": None, "hint": None, "decision": None,
             "diff": None, "added": 0, "removed": 0, **extra}
    if prev is not None:
        segments = textdiff.word_diff(prev, text)
        entry.update(diff=textdiff.trim_equal(segments), added=_words(segments, "insert"),
                     removed=_words(segments, "delete"))
    return entry


def _differs(a: str | None, b: str | None) -> bool:
    return (a or "").strip() != (b or "").strip()


def timeline(listing_id: int, kind_labels: dict[str, str] | None = None) -> tuple[list[dict], str | None]:
    """(versions oldest first, the text the person's current edits are compared with - the newest version of the
    open session, None without one). Versions: dicts from _entry; notes (critiques only noted at "Fertig") are
    entries with who "note"."""
    entries: list[dict] = []
    base: str | None = None
    previous_round: str | None = None
    for session in db.list_sessions(listing_id):
        draft = db.get_draft(session["base_draft_id"]) if session["base_draft_id"] else None
        kind = (draft or {}).get("kind") or "initial"
        if not entries:
            label = "erster Entwurf"
        else:
            label = "neuer Entwurf" + (f" ({kind_labels[kind]})" if kind_labels and kind in kind_labels else "")
        entries.append(_entry("claude", label, session["original_text"], previous_round,
                              (draft or {}).get("created_at") or session["created_at"], hint=(draft or {}).get("hint")))
        prev = session["original_text"]
        rows = sorted(db.list_feedback(session_id=session["id"]), key=lambda row: row["id"])
        notes = [{"who": "note", "label": "beim Abschließen notiert, nicht an Claude geschickt", "quote": row["text"],
                  "when": row["created_at"]} for row in rows if row["kind"] == "note"]
        for row in rows:
            if row["kind"] not in ("critique", "quick"):
                continue
            before = row.get("before_text")
            if before is not None and _differs(before, prev):
                entries.append(_entry("you", "von Hand geändert", before, prev, row["created_at"]))
                prev = before
            result = db.get_draft(row["draft_id"]) if row.get("draft_id") else None
            if result is None:  # "Ganz neu schreiben" (the next round shows it) or rows from before draft_id
                continue
            if row["kind"] == "quick":
                what, quote = "überarbeitet: " + (kind_labels or {}).get(row["text"], row["text"]), None
            elif personal_label(row["text"]):
                what, quote = "überarbeitet: " + personal_label(row["text"]), None
            else:
                what, quote = "überarbeitet nach", row["text"]
            entries.append(_entry("claude", what, result["text"], prev, result["created_at"], quote=quote,
                                  hint=result.get("hint"), decision=DECISIONS.get(row.get("decision") or "")))
            prev = before if row.get("decision") == "rejected" and before is not None else result["text"]
        if session["status"] == "done":
            if _differs(session["final_text"], prev):
                entries.append(_entry("you", "von Hand geändert", session["final_text"], prev,
                                      session["finished_at"]))
            prev = session["final_text"] or prev
            base = None
        elif session["status"] == "open":
            base = prev  # the live entry compares the current text with this
        else:  # superseded: its last saved text, if it differs
            if _differs(session["current_text"], prev):
                entries.append(_entry("you", "von Hand geändert", session["current_text"], prev, session["updated_at"]))
            prev = session["current_text"]
        entries.extend(notes)  # noted at "Fertig": after the last hand edits
        previous_round = prev
    return entries, base
