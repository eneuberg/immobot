"""Text diffs for the profile history, rewrite proposals and the draft editor.

word_diff() compares word by word (difflib) and returns segments [(op, text)] with op "equal", "insert" or
"delete". Joining the "equal" and "delete" texts gives the old text, "equal" and "insert" the new one, so all
whitespace and line breaks survive; macros.html renders the segments with text_diff() (<ins>/<del> in a
white-space: pre-wrap block). sentence_diff() is the compact form for prompts: only the changed sentences, an
edited sentence as "vorher / nachher" with its word changes.
"""

import difflib
import re

# Words, single punctuation marks and whitespace runs: "Guten Tag, Frau X." -> Guten|␠|Tag|,|␠|Frau|␠|X|.
_TOKEN = re.compile(r"\w+|[^\w\s]|\s+")
# Sentence ends: after . ! ? (plus closing quotes/brackets) before whitespace, and at every line break.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|(?<=[.!?…][\"'“”»«)\]])\s+|\s*\n+\s*")


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text or "")


def word_diff(old: str | None, new: str | None) -> list[tuple[str, str]]:
    """Segments [(op, text)] turning `old` into `new`. Neighbouring changes separated only by spaces are joined
    into one deletion and one insertion ("a b" -> "x y" reads as -"a b" +"x y", not -a +x -b +y); a line break
    between them is kept as an equal segment so the line structure stays visible."""
    a, b = _tokens(old), _tokens(new)
    raw: list[tuple[str, str]] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            raw.append(("equal", "".join(a[i1:i2])))
        else:
            if i2 > i1:
                raw.append(("delete", "".join(a[i1:i2])))
            if j2 > j1:
                raw.append(("insert", "".join(b[j1:j2])))

    segments: list[tuple[str, str]] = []
    deleted = inserted = ""

    def flush() -> None:
        nonlocal deleted, inserted
        if deleted:
            segments.append(("delete", deleted))
        if inserted:
            segments.append(("insert", inserted))
        deleted = inserted = ""

    for index, (op, text) in enumerate(raw):
        if op == "delete":
            deleted += text
        elif op == "insert":
            inserted += text
        elif ((deleted or inserted) and index + 1 < len(raw) and raw[index + 1][0] != "equal"
              and text.isspace() and "\n" not in text):
            deleted += text  # spaces between two changes: part of both sides of the joined change
            inserted += text
        else:
            flush()
            segments.append(("equal", text))
    flush()
    return _merge(segments)


def _merge(segments: list[tuple[str, str]]) -> list[tuple[str, str]]:
    merged: list[tuple[str, str]] = []
    for op, text in segments:
        if not text:
            continue
        if merged and merged[-1][0] == op:
            merged[-1] = (op, merged[-1][1] + text)
        else:
            merged.append((op, text))
    return merged


def has_changes(segments: list[tuple[str, str]]) -> bool:
    return any(op != "equal" for op, _ in segments)


def trim_equal(segments: list[tuple[str, str]], keep: int = 160) -> list[tuple[str, str]]:
    """Shorten long unchanged stretches to show changes in their context: before the first change only the last
    `keep` characters stay, after the last change the first `keep`, between changes both ends. Cuts are marked
    with "…". Without any change the segments are returned as they are."""
    if not has_changes(segments):
        return list(segments)
    first = next(i for i, (op, _) in enumerate(segments) if op != "equal")
    last = max(i for i, (op, _) in enumerate(segments) if op != "equal")
    trimmed = []
    for index, (op, text) in enumerate(segments):
        if op == "equal" and len(text) > keep:
            if index < first:
                text = "…" + text[-keep:]
            elif index > last:
                text = text[:keep] + "…"
            elif len(text) > 2 * keep:
                text = text[:keep] + " … " + text[-keep:]
        trimmed.append((op, text))
    return trimmed


def sentences(text: str | None) -> list[str]:
    """The text split into sentences and lines, stripped, without empty ones."""
    return [part.strip() for part in _SENTENCE_END.split(text or "") if part and part.strip()]


PAIR_MIN_RATIO = 0.5  # a changed sentence this similar to a new one counts as its edited version ("vorher → nachher")
WORD_CHANGES_MAX = 4  # more word changes in a pair: the sentence was rewritten, "- / +" says enough
STRUCTURE_ONLY = "(Sätze unverändert, nur Leerzeichen, Absätze oder Zeilenumbrüche geändert)"


def _pairs(old: list[str], new: list[str]) -> list[tuple[str | None, str | None]]:
    """Changed sentences in order; an old one is paired with the most similar new one (an edit of it), the others
    stand alone as removed (new None) or added (old None)."""
    pairs: list[tuple[str | None, str | None]] = []
    start = 0
    for sentence in old:
        best, best_index = 0.0, None
        for index in range(start, len(new)):
            ratio = difflib.SequenceMatcher(None, sentence, new[index], autojunk=False).ratio()
            if ratio > best:
                best, best_index = ratio, index
        if best_index is None or best < PAIR_MIN_RATIO:
            pairs.append((sentence, None))
            continue
        pairs += [(None, added) for added in new[start:best_index]]
        pairs.append((sentence, new[best_index]))
        start = best_index + 1
    pairs += [(None, added) for added in new[start:]]
    return pairs


def word_changes(old: str, new: str) -> str:
    """The word-level changes between two versions of a sentence, e.g. '„sehr“ gestrichen · „Mit freundlichen“ →
    „Viele“ · „gern“ ergänzt'; "" when only whitespace changed, nothing is left of the old sentence or there are
    more than WORD_CHANGES_MAX changes."""
    changes, deleted, inserted = [], "", ""

    def flush() -> None:
        nonlocal deleted, inserted
        gone, added = deleted.strip(), inserted.strip()
        if gone and added:
            changes.append(f"„{gone}“ → „{added}“")
        elif gone:
            changes.append(f"„{gone}“ gestrichen")
        elif added:
            changes.append(f"„{added}“ ergänzt")
        deleted = inserted = ""

    shared = False
    for op, text in word_diff(old, new):
        if op == "delete":
            deleted += text
        elif op == "insert":
            inserted += text
        else:
            shared = shared or bool(text.strip())
            flush()
    flush()
    if not shared:  # nothing in common: the whole sentence was replaced, "- / +" says that already
        return ""
    return " · ".join(changes) if len(changes) <= WORD_CHANGES_MAX else ""


def sentence_diff(old: str | None, new: str | None, max_chars: int = 1500) -> str:
    """Only the changed sentences: "- removed sentence" / "+ added sentence". An edited sentence comes as the pair
    "- vorher" / "+ nachher" plus an indented line with its word changes ("  Wörter: „sehr“ gestrichen"), so a
    one-word change can't go unnoticed. "" without changes; STRUCTURE_ONLY when only spaces, paragraphs or line
    breaks changed. Longer results are cut at max_chars (whole lines, then "…")."""
    a, b = sentences(old), sentences(new)
    lines: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        for before, after in _pairs(a[i1:i2], b[j1:j2]):
            if before is not None and after is not None and before.split() == after.split():
                continue  # only spaces inside the sentence changed
            if before is not None:
                lines.append("- " + before)
            if after is not None:
                lines.append("+ " + after)
            if before is not None and after is not None and (words := word_changes(before, after)):
                lines.append("  Wörter: " + words)
    if not lines:
        return STRUCTURE_ONLY if (old or "").strip() != (new or "").strip() else ""
    result, used = [], 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            result.append("…")
            break
        result.append(line)
        used += len(line) + 1
    return "\n".join(result)
