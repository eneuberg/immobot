"""ui-final: word flags on cards (triage.py), ❤️ + "Was weißt/magst du?" into the prompt (llm.personal_block),
"Bewerbungen" (db.listing_outcomes, flow.py) and "So schreibe ich" (/stil, flow.py)."""

from datetime import datetime, timedelta, timezone

from app import cards, db, editor, learning, llm, triage

from .test_editor import (client, editor_cookie, fake_generate, lid, login, open_editor,  # noqa: F401 (fixtures)
                          post)


def listing(**extra):
    return {"title": "Wohnung", "description": "", "details": "", **extra}


def labels(found):
    return [f["label"] for f in found]


# --- word flags ---------------------------------------------------------------------------------------------------

def test_flags_find_what_speaks_against_and_for_a_listing():
    assert labels(triage.flags(listing(title="Tauschwohnung 2 Zimmer", description="mit Balkon"))) == \
        ["Tauschwohnung", "Balkon/Terrasse"]
    assert labels(triage.flags(listing(title="WBS-Wohnung"))) == ["WBS nötig"]
    assert labels(triage.flags(listing(details="Wohnberechtigungsschein erforderlich: ja"))) == ["WBS nötig"]
    assert labels(triage.flags(listing(description="Befristeter Mietvertrag bis 2027, möbliert"))) == \
        ["befristet", "möbliert"]
    assert labels(triage.flags(listing(title="WG-Zimmer in Ehrenfeld"))) == ["WG"]
    assert labels(triage.flags(listing(description="Einbauküche vorhanden")))[-1] == "Küche"
    assert labels(triage.flags(listing(), duplicate_count=2)) == ["2× gemeldet"]


def test_flags_skip_the_false_positives_of_the_real_feed():
    quiet = [
        listing(description="Der Kühlschrank: ein Austausch wäre sinnvoll. Geborgenheit, Austausch"),  # no Tausch
        listing(description="Der Mietvertrag ist unbefristet."),
        listing(description="Für eine WG ungeeignet."),  # WG only from the title
        listing(details="Wohnberechtigungsschein erforderlich: nein"),
        listing(description="Leider kein Balkon, keine Einbauküche"),
        listing(details="Balkon/Terrasse: nein"),
        listing(description="unmöbliert, teilmöbliert"),
    ]
    for item in quiet:
        assert triage.flags(item) == [], item


def test_duplicates_by_street_and_price(fresh_db):
    a = db.insert_listing({"fredy_id": "d1", "title": "A", "price": "700 €", "address": "Hauptstr. 3, 51103 Köln"})[0]
    b = db.insert_listing({"fredy_id": "d2", "title": "B", "price": "700 €", "address": "Hauptstraße 3, 51103 Köln, Kalk"})[0]
    c = db.insert_listing({"fredy_id": "d3", "title": "C", "price": "800 €", "address": "Hauptstraße 3, 51103 Köln"})[0]
    d = db.insert_listing({"fredy_id": "d4", "title": "D", "price": "700 €", "address": "50737 Köln, Weidenpesch"})[0]
    e = db.insert_listing({"fredy_id": "d5", "title": "E", "price": "700 €", "address": "50737 Köln, Weidenpesch"})[0]
    assert triage.duplicates([a, b, c, d, e]) == {a: 2, b: 2}  # a postcode is no street
    assert triage.duplicates([]) == {}


def test_card_shows_word_flags_and_heart(client):
    login(client)
    lid_ = db.insert_listing({"fredy_id": "t1", "provider": "immoscout", "title": "Tauschwohnung mit Balkon",
                              "price": "700 €", "url": "https://portal.example/expose/9"})[0]
    db.set_personal(lid_, "", True)
    page = client.get("/").text
    card = page.split(f'id="l{lid_}"')[1].split('class="lc-toggle"')[0]
    assert '<li class="lc-tag is-dream">❤️ Traumwohnung</li>' in card
    assert '<li class="lc-tag is-warn">Tauschwohnung</li>' in card and '<li class="lc-tag is-good">Balkon/Terrasse</li>' in card


