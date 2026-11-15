"""The editor's Claude actions in the background (drafting.start_editor_job): "Überarbeiten lassen" and the quick
buttons with a progress bar, and "Nur fragen" (a question about the ad or the letter that changes nothing)."""

import asyncio
import re
import time
from types import SimpleNamespace

import pytest

from app import db, drafting, editor, learning, llm

from .test_editor import client, editor_cookie, lid, login, open_editor, post  # noqa: F401 (fixtures)

JSON = {"Accept": "application/json"}


@pytest.fixture
def slow_generate(monkeypatch):
    """llm.generate_draft replaced: waits calls["delay"] s, raises calls["error"] or returns a numbered version."""
    calls = {"list": [], "delay": 0.0, "error": None}

    async def generate(listing, settings, kind, previous_text=None, instruction=None, pictures=None, rules=None,
                       examples=None):
        calls["list"].append({"kind": kind, "previous_text": previous_text, "instruction": instruction})
        await asyncio.sleep(calls["delay"])
        if calls["error"] is not None:
            raise calls["error"]
        return llm.DraftResult(text=f"Fassung von Claude {len(calls['list'])}", hint=None, model="claude-opus-5-5",
                               input_tokens=100, output_tokens=50, cache_read_tokens=0, cache_write_tokens=0,
                               cost_usd=0.011)

    monkeypatch.setattr(llm, "generate_draft", generate)
    return calls


@pytest.fixture
def fake_answer(monkeypatch):
    """llm.answer_question replaced: records the question and the letter, answers after calls["delay"] s."""
    calls = {"list": [], "delay": 0.0, "answer": "Steht nicht im Inserat – frag über das **Kontaktformular**."}

    async def answer(listing, settings, question, letter):
        calls["list"].append((question, letter))
        await asyncio.sleep(calls["delay"])
        db.add_llm_usage("question", "anthropic", "claude-opus-5-5", listing_id=listing["id"], cost_usd=0.002)
        return calls["answer"]

    monkeypatch.setattr(llm, "answer_question", answer)
    return calls


def ready(client, listing_id):
    db.add_draft(listing_id, "initial", "Guten Tag, Entwurf", "claude-opus-5-5")
    editor_cookie(client, listing_id)
    return open_editor(client, listing_id)


def job_status(client, listing_id) -> dict:
    r = client.get(f"/editor/{listing_id}/job/status")
    assert r.status_code == 200
    return r.json()


def wait_for_job(client, listing_id, timeout=5.0) -> dict:
    end = time.monotonic() + timeout
    while True:
        status = job_status(client, listing_id)
        if status["state"] != "running" or time.monotonic() > end:
            return status
        time.sleep(0.02)


# --- revisions in the background ----------------------------------------------------------------------------

def test_revision_runs_in_the_background_with_a_bar(client, lid, slow_generate):
    r, fields = ready(client, lid)
    assert 'data-job="custom"' in r.text and 'data-editor-job="idle"' in r.text and "data-revising-badge hidden" in r.text
    slow_generate["delay"] = 0.3
    r = client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, von Hand", "critique": "kürzer"},
                    headers=JSON)
    assert r.status_code == 200
    started = r.json()
    assert started["state"] == "running" and started["kind"] == "custom"
    assert started["expected_ms"] == drafting.DEFAULT_REVISION_MS and started["slow_after_ms"] == drafting.SLOW_AFTER_MS
    assert db.latest_session(lid)["current_text"] == "Guten Tag, von Hand"  # the hand edits are saved at once

    page = client.get(f"/editor/{lid}").text  # left and came back: the bar, the letter read-only
    assert 'data-editor-job="running"' in page and 'data-job-kind="custom"' in page
    assert re.search(r'<textarea id="ed-text"[^>]* readonly', page)
    assert "data-revising-badge>" in page  # not hidden
    again = client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, von Hand", "critique": "x"},
                        headers=JSON)
    assert again.json()["state"] == "running" and len(slow_generate["list"]) == 1  # a second tap: the same job

    done = wait_for_job(client, lid)
    assert done["state"] == "done" and done["done_url"] == f"/editor/{lid}?m=revised#text" and done["answer_html"] is None
    assert job_status(client, lid)["state"] == "idle"  # reported once
    session = db.latest_session(lid)
    assert session["current_text"] == "Fassung von Claude 1"
    assert [f["text"] for f in db.list_feedback(session_id=session["id"])] == ["kürzer"]  # learning sees it as before
    assert slow_generate["list"][0]["previous_text"] == "Guten Tag, von Hand"
    assert db.latest_draft(lid)["duration_ms"] is not None and db.latest_draft(lid)["source"] == "editor"
    page = client.get(f"/editor/{lid}?m=revised").text
    assert "Claudes Überarbeitung" in page and not re.search(r'<textarea id="ed-text"[^>]* readonly', page)


