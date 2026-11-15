"""The editor's "Verlauf" (history.timeline: every version like a commit log) and the editor layout around it: no
quick buttons, the ad in a bottom drawer ("Inserat ansehen") with "Uninteressant", the folding bottom prompts."""

import asyncio
import re

from app import db, editor, history, learning, web

from .test_editor import (client, editor_cookie, fake_generate, lid, login, open_editor,  # noqa: F401 (fixtures)
                          post)


def start(listing_id, text="Guten Tag,\nich interessiere mich für die Wohnung.\nViele Grüße"):
    db.add_draft(listing_id, "initial", text, "claude-opus-5-5", hint="Codewort **Linde** nennen")
    return learning.open_or_create_session(listing_id)


def test_timeline_of_a_whole_session(client, lid, fake_generate):
    session = start(lid)
    sid = session["id"]
    edited = "Guten Tag,\nich interessiere mich sehr für die Wohnung.\nViele Grüße"
    asyncio.run(learning.revise_in_editor(sid, edited, "kürzer bitte"))  # with a hand edit before it
    learning.decide_revision(sid, True)
    db.set_personal(lid, "", 3)
    request = editor.personal_revision(db.get_listing(lid))
    asyncio.run(learning.revise_in_editor(sid, "Fassung von Claude 1", request))  # only the level changed
    learning.decide_revision(sid, False)  # back to "Fassung von Claude 1"
    learning.finish(sid, "Fassung von Claude 1, fertig", note="Balkon erwähnen")

    entries, base = history.timeline(lid, web.DRAFT_KIND_LABELS)
    assert base is None  # done: no live entry
    assert [(e["who"], e["label"]) for e in entries] == [
        ("claude", "erster Entwurf"),
        ("you", "von Hand geändert"),
        ("claude", "überarbeitet nach"),
        ("claude", "überarbeitet: Mühe → Volle Mühe"),
        ("you", "von Hand geändert"),
        ("note", "beim Abschließen notiert, nicht an Claude geschickt"),
    ]
    first, hand, revised, level, final, note = entries
    assert first["diff"] is None and first["hint"] == "Codewort **Linde** nennen" and first["text"].startswith("Guten Tag")
    assert hand["text"] == edited and hand["added"] == 1 and hand["removed"] == 0
    assert any(op == "insert" and "sehr" in t for op, t in hand["diff"])
    assert revised["quote"] == "kürzer bitte" and revised["text"] == "Fassung von Claude 1"
    assert revised["hint"] == "Codewort Linde" and revised["decision"] == "übernommen"
    assert level["quote"] is None and level["decision"] == "verworfen" and level["text"] == "Fassung von Claude 2"
    # rejected: her next version is compared with the text she went back to, not with Claude's rejected one
    assert final["text"] == "Fassung von Claude 1, fertig" and final["added"] == 1 and final["removed"] == 0
    assert note["quote"] == "Balkon erwähnen"


def test_timeline_open_session_and_rounds(client, lid, fake_generate):
    session = start(lid)
    learning.save_text(session["id"], "Guten Tag, von Hand")
    entries, base = history.timeline(lid)
    assert [e["who"] for e in entries] == ["claude"] and base == entries[0]["text"]  # edits still live, not a version
    db.add_draft(lid, "regenerate", "Ganz neuer Entwurf", "claude-opus-5-5", hint=None)
    learning.start_new_round(lid)  # the old session is superseded with its saved hand edits
    entries, base = history.timeline(lid, web.DRAFT_KIND_LABELS)
    assert [(e["who"], e["label"]) for e in entries] == [
        ("claude", "erster Entwurf"), ("you", "von Hand geändert"), ("claude", "neuer Entwurf (Neu erzeugt)")]
    assert entries[2]["diff"] is not None and base == "Ganz neuer Entwurf"


def test_nothing_changed_by_hand_is_no_version(client, lid, fake_generate):
    session = start(lid)
    asyncio.run(learning.revise_in_editor(session["id"], session["original_text"], "netter"))
    assert [e["who"] for e in history.timeline(lid)[0]] == ["claude", "claude"]


def test_personal_labels():
    assert history.personal_label(editor.personal_revision({"effort_level": 1})) == "Mühe → Ehrlich"
    assert history.personal_label(editor.personal_revision({"effort_level": 3, "own_words": "x"})) == "Mühe → Volle Mühe"
    assert history.personal_label(editor.personal_revision({"effort_level": 2})) == "Mühe → Normal"
    assert history.personal_label(editor.personal_revision({"effort_level": 2, "own_words": "x"})) == \
        "deine Zeile aufgegriffen"
    assert history.personal_label("kürzer") is None


def test_editor_page_shows_the_verlauf_collapsed_at_the_bottom(client, lid, fake_generate):
    start(lid)
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    post(client, lid, "revise", fields, text="Guten Tag, von Hand", critique="kürzer bitte")
    r, fields = open_editor(client, lid)
    page = r.text
    side = page.split('class="ed-side"')[1]
    assert side.index('id="kritik"') < side.index('class="ed-card ed-answer"') < side.index('id="verlauf"')
    verlauf = page.split('<details class="disclosure ed-verlauf" id="verlauf">')[1].split("</details>\n{%")[0]
    assert '<details class="disclosure ed-verlauf" id="verlauf" open' not in page  # collapsed
    assert '<span class="count">3</span>' in verlauf  # Claude, you, Claude
    assert "jetzt – noch nicht an Claude geschickt" in verlauf and 'data-original="Fassung von Claude 1"' in verlauf
    assert verlauf.index("überarbeitet nach") < verlauf.index("von Hand geändert") < verlauf.index("erster Entwurf")
    assert "<q>kürzer bitte</q>" in verlauf and "Hinweis von Claude" in verlauf and "<strong>Linde</strong>" in verlauf
    assert verlauf.count('<details class="vl-diff" open>') == 1  # only the newest diff open
    assert "Wörter" in verlauf
    # gone: the quick buttons, the separate diff box, the old list
    assert "ed-quick" not in page and "Oder mit einem Tipp" not in page and "Ganz neu schreiben" not in page
    assert "Deine Änderungen an Claudes Entwurf" not in page and 'class="ed-history"' not in page