# --- ❤️ and "Was weißt/magst du?" ----------------------------------------------------------------------------------

def test_personal_block_only_when_given():
    assert llm.personal_block(listing()) is None
    block = llm.personal_block(listing(own_words="  kenne die Ecke,\n Garten <b>super</b> ", dream=1))
    assert block.startswith("<persoenlich>\n") and block.endswith("\n</persoenlich>")
    assert "Traumwohnung" in block and "„kenne die Ecke, Garten bsuper/b“" in block  # one line, no tags
    assert "erfinde nichts dazu" in block
    text = llm.build_user_text(listing(own_words="schon öfter in der Naumannsiedlung"), "initial")
    assert text.index("</inserat>") < text.index("<persoenlich>") < text.index("## Aufgabe")
    assert "Traumwohnung" not in text
    assert "<persoenlich>" not in llm.build_user_text(listing(), "initial")


def test_set_personal(fresh_db):
    lid_ = db.insert_listing({"fredy_id": "p1", "title": "A"})[0]
    assert db.set_personal(lid_, "  Garten   ist super ", True) is True
    row = db.get_listing(lid_)
    assert (row["own_words"], row["dream"]) == ("Garten ist super", 1)
    assert db.set_personal(lid_, "Garten ist super", True) is False  # nothing changed
    assert db.set_personal(lid_, "x" * 500, False) is True and len(db.get_listing(lid_)["own_words"]) == db.OWN_WORDS_MAX
    assert db.set_personal(lid_, " ", False) is True and db.get_listing(lid_)["own_words"] is None


def test_listing_page_main_action_takes_the_personal_input(client, lid, monkeypatch):
    login(client)
    seen = []

    async def create(listing_id, kind, instruction=None, source=None, record_feedback=True):
        seen.append(llm.personal_block(db.get_listing(listing_id)))
        db.add_draft(listing_id, kind, "Guten Tag", "claude-opus-5-5")
        return db.list_drafts(listing_id)[-1]

    monkeypatch.setattr(llm, "create_and_store_draft", create)
    page = client.get(f"/listing/{lid}").text
    form = page.split(f'action="/listing/{lid}/draft"')[1].split("</form>")[0]
    assert 'name="personal" value="1"' in form and 'name="dream"' in form and 'name="own_words"' in form
    assert "Was weißt/magst du? (optional)" in form
    r = client.post(f"/listing/{lid}/draft", data={"then": "editor", "personal": "1", "dream": "1",
                                                   "own_words": "kenne die Ecke"})
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}#text"
    assert "Traumwohnung" in seen[0] and "„kenne die Ecke“" in seen[0]  # stored before Claude was asked
    client.post(f"/listing/{lid}/draft", data={})  # "Neues Anschreiben erzeugen" (no fields): stays as it was
    assert db.get_listing(lid)["own_words"] == "kenne die Ecke"


def test_editor_first_draft_and_revision_use_the_personal_input(client, lid, fake_generate, monkeypatch):
    editor_cookie(client, lid)
    r, fields = open_editor(client, lid)
    assert 'id="en-words"' in r.text  # no draft yet: asked before the first draft
    prompts = []
    real = llm.build_user_text
    monkeypatch.setattr(llm, "build_user_text", lambda *a, **k: prompts.append(real(*a, **k)) or prompts[-1])

    async def create(listing_id, kind, instruction=None, *, source="dashboard", record_feedback=True):
        llm.build_user_text(db.get_listing(listing_id), kind)
        db.add_draft(listing_id, kind, "Guten Tag", "claude-opus-5-5")
        return db.latest_draft(listing_id)

    monkeypatch.setattr(llm, "create_and_store_draft", create)
    r = post(client, lid, "draft", fields, personal="1", own_words="Garten ist super")
    assert r.status_code == 303 and "„Garten ist super“" in prompts[-1]

    r, fields = open_editor(client, lid)
    assert 'id="ed-words"' in r.text and 'value="Garten ist super"' in r.text
    r = post(client, lid, "revise", fields, text="Guten Tag", critique="", personal="1", dream="1",
             own_words="Garten ist super")
    assert r.status_code == 303  # only ❤️ changed: a revision without a typed critique
    assert fake_generate[-1]["instruction"] == editor.PERSONAL_REVISION
    assert db.get_listing(lid)["dream"] == 1
    r = post(client, lid, "revise", fields, text="Fassung von Claude 1", critique="", personal="1", dream="1",
             own_words="Garten ist super")
    assert r.status_code == 400  # nothing changed and no critique: as before