def test_quick_button_runs_in_the_background(client, lid, slow_generate):
    r, fields = ready(client, lid)
    r = client.post(f"/editor/{lid}/quick", data={**fields, "text": "Guten Tag, Entwurf", "kind": "shorter"},
                    headers=JSON)
    assert r.json()["kind"] == "shorter"
    done = wait_for_job(client, lid)
    assert done["state"] == "done" and done["done_url"] == f"/editor/{lid}?m=shorter#text"
    assert slow_generate["list"][0]["kind"] == "shorter"
    bad = client.post(f"/editor/{lid}/quick", data={**fields, "text": "x", "kind": "nonsense"}, headers=JSON)
    assert bad.status_code == 400 and bad.json() == {"state": "error", "error": "Unbekannte Aktion."}


def test_revision_errors_come_back_with_the_critique(client, lid, slow_generate):
    r, fields = ready(client, lid)
    empty = client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, Entwurf", "critique": " "},
                        headers=JSON)
    assert empty.status_code == 400 and empty.json()["error"] == "Bitte schreiben, was Claude ändern soll."
    slow_generate["error"] = llm.LLMError("Anthropic ist gerade überlastet.")
    client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, Entwurf", "critique": "netter"},
                headers=JSON)
    failed = wait_for_job(client, lid)
    assert (failed["state"], failed["error"], failed["done_url"]) == ("error", "Anthropic ist gerade überlastet.", None)
    # the page came back before the status was read: the error in the panel, the critique back in its field
    slow_generate["error"] = llm.LLMError("Wieder kaputt.")
    client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, Entwurf", "critique": "netter"},
                headers=JSON)
    while drafting.editor_running(lid):
        time.sleep(0.02)
    page = client.get(f"/editor/{lid}").text
    assert 'data-editor-job="error"' in page and "Wieder kaputt." in page and "Claude konnte nicht überarbeiten" in page
    assert ">netter</textarea>" in page
    assert 'data-editor-job="idle"' in client.get(f"/editor/{lid}").text  # shown once
    assert db.latest_session(lid)["current_text"] == "Guten Tag, Entwurf"


def test_revision_refused_before_the_bar(client, lid, slow_generate, monkeypatch):
    r, fields = ready(client, lid)
    monkeypatch.setattr(learning, "EDITOR_DAILY_LIMIT", 0)
    r = client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag", "critique": "kürzer"}, headers=JSON)
    assert r.status_code == 400 and r.json()["error"].startswith("Tageslimit erreicht")
    assert slow_generate["list"] == [] and job_status(client, lid)["state"] == "idle"
    stale = client.post(f"/editor/{lid}/revise", data={**fields, "session_id": "999", "text": "x", "critique": "y"},
                        headers=JSON)
    assert stale.status_code == 409 and stale.json()["state"] == "stale"
    client.cookies.clear()
    denied = client.post(f"/editor/{lid}/revise", data={**fields, "text": "x", "critique": "y"}, headers=JSON)
    assert denied.json()["state"] == "denied"
    assert client.get(f"/editor/{lid}/job/status").status_code == 403


def test_no_js_revision_still_waits_for_claude(client, lid, slow_generate):
    r, fields = ready(client, lid)
    r = post(client, lid, "revise", fields, text="Guten Tag, Entwurf", critique="kürzer")
    assert r.status_code == 303 and r.headers["location"] == f"/editor/{lid}?m=revised#text"
    assert db.latest_session(lid)["current_text"] == "Fassung von Claude 1"


