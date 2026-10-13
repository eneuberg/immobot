"""Text diffs for the profile history, rewrite proposals and the draft editor.

word_diff() compares word by word (difflib) and returns segments [(op, text)] with op "equal", "insert" or
"delete". Joining the "equal" and "delete" texts gives the old text, "equal" and "insert" the new one, so all
whitespace and line breaks survive; macros.html renders the segments with text_diff() (<ins>/<del> in a
white-space: pre-wrap block). sentence_diff() is the compact form for prompts: only the changed sentences.
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


def sentence_diff(old: str | None, new: str | None, max_chars: int = 1500) -> str:
    """Only the changed sentences, one per line: "- removed sentence" / "+ added sentence". "" without changes.
    Longer results are cut at max_chars (whole lines, then "…")."""
    a, b = sentences(old), sentences(new)
    lines: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        lines += ["- " + sentence for sentence in a[i1:i2]]
        lines += ["+ " + sentence for sentence in b[j1:j2]]
    result, used = [], 0
    for line in lines:
        if used + len(line) + 1 > max_chars:
            result.append("…")
            break
        result.append(line)
        used += len(line) + 1
    return "\n".join(result)
