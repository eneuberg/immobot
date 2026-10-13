"""Simple Markdown in Claude's notes to the user: the draft hint and the reasons of rule proposals and rewrite edits.

basic_markdown() renders them for the dashboard (Jinja filter "md"), telegram_html() for a Telegram message (parse
mode HTML). Both escape everything first, so no HTML from the model gets through, and only ever emit balanced tags.
Never used for the draft itself or the user's own texts: those are sent or copied exactly as they are.

Supported: paragraphs (blank line), line breaks, "- " / "* " / "• " and "1. " / "1) " lists, "#" headings (shown as
a bold paragraph), **bold**, *italic* and `code`. A single "*" only opens italics after the start, a space or a
punctuation mark and before a non-space, and only closes before the end, a space or a punctuation mark, so the
gender star ("Mieter*innen") stays as it is. Underscores, links and images stay plain text, and so does a marker
without a partner.
"""

import html
import re

from markupsafe import Markup, escape

_BULLET = re.compile(r"^[ \t]*[-*•][ \t]+(.*)$")
_NUMBER = re.compile(r"^[ \t]*(\d{1,9})[.)][ \t]+(.*)$")
_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.*?)[ \t#]*$")
_CODE = re.compile(r"`([^`\n]+)`")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<![\w*])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\w*])")


def _inline(line: str, strong: str = "strong", em: str = "em", code: str = "code") -> str:
    """One escaped line with `code`, **bold** and *italic* as tags. Nothing is formatted inside code, and italics
    are matched inside and outside of bold separately, so the tags always nest properly."""
    parts = []
    for i, piece in enumerate(_CODE.split(line)):
        if i % 2:
            parts.append(f"<{code}>{piece}</{code}>")
            continue
        for j, chunk in enumerate(_BOLD.split(piece)):
            chunk = _ITALIC.sub(rf"<{em}>\1</{em}>", chunk)
            parts.append(f"<{strong}>{chunk}</{strong}>" if j % 2 else chunk)
    return "".join(parts)


def _blocks(text: str) -> list[tuple[str, list, int]]:
    """[(kind, content, start)]: ("p", lines, 0), ("h", [line], 0), ("ul"/"ol", items as lists of lines, start)."""
    blocks: list[tuple[str, list, int]] = []
    current = None  # the open paragraph or list; a blank line or a heading closes it
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        heading = _HEADING.match(raw)
        bullet = _BULLET.match(raw)
        number = None if bullet else _NUMBER.match(raw)
        if number and current and current[0] == "p" and number.group(1) != "1":
            number = None  # "15. Dezember" at a line start inside a paragraph is no list (as in CommonMark)
        if not line:
            current = None
        elif heading:
            if heading.group(1):
                blocks.append(("h", [heading.group(1)], 0))
            current = None
        elif bullet or number:
            kind, item = ("ul", bullet.group(1)) if bullet else ("ol", number.group(2))
            if current and current[0] == kind:
                current[1].append([item.strip()])
            else:
                current = (kind, [[item.strip()]], int(number.group(1)) if number else 1)
                blocks.append(current)
        elif current and current[0] != "p" and raw[:1] in " \t":
            current[1][-1].append(line)  # indented: continues the list item
        elif current and current[0] == "p":
            current[1].append(line)
        else:
            current = ("p", [line], 0)
            blocks.append(current)
    return blocks


def basic_markdown(text: str | None) -> Markup:
    """Claude's note as safe HTML: <p>, <br>, <ul>/<ol>, <strong>, <em>, <code>. Headings become bold paragraphs."""
    out = []
    for kind, content, start in _blocks(text or ""):
        if kind == "ul" or kind == "ol":
            items = "".join("<li>" + "<br>".join(_inline(str(escape(line))) for line in item) + "</li>"
                            for item in content)
            attr = f' start="{start}"' if kind == "ol" and start != 1 else ""
            out.append(f"<{kind}{attr}>{items}</{kind}>")
        else:
            lines = "<br>".join(_inline(str(escape(line))) for line in content)
            out.append(f"<p><strong>{lines}</strong></p>" if kind == "h" else f"<p>{lines}</p>")
    return Markup("".join(out))


def telegram_html(text: str) -> str:
    """Claude's note as Telegram HTML, line by line: escaped, a list marker at the line start as "• ", a heading in
    bold, **bold**, *italic* and `code` as <b>, <i> and <code>. Shorten `text` before, never the result."""
    def inline(value: str) -> str:
        return _inline(html.escape(value, quote=False), "b", "i", "code")

    lines = []
    for line in (text or "").split("\n"):
        bullet, heading = _BULLET.match(line), _HEADING.match(line)
        if heading and heading.group(1):
            lines.append(f"<b>{inline(_BOLD.sub(r'\1', heading.group(1)))}</b>")  # bold as a whole: drop its own **
        else:
            lines.append(inline("• " + bullet.group(1) if bullet else line))
    return "\n".join(lines)