# --- Bewerbungen --------------------------------------------------------------------------------------------------

def test_application_view():
    now = datetime(2026, 11, 15, 12, tzinfo=timezone.utc)
    sent = {"sent_at": (now - timedelta(days=9)).isoformat()}
    waiting = cards.application(sent, [], now)
    assert (waiting["stage"], waiting["stage_label"], waiting["sent_days"], waiting["quiet"]) == ("wartet", "Gesendet", 9, True)
    steps = [{"kind": "antwort", "happened_on": None, "note": None},
             {"kind": "besichtigung", "happened_on": "2026-11-18T17:30", "note": "Unterlagen"}]
    viewing = cards.application(sent, steps, now)
    assert (viewing["stage"], viewing["viewing_label"], viewing["quiet"]) == ("besichtigung", "Mi 18.11., 17:30 Uhr", False)
    assert [e["label"] for e in viewing["events"]] == ["Antwort", "Besichtigung"]
    assert cards.when_label("2026-11-18") == "Mi 18.11." and cards.when_label(None) is None


def test_outcomes_only_for_sent_listings(fresh_db):
    lid_ = db.insert_listing({"fredy_id": "o1", "title": "A"})[0]
    assert db.add_outcome(lid_, "antwort") is None
    db.mark_sent(lid_)
    assert db.add_outcome(lid_, "antwort") and db.add_outcome(lid_, "besichtigung", "2026-11-18T17:30")
    assert [e["kind"] for e in db.outcomes([lid_])[lid_]] == ["antwort", "besichtigung"]
    assert db.undo_outcome(lid_) and [e["kind"] for e in db.outcomes()[lid_]] == ["antwort"]
    assert db.outcomes([]) == {}


def test_bewerbungen_page_and_steps(client, lid):
    login(client)
    page = client.get("/bewerbungen").text
    assert "Noch nichts abgeschickt" in page and '<a href="/bewerbungen" aria-current="page">Bewerbungen</a>' in page
    db.mark_sent(lid)
    page = client.get("/bewerbungen").text
    assert "<strong>1</strong> abgeschickt · 0 mit Antwort" in page and "Wartet auf Antwort" in page
    assert f'action="/bewerbung/{lid}"' in page and "Wie geht's weiter?" in page
    r = client.post(f"/bewerbung/{lid}", data={"kind": "besichtigung", "when": "2099-11-18", "time": "17:30",
                                              "note": " Unterlagen   mitbringen ", "next": "/bewerbungen"})
    assert r.status_code == 303 and r.headers["location"] == f"/bewerbungen?m=step#b{lid}"
    (step,) = db.outcomes([lid])[lid]
    assert (step["kind"], step["happened_on"], step["note"], step["source"]) == \
        ("besichtigung", "2099-11-18T17:30", "Unterlagen mitbringen", "dashboard")
    page = client.get("/bewerbungen?m=step").text
    assert "Eingetragen." in page and "Nächste Besichtigung" in page and "17:30 Uhr" in page
    assert "„Besichtigung“ zurücknehmen" in page
    client.post(f"/bewerbung/{lid}", data={"kind": "nonsense"})
    client.post(f"/bewerbung/{lid}", data={"kind": "besichtigung", "when": "kein Datum"})
    assert [s["happened_on"] for s in db.outcomes([lid])[lid]] == ["2099-11-18T17:30", None]
    r = client.post(f"/bewerbung/{lid}/zurueck", data={"next": f"/listing/{lid}"})
    assert r.headers["location"] == f"/listing/{lid}?m=undo#bewerbung"
    assert len(db.outcomes([lid])[lid]) == 1
    r = client.post(f"/bewerbung/{lid}", data={"kind": "zusage", "next": "https://evil.example/"})
    assert r.headers["location"] == f"/bewerbungen?m=step#b{lid}"  # foreign next ignored
    assert "Zu- und Absagen" in client.get("/bewerbungen").text