def test_expected_revision_time_from_the_editors_revisions(fresh_db):
    listing_id = db.insert_listing({"fredy_id": "x1", "title": "A"})[0]
    assert drafting.editor_expected_ms("custom") == drafting.DEFAULT_REVISION_MS
    for ms, kind, source in ((20_000, "custom", "editor"), (26_000, "shorter", "editor"), (23_000, "formal", "editor"),
                             (90_000, "initial", "editor"), (90_000, "custom", "telegram")):
        db.add_draft(listing_id, kind, "x", "m", source=source, duration_ms=ms)
    assert drafting.editor_expected_ms("custom") == 23_000  # only the editor's revisions
    assert db.recent_draft_durations(("custom", "shorter"), source="editor") == [26_000, 20_000]
    assert drafting.editor_expected_ms("ask") == drafting.DEFAULT_ASK_MS


# --- "Nur fragen" -------------------------------------------------------------------------------------------

def test_question_answers_without_touching_the_letter(client, lid, fake_answer):
    r, fields = ready(client, lid)
    assert 'formaction="/editor/%d/ask"' % lid in r.text and ">Nur fragen</button>" in r.text
    assert "Ändern lassen oder fragen" in r.text and "data-answer" in r.text
    fake_answer["delay"] = 0.2
    last_draft = db.latest_session(lid)["last_draft_id"]
    r = client.post(f"/editor/{lid}/ask", data={**fields, "text": "Guten Tag, von Hand", "critique": "Wo ist die Mail?"},
                    headers=JSON)
    assert r.json()["state"] == "running" and r.json()["kind"] == "ask"
    page = client.get(f"/editor/{lid}").text
    assert "Claude beantwortet deine Frage" in page and not re.search(r'<textarea id="ed-text"[^>]* readonly', page)
    done = wait_for_job(client, lid)
    assert done["state"] == "done" and done["done_url"] is None
    assert done["answer_html"] == "<p>Steht nicht im Inserat – frag über das <strong>Kontaktformular</strong>.</p>"
    assert fake_answer["list"] == [("Wo ist die Mail?", "Guten Tag, von Hand")]
    session = db.latest_session(lid)
    assert session["current_text"] == "Guten Tag, von Hand" and session["last_draft_id"] == last_draft  # no revision
    assert db.list_feedback(session_id=session["id"]) == [] and len(db.list_drafts(lid)) == 1  # nothing to learn
    assert learning.pending_review(session) is None
    assert learning.editor_calls_today(lid) == 1  # but it counts toward the daily limit
    assert len(drafting._ask_durations) == 1


def test_question_answer_shown_when_coming_back(client, lid, fake_answer):
    r, fields = ready(client, lid)
    client.post(f"/editor/{lid}/ask", data={**fields, "text": "Guten Tag, Entwurf", "critique": "Ab wann frei?"},
                headers=JSON)
    while drafting.editor_running(lid):
        time.sleep(0.02)
    page = client.get(f"/editor/{lid}").text  # left the page while Claude answered
    answer = page.split('class="ed-card ed-answer"')[1].split("</article>")[0]
    assert "hidden" not in answer.split(">")[0] and "Ab wann frei?" in answer and "<strong>Kontaktformular</strong>" in answer
    assert '<textarea id="ed-critique" name="critique" rows="2" placeholder' in page and "Ab wann frei?</textarea>" not in page
    assert 'class="ed-card ed-answer" data-answer aria-labelledby="ed-answer-title" aria-live="polite" hidden' in \
        client.get(f"/editor/{lid}").text  # until the next reload


def test_question_without_js_and_its_limits(client, lid, fake_answer, monkeypatch):
    r, fields = ready(client, lid)
    r = post(client, lid, "ask", fields, text="Guten Tag, Entwurf", critique="Gibt es einen Balkon?")
    assert r.status_code == 200 and "Gibt es einen Balkon?" in r.text and "Kontaktformular" in r.text
    assert "Gibt es einen Balkon?</textarea>" not in r.text
    r = post(client, lid, "ask", fields, text="Guten Tag, Entwurf", critique="  ")
    assert r.status_code == 400 and "Bitte schreiben, was du wissen willst." in r.text
    empty = client.post(f"/editor/{lid}/ask", data={**fields, "text": "x", "critique": ""}, headers=JSON)
    assert empty.status_code == 400 and empty.json()["error"] == "Bitte schreiben, was du wissen willst."
    monkeypatch.setattr(learning, "EDITOR_DAILY_LIMIT", 1)  # the one question above used it up
    r = client.post(f"/editor/{lid}/ask", data={**fields, "text": "x", "critique": "Noch was?"}, headers=JSON)
    assert r.status_code == 400 and r.json()["error"].startswith("Tageslimit erreicht")
    assert len(fake_answer["list"]) == 1


