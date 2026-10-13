"""Einstellungen (Phase 2A): Profil with history and "Aus Feedback verbessern", Verhalten → Lernen, the editor login
hint on Zugänge, and learning costs in the totals. The general settings/credential tests are in test_web.py."""

import asyncio
import json
import re
import time

import pytest

from app import db, learning, llm, settings, web

from .test_web import add_listing, client, login  # noqa: F401 - "client" is a pytest fixture


@pytest.fixture(autouse=True)
def fresh_rewrite_state(monkeypatch):
    monkeypatch.setattr(web, "_rewrite_task", None)
    monkeypatch.setattr(learning, "_rewrite_running", False)


def save_profile(client, **fields):
    form = {"section": "profil", **{key: db.get_settings()[key] for key in settings.PROFILE_FIELDS}, **fields}
    return client.post("/settings", data=form)


def done_session(listing_id, original="Guten Tag. Ich bin Erika.", final="Hallo. Ich bin Erika, 34."):
    db.add_draft(listing_id, "initial", original, "claude-opus-5-5")
    session = learning.open_or_create_session(listing_id)
    return learning.finish(session["id"], final)


def make_proposal(*specs, feedback_ids=(), session_ids=()) -> int:
    """A pending proposal against the current profile, edits checked like learning._valid_edits does."""
    values = db.current_profile_values()
    edits = []
    for number, spec in enumerate(specs, start=1):
        edit = {"id": number, "old": "", "new": "", "reason": "", "evidence": [], "covers_rules": [], "is_fact": False,
                "decision": None, **spec}
        edit["problem"] = learning.edit_problem(values, edit)
        edit["applicable"] = edit["problem"] is None
        edits.append(edit)
    db.snapshot_profile("manual")  # the proposal's base is the current profile
    return db.add_rewrite_proposal(db.latest_version()["id"], edits, list(feedback_ids), list(session_ids))