def test_questions_are_not_versions(client, lid, fake_generate, monkeypatch):
    start(lid)
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)

    async def answer(listing, settings, question, letter):
        db.add_llm_usage("question", "anthropic", "m", listing_id=listing["id"])
        return "Steht nicht drin."

    from app import llm
    monkeypatch.setattr(llm, "answer_question", answer)
    post(client, lid, "ask", fields, text="Guten Tag, Entwurf", critique="Wo ist die Mail?")
    assert [e["who"] for e in history.timeline(lid)[0]] == ["claude"]
    assert "Wo ist die Mail?" not in open_editor(client, lid)[0].text.split('id="verlauf"')[1]


def test_drawer_with_the_ad_and_uninteressant(client, lid):
    start(lid)
    db.set_listing_details(lid, "Zimmer: 2", None)
    editor_cookie(client, lid)
    page = open_editor(client, lid)[0].text
    head = page.split('<header class="ed-head">')[1].split("</header>")[0]
    assert "ed-facts" not in head and "780 €" not in head and "Helle 2-Zimmer-Wohnung" in head  # the title stays
    handle = page.split('class="ed-drawer-handle"')[1].split("</a>")[0]
    assert 'href="#inserat"' in handle and 'aria-controls="inserat" aria-expanded="false"' in handle
    assert "<strong>Inserat ansehen</strong> · 780 € · 54 m²" in handle
    sheet = page.split('<dialog class="ed-sheet" id="inserat"')[1].split("</dialog>")[0]
    assert '<p class="ed-facts">780 € · 54 m² · Musterstr. 12 · <a href="' in sheet and "Karte ↗" in sheet
    assert f'action="/editor/{lid}/dismiss"' in sheet and "❌ Uninteressant" in sheet
    assert "Beschreibung &amp; Details" in sheet
    # the drawer is outside the editor's form (no nested forms)
    form = page.split('id="ed-form"')[1].split("</form>\n")[0]
    assert "ed-sheet" not in form

    db.discard(lid)
    sheet = open_editor(client, lid)[0].text.split('<dialog class="ed-sheet"')[1].split("</dialog>")[0]
    assert f'action="/editor/{lid}/restore"' in sheet and "Uninteressant</button>" not in sheet
    db.restore(lid)
    db.mark_sent(lid)
    sheet = open_editor(client, lid)[0].text.split('<dialog class="ed-sheet"')[1].split("</dialog>")[0]
    assert "/dismiss" not in sheet and "/restore" not in sheet


def test_drawer_before_the_first_draft_and_bottom_prompts_fold(client, lid):
    editor_cookie(client, lid)
    page = client.get(f"/editor/{lid}").text  # no draft yet: the progress bar, the ad below in the drawer
    assert 'data-draft-job="' in page and 'class="ed-drawer-handle"' in page and "❌ Uninteressant" in page
    db.add_draft(lid, "initial", "Guten Tag", "claude-opus-5-5")
    page = client.get(f"/editor/{lid}").text
    bar = page.split('class="ask-sheet ask-bar"')[1].split("</aside>")[0]
    assert "data-ask-fold" in bar and "data-ask-unfold" in bar and "Uninteressant?" in bar
    assert f'id="editor-{lid}-ask"' in page and f'aria-controls="editor-{lid}-ask"' in bar


def test_first_draft_view_is_the_editor_layout(client, lid):
    editor_cookie(client, lid)
    page = client.get(f"/editor/{lid}").text  # no draft yet
    form = page.split(f'<form method="post" action="/editor/{lid}/draft" class="ed-grid" data-draft-start>')[1]
    form = form.split("</form>")[0]
    main, side = form.split('<div class="ed-side">')
    assert 'class="ed-card ed-letter ed-writing" id="text"' in main and "Dein Anschreiben" in main
    assert "data-draft-bar" in main and "data-draft-start-row" in main
    assert 'id="kritik"' in side and 'name="effort_level"' in side and 'name="own_words"' in side  # go into the draft
    assert re.search(r'<textarea id="ed-critique-wait"[^>]* disabled', side) and side.count("disabled>") == 2
    assert 'name="critique"' not in side and "ed-verlauf" not in page
    head = page.split('<header class="ed-head">')[1].split("</header>")[0]
    assert "780 €" not in head and "ed-facts" not in head
    assert 'class="disclosure" id="bilder"' not in page and 'class="ed-bottom"' in page


def test_open_text_has_inserat_in_the_kopieren_bar(client, lid):
    start(lid)
    editor_cookie(client, lid)
    page = open_editor(client, lid)[0].text
    bar = page.split('<div class="ed-bar">')[1].split("</div>")[0]
    assert "Kopieren &amp; Inserat öffnen" in bar
    assert '<a class="ed-bar-inserat" href="#inserat" data-drawer-open aria-controls="inserat"' in bar
    assert page.count("data-drawer-open") == 2  # and the handle under the right column