def test_listing_page_of_a_sent_listing_has_the_application(client, lid):
    login(client)
    assert 'id="bewerbung"' not in client.get(f"/listing/{lid}").text
    db.mark_sent(lid)
    db.add_outcome(lid, "antwort", note="Frau Peil ruft an")
    page = client.get(f"/listing/{lid}").text
    box = page.split('id="bewerbung"')[1].split("</details>")[0]
    assert "Antwort</span>" in box and "„Frau Peil ruft an“" in box
    assert f'<input type="hidden" name="next" value="/listing/{lid}">' in box


# --- So schreibe ich ----------------------------------------------------------------------------------------------

def proposals(n, days_old=0):
    when = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat(timespec="seconds")
    ids = []
    for i in range(n):
        ids.append(db.add_rule(f"Regel {days_old}-{i}", status="proposed"))
        with db.connect() as conn:
            conn.execute("UPDATE learned_rules SET created_at = ? WHERE id = ?", (when, ids[-1]))
    return ids


def test_stil_shows_few_proposals_and_lets_old_ones_expire(client):
    login(client)
    old = proposals(3, days_old=30)
    fresh = proposals(7)
    page = client.get("/stil").text
    assert '<a href="/stil" aria-current="page">Stil</a>' in page
    shown = page.split('<ul class="st-proposals">')[1].split('class="st-section" id="regeln"')[0]
    assert shown.count('class="st-proposal"') == 5 and f'/stil/vorschlag/{old[0]}"' not in shown
    assert "Noch 5 weitere" in page and "<strong>3</strong> Vorschläge sind älter als 14 Tage" in page
    r = client.post("/stil/aufraeumen")
    assert r.headers["location"] == "/stil?m=aufgeraeumt#neu"
    assert {db.get_rule(i)["status"] for i in old} == {"rejected"}
    assert {db.get_rule(i)["status"] for i in fresh} == {"proposed"}

    r = client.post(f"/stil/vorschlag/{fresh[0]}", data={"decision": "ja"})
    assert r.headers["location"] == "/stil?m=ja#neu" and db.get_rule(fresh[0])["status"] == "active"
    client.post(f"/stil/vorschlag/{fresh[1]}", data={"decision": "nein"})
    assert db.get_rule(fresh[1])["status"] == "rejected"
    client.post(f"/stil/vorschlag/{fresh[2]}", data={"decision": "angepasst", "text": "Kein Ausrufezeichen.",
                                                     "scope": "private Vermieter", "strength": "hard"})
    rule = db.get_rule(fresh[2])
    assert (rule["status"], rule["text"], rule["scope"], rule["strength"]) == \
        ("active", "Kein Ausrufezeichen.", "private Vermieter", "hard")
    r = client.post(f"/stil/vorschlag/{fresh[2]}", data={"decision": "ja"})  # decided already
    assert "/stil?error=" in r.headers["location"]

    r = client.post(f"/stil/regel/{fresh[0]}", data={"active": "0"})
    assert r.headers["location"] == "/stil?m=aus#regeln" and db.get_rule(fresh[0])["status"] == "inactive"
    assert "Wieder an" in client.get("/stil").text
    client.post(f"/stil/regel/{fresh[0]}", data={"active": "1"})
    assert db.get_rule(fresh[0])["status"] == "active"
    assert learning.MAX_ACTIVE_RULES  # the page says "x/max"


def test_hint_box_opens_by_itself_only_for_a_warning(client, lid):
    login(client)
    db.add_draft(lid, "initial", "Text", "claude-opus-5-5", hint="- **Vorsicht:** Vorkasse verlangt")
    assert '<details class="ed-hints is-alarm" id="hinweise" open>' in client.get(f"/listing/{lid}").text