def test_question_prompt_and_call(fresh_db, monkeypatch):
    listing_id = db.insert_listing({"fredy_id": "q1", "provider": "immoscout", "title": "2 Zimmer", "price": "800 €",
                                    "address": "50735 Köln", "description": "Ruhige Lage",
                                    "url": "https://portal.example/expose/7"})[0]
    db.set_listing_details(listing_id, "Etage: 2", "Frau Muster")
    listing = db.get_listing(listing_id)
    settings = {**db.get_settings(), "profile": "Name: Alex"}
    text = llm.build_question_text(listing, settings, " Wo ist die Mail? ", "Guten Tag, Frau Muster")
    assert "<inserat>" in text and "Ansprechperson: Frau Muster" in text and "Link: https://portal.example/expose/7" in text
    assert "<profil>\nName: Alex\n</profil>" in text and "<anschreiben>\nGuten Tag, Frau Muster\n</anschreiben>" in text
    assert text.endswith("<frage>\nWo ist die Mail?\n</frage>")
    assert "Nenn nie Kontaktdaten" in llm.QUESTION_SYSTEM and "Kontaktformular" in llm.QUESTION_SYSTEM
    assert "Rate nicht und erfinde nichts" in llm.QUESTION_SYSTEM

    sent = []

    async def send(client, provider, model, params):
        sent.append(params)
        return SimpleNamespace(model=model, stop_reason="end_turn", content=[
            SimpleNamespace(type="thinking", text=""), SimpleNamespace(type="text", text=" Steht nicht drin. ")],
            usage=SimpleNamespace(input_tokens=1000, output_tokens=20, cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0))

    monkeypatch.setattr(llm, "_get_client", lambda provider: object())
    monkeypatch.setattr(llm, "send", send)
    answer = asyncio.run(llm.answer_question(listing, settings, "Wo ist die Mail?", "Guten Tag"))
    assert answer == "Steht nicht drin."
    assert sent[0]["system"] == llm.QUESTION_SYSTEM and "output_config" in sent[0] and "format" not in sent[0]["output_config"]
    usage = db.list_llm_usage()
    assert [(u["purpose"], u["listing_id"]) for u in usage] == [("question", listing_id)]
    assert db.list_feedback() == [] and db.list_drafts(listing_id) == []



# --- a restart of the server loses the jobs: editor.js starts them again (boot, revision_mark) ---------------------

def test_status_says_which_server_answers(client, lid, slow_generate, monkeypatch):
    editor_cookie(client, lid)
    page = client.get(f"/editor/{lid}").text  # no draft yet: the first draft's bar
    assert f'data-boot="{drafting.BOOT}"' in page
    status = client.get(f"/editor/{lid}/draft/status").json()
    assert status["state"] == "idle" and status["boot"] == drafting.BOOT
    r, fields = ready(client, lid)
    assert f'data-boot="{drafting.BOOT}" data-revision-mark="0"' in r.text
    idle = job_status(client, lid)
    assert (idle["state"], idle["boot"], idle["revision_mark"]) == ("idle", drafting.BOOT, 0)
    monkeypatch.setattr(drafting, "BOOT", "anderer-start")  # as after a restart
    client.post(f"/editor/{lid}/revise", data={**fields, "text": "Guten Tag, Entwurf", "critique": "kürzer"},
                headers=JSON)
    done = wait_for_job(client, lid)
    mark = done["revision_mark"]
    assert done["boot"] == "anderer-start" and mark > 0  # changed: a lost revision that had finished isn't sent again
    assert job_status(client, lid)["revision_mark"] == mark
    assert f'data-revision-mark="{mark}"' in client.get(f"/editor/{lid}").text
    assert editor.revision_mark(9999) == 0