def wait_for(predicate, timeout=3.0):
    """The background task runs in the TestClient's event loop thread."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- Profil: versions ------------------------------------------------------------------------

def test_profile_history_view_and_restore(client):
    login(client)
    save_profile(client, style="Locker, per Du.")
    r = save_profile(client, style="Förmlich, per Sie.", target_words="90")
    assert r.headers["location"] == "/settings?saved=1&version=3"
    page = client.get("/settings").text
    verlauf = page.split('id="verlauf"')[1]
    assert '<span class="count">3</span>' in verlauf and "Ausgangsstand" in verlauf and "gespeichert" in verlauf
    assert "<strong>#3</strong>" in verlauf and "aktuell" in verlauf
    assert 'href="/settings/version/2"' in verlauf and 'action="/settings/version/2/restore"' in verlauf
    assert 'href="/settings/version/3"' not in verlauf  # the current one has nothing to show or restore
    assert "data-confirm=\"Version #2 wiederherstellen?" in verlauf
    assert "<details >" in verlauf or "<details>" in verlauf  # collapsed
    assert "<details open>" in client.get("/settings?verlauf=1").text.split('id="verlauf"')[1]

    view = client.get("/settings/version/2").text  # version 2 -> current, per field
    assert "Version #2" in view and "gespeichert" in view
    style = view.split("<h3>Schreibstil</h3>")[1].split("</article>")[0]
    assert "<del>Locker</del><ins>Förmlich</ins>, per <del>Du</del><ins>Sie</ins>." in style
    assert "<del>150</del><ins>90</ins>" in view.split("<h3>Ziellänge</h3>")[1].split("</article>")[0]
    assert "unverändert" in view.split("<h3>Über mich</h3>")[1].split("</article>")[0]
    assert client.get("/settings/version/99").status_code == 404

    r = client.post("/settings/version/2/restore")
    assert r.status_code == 303 and r.headers["location"] == "/settings?restored=2&version=4"
    assert "Version #2 wiederhergestellt – jetzt Version #4." in client.get(r.headers["location"]).text
    latest = db.latest_version()
    assert (latest["source"], latest["note"], latest["data"]["style"]) == ("restore", "aus Version #2", "Locker, per Du.")
    assert "Das Profil entspricht genau dieser Version." in client.get("/settings/version/2").text
    r = client.post("/settings/version/2/restore")  # nothing to change: no version
    assert r.headers["location"] == "/settings?restored=2"
    assert "Das Profil entspricht schon Version #2 – nichts geändert." in client.get(r.headers["location"]).text
    assert client.post("/settings/version/99/restore").status_code == 404


def test_profile_history_shows_the_newest_and_all_on_request(client, monkeypatch):
    login(client)
    monkeypatch.setattr(web, "VERSIONS_SHOWN", 2)
    for words in ("100", "110", "120"):
        save_profile(client, target_words=words)
    page = client.get("/settings").text
    assert page.count('href="/settings/version/') == 1  # #4 (current) and #3 shown, #3 with "Ansehen"
    assert "Alle 4 Versionen zeigen" in page
    page = client.get("/settings?verlauf=alle").text
    assert page.count('href="/settings/version/') == 3 and "Alle 4 Versionen zeigen" not in page


# --- Profil: "Aus Rückmeldungen verbessern" -----------------------------------------------------------

def test_rewrite_prominence_follows_the_feedback(client):
    """Nothing yet: a quiet line with the threshold. Possible but little: a quiet line with a small button.
    Plenty: a card with the main button. A proposal waiting: a card with "Vorschlag ansehen"."""
    login(client)
    page = client.get("/settings").text
    assert 'class="rewrite-line"' in page and "möglich ab 3 Rückmeldungen oder 3 aktiven Regeln" in page
    assert "(bisher 0 Rückmeldungen, 0 Regeln)" in page and 'action="/settings/rewrite"' not in page
    lid = add_listing()
    for kind in ("shorter", "shorter", "formal"):
        db.add_feedback(lid, "telegram", "quick", kind)
    page = client.get("/settings").text
    assert '<article id="verbessern"' not in page
    line = page.split('<div class="rewrite-line" id="verbessern">')[1].split("</div>")[0]
    assert "3 Rückmeldungen und 0 aktive Regeln seit Version #1." in line and "ab etwa 10" in line
    assert 'action="/settings/rewrite"' in line and 'class="linkish">Verbesserung vorschlagen' in line
    for _ in range(learning.REWRITE_SUGGEST_AT - 3):
        db.add_feedback(lid, "telegram", "quick", "shorter")
    card = client.get("/settings").text.split('<article id="verbessern"')[1].split("</article>")[0]
    assert "10 Rückmeldungen und 0 aktive Regeln seit Version #1. Genug" in card and "is-active" in card
    assert 'action="/settings/rewrite"' in card and "Verbesserung vorschlagen" in card
    assert "bis zu ein paar Minuten" in card
    pid = make_proposal({"field": "target_words", "old": "150", "new": "120"})
    card = client.get("/settings").text.split('<article id="verbessern"')[1].split("</article>")[0]
    assert "Vorschlag bereit: <strong>1 Änderung</strong>" in card and f'href="/settings/vorschlag/{pid}"' in card
    assert 'action="/settings/rewrite"' not in card


def test_one_active_rule_is_not_enough_for_a_rewrite(client):
    login(client)
    db.add_rule("Kein Ausrufezeichen.", status="active")
    page = client.get("/settings").text
    assert '<article id="verbessern"' not in page and 'action="/settings/rewrite"' not in page
    assert "(bisher 0 Rückmeldungen, 1 Regel)" in page
    r = client.post("/settings/rewrite")
    assert r.status_code == 409 and "Noch zu wenig Rückmeldungen – möglich ab 3 Rückmeldungen" in r.text


@pytest.fixture
def gated_rewrite(monkeypatch):
    """learning.propose_rewrite replaced: waits for gate["open"], then makes a proposal (or raises gate["error"])."""
    gate = {"open": False, "calls": 0, "error": None}

    async def propose():
        gate["calls"] += 1
        while not gate["open"]:
            await asyncio.sleep(0.01)
        if gate["error"]:
            raise gate["error"]
        return db.get_rewrite_proposal(make_proposal({"field": "style", "old": "", "new": "Kurz und knapp."}))

    monkeypatch.setattr(learning, "propose_rewrite", propose)
    yield gate
    gate["open"] = True  # never leave a task waiting when the client closes


def enable_rewrite():
    lid = add_listing()
    for _ in range(3):
        db.add_feedback(lid, "telegram", "critique", "Bitte kürzer.")


def test_rewrite_runs_in_the_background(client, gated_rewrite):
    login(client)
    enable_rewrite()
    assert client.get("/settings/rewrite/status").json() == {"state": "none", "url": "/settings"}
    r = client.post("/settings/rewrite")
    assert r.status_code == 303 and r.headers["location"] == "/settings#verbessern"  # at once, not after the call
    assert client.get("/settings/rewrite/status").json()["state"] == "running"
    page = client.get("/settings").text
    assert "Vorschlag wird erstellt …" in page and 'data-rewrite-poll="/settings/rewrite/status"' in page
    assert '<noscript><meta http-equiv="refresh" content="10; url=/settings"></noscript>' in page  # without JS
    assert client.post("/settings/rewrite").status_code == 303  # one job at a time
    assert wait_for(lambda: gated_rewrite["calls"] == 1) and gated_rewrite["calls"] == 1

    gated_rewrite["open"] = True
    assert wait_for(lambda: client.get("/settings/rewrite/status").json()["state"] == "done")
    pid = db.list_rewrite_proposals(limit=1)[0]["id"]
    assert client.get("/settings/rewrite/status").json() == {"state": "done", "url": f"/settings/vorschlag/{pid}"}
    page = client.get("/settings").text
    assert "Vorschlag wird erstellt" not in page and "http-equiv" not in page and "Vorschlag bereit" in page


@pytest.mark.parametrize("error,shown", [
    (learning.LearningError("Ein Vorschlag wird schon erstellt – bitte kurz warten."), "Ein Vorschlag wird schon erstellt"),
    (llm.BudgetExceeded("Monatsbudget erreicht."), "Monatsbudget erreicht."),
    (RuntimeError("boom"), web.GENERIC_REWRITE_ERROR),
])
def test_rewrite_failure_is_shown_until_dismissed(client, gated_rewrite, error, shown):
    login(client)
    enable_rewrite()
    gated_rewrite.update(open=True, error=error)
    client.post("/settings/rewrite")
    assert wait_for(lambda: client.get("/settings/rewrite/status").json()["state"] == "failed")
    assert client.get("/settings/rewrite/status").json() == {"state": "failed", "url": "/settings"}
    card = client.get("/settings").text.split('<article id="verbessern"')[1].split("</article>")[0]
    assert "Der letzte Vorschlag ist fehlgeschlagen" in card and shown in card and "boom" not in card
    assert "Verbesserung vorschlagen" in card  # try again
    assert client.post("/settings/rewrite/dismiss").headers["location"] == "/settings"
    assert "fehlgeschlagen" not in client.get("/settings").text


def test_rewrite_cut_off_by_a_restart_counts_as_failed(client):
    login(client)
    enable_rewrite()
    db.set_meta(web.REWRITE_JOB_META, json.dumps({"state": "running", "started_at": db.now_iso()}))  # no task
    assert client.get("/settings/rewrite/status").json()["state"] == "failed"
    assert "Abgebrochen (Neustart?)" in client.get("/settings").text


def test_rewrite_needs_enough_feedback(client, gated_rewrite):
    login(client)
    r = client.post("/settings/rewrite")
    assert r.status_code == 409 and "Noch zu wenig Rückmeldungen" in r.text and gated_rewrite["calls"] == 0
    assert db.get_meta(web.REWRITE_JOB_META) is None


# --- Profil: proposal view, apply, reject, stale ------------------------------------------------------

def proposal_fixture():
    """Feedback, a session, a rule and a proposal with a normal edit, a fact, an append and an inapplicable edit."""
    lid = add_listing(title="Helle Wohnung")
    session = done_session(lid)
    critique = db.add_feedback(lid, "telegram", "critique", "Bitte ohne Floskeln am Anfang")
    quick = db.add_feedback(lid, "dashboard", "quick", "shorter")
    rule = db.add_rule("Keine Floskeln.", status="active")
    db.update_settings({"profile": "Name: Erika\nEinzug: ab 1.12.", "style": "Freundlich und förmlich."})
    pid = make_proposal(
        {"field": "style", "old": "förmlich", "new": "direkt", "reason": "Mehrfach gewünscht.",
         "evidence": [f"F{critique}", f"S{session['id']}", f"R{rule}"], "covers_rules": [rule]},
        {"field": "profile", "old": "ab 1.12.", "new": "ab sofort", "reason": "Selbst korrigiert.", "is_fact": True,
         "evidence": [f"F{quick}"]},
        {"field": "wishes", "old": "", "new": "- Balkon", "reason": "Neu."},
        {"field": "system_prompt", "old": "gibt es nicht", "new": "x", "reason": "Passt nicht."},
        feedback_ids=[critique, quick], session_ids=[session["id"]],
    )
    return pid, rule, critique


def test_proposal_page_shows_each_edit_in_context(client):
    login(client)
    pid, rule, critique = proposal_fixture()
    page = client.get(f"/settings/vorschlag/{pid}").text
    assert f'action="/settings/vorschlag/{pid}/apply"' in page and f'formaction="/settings/vorschlag/{pid}/reject"' in page
    assert "Ausgewählte übernehmen" in page and "Verwerfen" in page
    boxes = dict(re.findall(r'<input type="checkbox" name="edit" value="(\d)" ?(checked)?', page))
    assert boxes == {"1": "checked", "2": "", "3": "checked"}  # the fact is not preselected, edit 4 has no box
    cards = page.split('<article class="edit-card')[1:]
    assert "Schreibstil" in cards[0] and "Freundlich und <del>förmlich</del><ins>direkt</ins>." in cards[0]
    assert "„Bitte ohne Floskeln am Anfang“ (Telegram · Helle Wohnung)" in cards[0]
    assert "Deine Endversion zu „Helle Wohnung“" in cards[0] and "Regel „Keine Floskeln.“" in cards[0]
    assert "Arbeitet die Regel „Keine Floskeln.“ ins Profil ein" in cards[0]
    assert "⚠ Fakt" in cards[1] and "Nur übernehmen, wenn es stimmt." in cards[1]
    assert "Button „Kürzer“ (Dashboard · Helle Wohnung)" in cards[1]
    assert "<ins>- Balkon</ins>" in cards[2]  # appended at the end of the field
    assert "Nicht anwendbar: Textstelle nicht (mehr) im Feld gefunden." in cards[3]
    assert client.get("/settings/vorschlag/99").status_code == 404


def test_apply_selected_edits(client):
    login(client)
    pid, rule, critique = proposal_fixture()
    r = client.post(f"/settings/vorschlag/{pid}/apply", data={"edit": ["1", "3"]})
    assert r.status_code == 303 and r.headers["location"] == f"/settings?applied={pid}"
    current = db.get_settings()
    assert current["style"] == "Freundlich und direkt." and current["profile"].endswith("ab 1.12.")  # fact not taken
    assert current["wishes"].endswith("\n- Balkon")
    version = db.latest_version()
    assert version["source"] == "feedback" and version["note"] == "2 Änderungen aus Rückmeldungen"
    assert f"2 Änderungen übernommen – Version #{version['id']}." in client.get(r.headers["location"]).text
    assert db.get_rule(rule)["status"] == "merged" and db.count_unprocessed_feedback() == 0
    page = client.get(f"/settings/vorschlag/{pid}").text  # read-only now
    assert "Übernommen am" in page and 'name="edit"' not in page and "nicht übernommen" in page
    r = client.post(f"/settings/vorschlag/{pid}/apply", data={"edit": ["1"]})
    assert r.status_code == 409 and "schon entschieden" in r.text


def test_apply_reports_edits_that_no_longer_fit(client, monkeypatch):
    login(client)
    pid, rule, critique = proposal_fixture()
    real = learning.apply_rewrite

    def apply(proposal_id, ids):
        result = real(proposal_id, ids)
        return {**result, "failed": [3]}

    monkeypatch.setattr(learning, "apply_rewrite", apply)
    r = client.post(f"/settings/vorschlag/{pid}/apply", data={"edit": ["1", "3"]})
    assert r.headers["location"] == f"/settings?applied={pid}&failed=1"
    assert "1 Änderung passte nicht mehr." in client.get(r.headers["location"]).text


def test_reject_proposal(client):
    login(client)
    pid, rule, critique = proposal_fixture()
    before = db.get_settings()
    r = client.post(f"/settings/vorschlag/{pid}/reject")
    assert r.status_code == 303 and r.headers["location"] == f"/settings?rejected={pid}"
    assert "Vorschlag verworfen. Die Rückmeldungen bleiben" in client.get(r.headers["location"]).text
    assert db.get_settings() == before and db.get_rewrite_proposal(pid)["status"] == "rejected"
    assert db.count_unprocessed_feedback() == 2
    assert "Verworfen am" in client.get(f"/settings/vorschlag/{pid}").text


def test_stale_proposal_is_explained(client):
    login(client)
    pid, rule, critique = proposal_fixture()
    save_profile(client, style="Ganz anders.")
    page = client.get(f"/settings/vorschlag/{pid}").text
    assert "Veraltet:" in page and 'action="/settings/rewrite"' in page and "Neu erstellen" in page
    assert 'name="edit"' not in page
    card = client.get("/settings").text.split('<article id="verbessern"')[1].split("</article>")[0]
    assert f"Vorschlag #{pid} passt nicht mehr" in card and "Neu erstellen" in card
    r = client.post(f"/settings/vorschlag/{pid}/apply", data={"edit": ["1"]})
    assert r.status_code == 409 and "bitte neu vorschlagen" in r.text
    assert db.get_settings()["style"] == "Ganz anders."


def test_proposal_without_edits_can_be_marked_processed(client):
    login(client)
    lid = add_listing()
    feedback = [db.add_feedback(lid, "telegram", "quick", "shorter") for _ in range(3)]
    pid = make_proposal(feedback_ids=feedback)
    page = client.get(f"/settings/vorschlag/{pid}").text
    assert "Keine Änderung vorgeschlagen" in page and "Als verarbeitet markieren" in page
    r = client.post(f"/settings/vorschlag/{pid}/apply")
    assert "Nichts übernommen. Die Rückmeldungen gelten als verarbeitet." in client.get(r.headers["location"]).text
    assert db.count_unprocessed_feedback() == 0


# --- Verhalten → Lernen ---------------------------------------------------------------------------------

def test_learning_switches_saved_on_verhalten(client):
    login(client)
    page = client.get("/settings/verhalten").text
    assert 'name="use_examples" value="1" checked' in page and 'name="use_rules" value="1" checked' in page
    r = client.post("/settings", data={"section": "verhalten", "followup_delay_s": "4"})  # both switched off
    assert r.headers["location"] == "/settings/verhalten?saved=1"
    assert (db.get_settings()["use_examples"], db.get_settings()["use_rules"]) == ("0", "0")
    page = client.get("/settings/verhalten").text
    assert "Regeln werden gerade nicht genutzt" in page and "Beispiele werden gerade nicht genutzt" in page
    r = client.post("/settings", data={"section": "profil", "profile": "x", "target_words": "150"})  # other tab
    assert (db.get_settings()["use_examples"], db.get_settings()["use_rules"]) == ("0", "0")


def rules_fixture():
    lid = add_listing(title="Altbau")
    session = done_session(lid)
    active = db.add_rule("Formuliere den Einzug als „ab sofort“.", status="active", session_id=session["id"],
                         reason="Steht so in der Endversion.")
    off = db.add_rule("Keine Ausrufezeichen.", status="inactive")
    new = db.add_rule("Nenne keine Zahlen.", status="proposed", session_id=session["id"], reason="Zahlen gestrichen.")
    update = db.add_rule("Einzug immer „ab sofort“, nie ein Datum.", status="proposed", action="update",
                         replaces_rule_id=active, session_id=session["id"])
    rejected = db.add_rule("Schreib per Du.", status="rejected")
    merged = db.add_rule("Halte dich kurz.", status="active")
    db.merge_rules([merged], db.latest_version()["id"])
    return {"lid": lid, "active": active, "off": off, "new": new, "update": update, "rejected": rejected,
            "merged": merged}


def test_rules_list_on_verhalten(client):
    login(client)
    ids = rules_fixture()
    page = client.get("/settings/verhalten").text
    card = page.split('<article id="regeln"')[1].split('<article id="beispiele"')[0]
    assert "1 von 25 aktiv" in card
    proposed = card.split("Warten auf deine Entscheidung")[1].split('<ul class="rule-list">')[1].split("</ul>")[0]
    assert f'action="/settings/rules/{ids["new"]}/decide"' in proposed and "Neue Regel" in proposed
    assert "Regel ändern – bisher: <span class=\"old\">„Formuliere den Einzug als „ab sofort“.“</span>" in proposed
    assert '<span class="md-label">Warum:</span> <p>Zahlen gestrichen.</p>' in proposed  # reason via the md filter
    assert f'href="/listing/{ids["lid"]}">„Altbau“</a>' in proposed
    in_use = re.findall(r'action="/settings/rules/(\d+)/active">\s*<label><input type="checkbox" role="switch" name="active" '
                        r'value="1" data-autosubmit\s*(checked)?', card)
    assert in_use == [(str(ids["active"]), "checked"), (str(ids["off"]), "")]
    assert "wird genutzt</label>" in card and "abgeschaltet</label>" in card  # the switch says its state
    assert "nojs-only" in card  # fallback button without JS
    assert '<h3 class="rule-list-title">Deine Regeln</h3>' in card  # set apart from the open proposals
    assert f'formaction="/settings/rules/{ids["active"]}/delete"' in card
    history = card.split("Verlauf")[1]
    assert "Schreib per Du." in history and "verworfen" in history and "ins Profil übernommen" in history
    assert 'href="/settings/version/1">Version #1</a>' in history
    lernen = page.split('<article id="lernen"')[1].split("</article>")[0]
    assert "1 aktive Regel" in lernen and "2 warten auf dich" in lernen and "1 Beispiel" in lernen


def test_rule_actions(client):
    login(client)
    ids = rules_fixture()
    r = client.post(f"/settings/rules/{ids['active']}/active")  # unticked checkbox: nothing sent
    assert r.status_code == 303 and r.headers["location"] == "/settings/verhalten?done=rule_off#regeln"
    assert db.get_rule(ids["active"])["status"] == "inactive"
    assert "Regel abgeschaltet – sie bleibt gespeichert." in client.get("/settings/verhalten?done=rule_off").text
    client.post(f"/settings/rules/{ids['active']}/active", data={"active": "1"})
    assert db.get_rule(ids["active"])["status"] == "active"
    r = client.post(f"/settings/rules/{ids['off']}/text", data={"text": "  Keine Ausrufezeichen,\nnie.  "})
    assert r.headers["location"].endswith("done=rule_saved#regeln")
    assert db.get_rule(ids["off"])["text"] == "Keine Ausrufezeichen, nie."
    r = client.post(f"/settings/rules/{ids['off']}/text", data={"text": "  "})
    assert r.status_code == 409 and "Die Regel ist leer." in r.text
    r = client.post(f"/settings/rules/{ids['new']}/decide", data={"accept": "1", "text": "Nenne keine Beträge."})
    assert r.headers["location"].endswith("done=rule_accepted#regeln")
    rule = db.get_rule(ids["new"])
    assert (rule["status"], rule["text"], rule["proposed_text"]) == ("active", "Nenne keine Beträge.", "Nenne keine Zahlen.")
    r = client.post(f"/settings/rules/{ids['update']}/decide", data={"accept": "0", "text": "ignoriert"})
    assert r.headers["location"].endswith("done=rule_rejected#regeln")
    assert db.get_rule(ids["update"])["status"] == "rejected" and db.get_rule(ids["update"])["text"].startswith("Einzug")
    assert client.post(f"/settings/rules/{ids['update']}/decide", data={"accept": "1"}).status_code == 409  # decided
    r = client.post(f"/settings/rules/{ids['rejected']}/delete")
    assert r.headers["location"].endswith("done=rule_deleted#regeln") and db.get_rule(ids["rejected"]) is None
    assert client.post("/settings/rules/999/active", data={"active": "1"}).status_code == 409
    assert client.post(f"/settings/rules/{ids['active']}/active", data={"active": "1"},
                       headers={"Origin": "https://evil.example"}).status_code == 403


def test_rule_limit_and_merge_hint(client, monkeypatch):
    login(client)
    for i in range(learning.RULES_MERGE_HINT_AT):
        db.add_rule(f"Regel {i}.", status="active")
    card = client.get("/settings/verhalten").text.split('<article id="regeln"')[1]
    assert "Schon 15 aktive Regeln" in card and 'href="/settings#verbessern"' in card
    profil = client.get("/settings").text
    assert "Schon 15 aktive Regeln – ein Vorschlag kann sie ins Profil einarbeiten." in profil
    monkeypatch.setattr(learning, "MAX_ACTIVE_RULES", 15)
    off = db.add_rule("Noch eine.", status="inactive")
    card = client.get("/settings/verhalten").text.split('<article id="regeln"')[1]
    assert "Höchstgrenze erreicht:" in card and "15 von 15 aktiv" in card
    r = client.post(f"/settings/rules/{off}/active", data={"active": "1"})
    assert r.status_code == 409 and "Höchstens 15 aktive Regeln" in r.text


def test_examples_listed_and_removed(client):
    login(client)
    assert "Noch keine. Sobald du im 📝 Editor auf „Fertig“ drückst" in client.get("/settings/verhalten").text
    sessions = []
    for n in range(4):
        lid = add_listing(fredy_id=f"e{n}", title=f"Wohnung {n}", address=f"Straße {n}")
        sessions.append(done_session(lid, final=f"Endversion {n}"))
    card = client.get("/settings/verhalten").text.split('<article id="beispiele"')[1]
    assert "3 von 3" in card and "1 weitere Endversion rückt nach" in card
    assert [int(i) for i in re.findall(r'action="/settings/examples/(\d+)/off"', card)] == [s["id"] for s in sessions[:0:-1]]
    assert f'href="/listing/{sessions[3]["listing_id"]}">Wohnung 3</a>' in card and "Endversion 3" in card
    assert "Wohnung 0" not in card  # the oldest isn't used
    assert "Nicht mehr als Beispiel genutzt" not in card
    sid = sessions[3]["id"]
    r = client.post(f"/settings/examples/{sid}/off")
    assert r.status_code == 303 and r.headers["location"] == f"/settings/verhalten?done=example_off&undo={sid}#beispiele"
    assert db.get_session(sid)["use_as_example"] == 0
    card = client.get(r.headers["location"]).text.split('<article id="beispiele"')[1]
    used = card.split("Nicht mehr als Beispiel genutzt")[0]
    assert "Wird nicht mehr als Beispiel genutzt." in card and "Wohnung 0" in used and "Wohnung 3" not in used
    assert f'action="/settings/examples/{sid}/on" class="flash-dismiss"' in card and "Rückgängig" in card
    taken_out = card.split("Nicht mehr als Beispiel genutzt")[1]  # reversible later too
    assert "Wohnung 3" in taken_out and f'action="/settings/examples/{sid}/on"' in taken_out
    assert "Rückgängig" not in client.get("/settings/verhalten?done=rule_off&undo=3").text  # only after example_off
    r = client.post(f"/settings/examples/{sid}/on")
    assert r.status_code == 303 and r.headers["location"] == "/settings/verhalten?done=example_on#beispiele"
    assert db.get_session(sid)["use_as_example"] == 1
    card = client.get(r.headers["location"]).text.split('<article id="beispiele"')[1]
    assert "Wird wieder als Beispiel genutzt." in card and "Nicht mehr als Beispiel genutzt" not in card
    assert "Wohnung 3" in card and "Wohnung 0" not in card
    assert client.post("/settings/examples/999/off").status_code == 409
    assert client.post("/settings/examples/999/on").status_code == 409
    assert client.post(f"/settings/examples/{sid}/on", headers={"Origin": "https://evil.example"}).status_code == 403


# --- Zugänge: editor login from Telegram ---------------------------------------------------------------

def test_setdomain_hint_with_the_dashboard_domain(client):
    login(client)
    page = client.get("/settings/zugaenge").text
    hint = page.split('<article id="editor-login"')[1].split("</article>")[0]
    assert "<code>/setdomain</code>" in hint and "@BotFather" in hint
    assert 'id="editor-domain"' not in hint and "<code>immobot.example.com</code> (noch unbekannt" in hint
    assert 'href="#telegram_allowed_user_ids"' in hint
    client.get("/settings/zugaenge", headers={"host": "immobot.example.com"})  # the public address is remembered
    hint = client.get("/settings/zugaenge").text.split('<article id="editor-login"')[1]
    assert '<code id="editor-domain">immobot.example.com</code>' in hint and "noch unbekannt" not in hint


@pytest.mark.parametrize("stored,text,when", [
    ("2026-10-13T07:12:00+00:00 Bad Request: BOT_DOMAIN_INVALID", "Bad Request: BOT_DOMAIN_INVALID", "13.10.2026 09:12"),
    # bot.py's own wording: the page says "abgelehnt" already, so only the reason is shown
    ("2026-10-13T07:12:00+00:00 Telegram lehnt den Editor-Button ab: Domain nicht mit dem Bot verknüpft "
     "(@BotFather → /setdomain).", "(zuletzt am 13.10.2026 09:12): Domain nicht mit dem Bot verknüpft",
     "13.10.2026 09:12"),
    ("2026-10-13T07:12:00+00:00 Telegram lehnt den Editor-Button ab (Bad Request: URL host is empty).",
     ": Bad Request: URL host is empty", "13.10.2026 09:12"),
    ("Bad Request: BOT_DOMAIN_INVALID 2026-10-13T07:12:00+00:00", "Bad Request: BOT_DOMAIN_INVALID", "13.10.2026 09:12"),
    ('{"error": "Button abgelehnt", "at": "2026-10-13T07:12:00+00:00"}', "Button abgelehnt", "13.10.2026 09:12"),
    ("Nur Text", "Nur Text", None),
])
def test_editor_login_error_warning(client, stored, text, when):
    login(client)
    assert "abgelehnt" not in client.get("/settings/zugaenge").text and "Editor-Button" not in client.get("/status").text
    db.set_meta(web.EDITOR_LOGIN_ERROR_META, stored)
    zugaenge = client.get("/settings/zugaenge").text.split('<article id="editor-login"')[1].split("</article>")[0]
    status = client.get("/status").text
    for page in (zugaenge, status):
        assert text.split(": ", 1)[-1] in page and (when is None or when in page)
        assert "lehnt den Editor-Button ab" not in page
    assert text in zugaenge
    assert "Telegram hat den „📝 Bearbeiten“-Button abgelehnt" in zugaenge and "/setdomain" in zugaenge
    assert "📝 Editor-Button" in status and 'href="/settings/zugaenge#editor-login"' in status


# --- costs ---------------------------------------------------------------------------------------------

def test_learning_costs_count_in_the_totals(client):
    login(client)
    lid = add_listing()
    db.add_draft(lid, "initial", "A", "claude-opus-5-5", cost_usd=1.1)  # 1,00 € at 1,10
    db.add_llm_usage("rewrite", "anthropic", "claude-opus-5-5", cost_usd=2.2)  # 2,00 €
    db.add_llm_usage("rules", "deutschlandgpt", "claude-sonnet-5.5", listing_id=lid, cost_usd=0.55, currency="EUR",
                     cost_native=0.5)  # 0,50 €
    for path in ["/", "/status"]:
        assert client.get(path).text.count(">3,50 €</span>") == 2, path  # this month and in total
