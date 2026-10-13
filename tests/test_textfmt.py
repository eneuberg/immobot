"""Simple Markdown in Claude's notes (app/textfmt.py): the dashboard renderer and the Telegram conversion."""

import pytest
from markupsafe import Markup

from app.textfmt import basic_markdown, telegram_html

GENDER = "Liebe Vermieter*innen, Mieter*in und Vermieter*in"


def md(text):
    result = basic_markdown(text)
    assert isinstance(result, Markup)
    return str(result)


def test_escapes_everything_first():
    assert md("<script>alert(1)</script> & <b>x</b>") == (
        "<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;</p>")
    assert md("**<i>x</i>**") == "<p><strong>&lt;i&gt;x&lt;/i&gt;</strong></p>"
    assert md("`<b>`") == "<p><code>&lt;b&gt;</code></p>"
    assert md(None) == md("") == md(" \n\n ") == ""


@pytest.mark.parametrize("text, expected", [
    ("Das ist **wichtig**.", "Das ist <strong>wichtig</strong>."),
    ("**a** und **b**", "<strong>a</strong> und <strong>b</strong>"),
    ("Ganz *sicher*, (*so*) und *x*.", "Ganz <em>sicher</em>, (<em>so</em>) und <em>x</em>."),
    ("Befehl `a*b*c` und `**x**`", "Befehl <code>a*b*c</code> und <code>**x**</code>"),
    ("**Mieter*innen** zuerst", "<strong>Mieter*innen</strong> zuerst"),
    ("*Hinweis: Mieter*innen fragen*", "<em>Hinweis: Mieter*innen fragen</em>"),
])
def test_inline_formatting(text, expected):
    assert md(text) == f"<p>{expected}</p>"


@pytest.mark.parametrize("text", [
    GENDER,
    "Ansprechpartner*in: Frau X",
    "5 * 3 = 15, a*b und 2*3*4",
    "**offen und *auch offen",
    "ein * kein Kursiv*",
    "*kein Kursiv *",
    "_unterstrichen_ [Link](https://example.com) ![Bild](https://example.com/a.png)",
    "``",
])
def test_left_as_it_is(text):
    """The gender star, lone or space-flanked markers, underscores, links and images: plain text."""
    assert md(text) == f"<p>{text.replace('&', '&amp;')}</p>"


def test_nested_markers_never_cross():
    assert md("*a **b** c*") == "<p>*a <strong>b</strong> c*</p>"  # valid HTML, the outer stars stay
    assert md("***x***") == "<p><strong>*x</strong>*</p>"


def test_paragraphs_and_line_breaks():
    assert md("Erste Zeile\nzweite Zeile\n\n\nNeuer Absatz") == "<p>Erste Zeile<br>zweite Zeile</p><p>Neuer Absatz</p>"
    assert md("a\r\nb") == "<p>a<br>b</p>"


def test_lists():
    hint = ("- **Objekt-ID senden:** Die LEG bittet zusätzlich darum.\n"
            "* **E-Mail bestätigen:** Nach dem Absenden …\n"
            "• dritter Punkt")
    assert md(hint) == ("<ul><li><strong>Objekt-ID senden:</strong> Die LEG bittet zusätzlich darum.</li>"
                        "<li><strong>E-Mail bestätigen:</strong> Nach dem Absenden …</li><li>dritter Punkt</li></ul>")
    assert md("Bitte:\n1. eins\n2) zwei") == "<p>Bitte:</p><ol><li>eins</li><li>zwei</li></ol>"
    assert md("3. drei\n4. vier") == '<ol start="3"><li>drei</li><li>vier</li></ol>'
    assert md("- a\n  weiter\nDanach") == "<ul><li>a<br>weiter</li></ul><p>Danach</p>"  # indented: in the item
    assert md("- a\n\n- b") == "<ul><li>a</li></ul><ul><li>b</li></ul>"
    assert md("- a\n1. b") == "<ul><li>a</li></ul><ol><li>b</li></ol>"
    assert md("Einzug am\n15. Dezember") == "<p>Einzug am<br>15. Dezember</p>"  # no list inside a paragraph
    assert md("-5 Grad, *nicht* -") == "<p>-5 Grad, <em>nicht</em> -</p>"


def test_headings_become_bold_paragraphs():
    assert md("# Titel\n## Wichtig ##\n### **Drei**\nText") == (
        "<p><strong>Titel</strong></p><p><strong>Wichtig</strong></p><p><strong><strong>Drei</strong></strong></p>"
        "<p>Text</p>")
    assert md("#hashtag und #1") == "<p>#hashtag und #1</p>"


# --- Telegram ------------------------------------------------------------------------------------------------------

def test_telegram_html():
    hint = ("Zwei Punkte <wichtig> & so:\n"
            "- **Objekt-ID senden:** bitte *zusätzlich*\n"
            "* `Code`\n"
            "## Titel **fett**\n"
            + GENDER)
    assert telegram_html(hint) == ("Zwei Punkte &lt;wichtig&gt; &amp; so:\n"
                                   "• <b>Objekt-ID senden:</b> bitte <i>zusätzlich</i>\n"
                                   "• <code>Code</code>\n"
                                   "<b>Titel fett</b>\n"
                                   + GENDER)
    assert telegram_html("**offen und *offen") == "**offen und *offen"
    assert telegram_html("") == ""
