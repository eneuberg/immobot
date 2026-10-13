"""Learning (learning.py), profile versions, feedback capture, text diffs and the prompt's learned parts."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import db, learning, llm, media, textdiff, web
from app.settings import DEFAULT_SETTINGS, PROFILE_FIELDS


LISTING = {"fredy_id": "L1", "provider": "immoscout", "title": "Helle 2-Zimmer-Wohnung", "price": "780 €",
           "size": "54 m²", "address": "Musterstr. 12, Berlin", "url": "https://portal.example/expose/1"}


# --- fakes ---------------------------------------------------------------------------

def fake_response(payload=None, *, text=None, model="claude-opus-5-5", stop_reason="end_turn", input_tokens=2000,
                  output_tokens=500):
    return SimpleNamespace(
        model=model, stop_reason=stop_reason, stop_details=None,
        content=[SimpleNamespace(type="thinking", thinking=""),
                 SimpleNamespace(type="text", text=json.dumps(payload) if text is None else text)],
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
    )


class FakeMessages:
    def __init__(self, client):
        self.client, self.calls = client, []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        self.client.calls.append(kwargs)
        return self.client.responses.pop(0)


class FakeClient:
    """Answers with the queued responses; records every request (beta and plain) in `calls`."""

    def __init__(self):
        self.responses, self.calls, self.timeouts = [], [], []
        self.messages = FakeMessages(self)
        self.beta = SimpleNamespace(messages=FakeMessages(self))

    def with_options(self, timeout=None, **kwargs):
        self.timeouts.append(timeout)
        return self


@pytest.fixture
def client(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(llm, "_get_client", lambda provider: fake)
    return fake


@pytest.fixture
def lid(fresh_vault, monkeypatch):
    async def no_media(listing_id, retry=False, want_images=True):
        return []

    monkeypatch.setattr(media, "ensure_media", no_media)
    monkeypatch.setattr(learning, "_busy_sessions", set())
    monkeypatch.setattr(learning, "_rewrite_running", False)
    return db.insert_listing(LISTING)[0]


@pytest.fixture
def fake_generate(monkeypatch):
    calls = []

    async def generate(listing, settings, kind, previous_text=None, instruction=None, pictures=None, rules=None,
                       examples=None):
        calls.append({"kind": kind, "previous_text": previous_text, "instruction": instruction, "rules": rules,
                      "examples": examples})
        return llm.DraftResult(text=f"Fassung {len(calls)}", hint=None, model="claude-opus-5-5", input_tokens=100,
                               output_tokens=50, cache_read_tokens=0, cache_write_tokens=0, cost_usd=0.011)

    monkeypatch.setattr(llm, "generate_draft", generate)
    return calls


def done_session(listing_id, original="Guten Tag. Ich bin Erika.", final="Hallo. Ich bin Erika, 34."):
    draft_id = db.add_draft(listing_id, "initial", original, "claude-opus-5-5")
    session = learning.open_or_create_session(listing_id)
    assert session["base_draft_id"] == draft_id
    return learning.finish(session["id"], final)


# --- text diff ---------------------------------------------------------------------------

def sides(segments):
    old = "".join(text for op, text in segments if op != "insert")
    new = "".join(text for op, text in segments if op != "delete")
    return old, new


@pytest.mark.parametrize("old, new", [
    ("Guten Tag, Frau X.\nIch bin Erika.", "Hallo Frau X!\nIch bin Erika, 34."),
    ("a b", "x y"),
    ("x b", " b"),
    ("", "neu"),
    ("alt", ""),
    ("Zeile 1\n\n  eingerückt\tTab", "Zeile 1\n\n  eingerückt\tTab und mehr"),
    ("gleich", "gleich"),
])
def test_word_diff_keeps_every_character(old, new):
    segments = textdiff.word_diff(old, new)
    assert sides(segments) == (old, new)
    assert all(op in ("equal", "insert", "delete") for op, _ in segments)
    assert textdiff.has_changes(segments) == (old != new)


def test_word_diff_joins_neighbouring_changes_but_keeps_line_breaks():
    assert textdiff.word_diff("a b c", "x y c") == [("delete", "a b"), ("insert", "x y"), ("equal", " c")]
    segments = textdiff.word_diff("eins\nzwei", "drei\nvier")
    assert ("equal", "\n") in segments  # the line break is not swallowed into the changes
    assert textdiff.word_diff("Erika.", "Erika, 34.") == [("equal", "Erika"), ("insert", ", 34"), ("equal", ".")]


def test_sentence_diff_shows_only_changed_sentences():
    old = "Guten Tag. Ich bin Erika. Ich arbeite als Ärztin.\nViele Grüße"
    new = "Guten Tag. Ich bin Erika, 34. Ich arbeite als Ärztin.\nViele Grüße"
    assert textdiff.sentence_diff(old, new) == "- Ich bin Erika.\n+ Ich bin Erika, 34."
    assert textdiff.sentence_diff(old, old) == ""
    assert textdiff.sentences("Er sagt „Hallo.“ Dann geht er.") == ["Er sagt „Hallo.“", "Dann geht er."]
    long = textdiff.sentence_diff("", "\n".join(f"Satz {i}." for i in range(100)), max_chars=40)
    assert long.endswith("…") and len(long) <= 42


def test_trim_equal_keeps_context_around_changes():
    segments = textdiff.word_diff("A" * 50 + " alt " + "B" * 50, "A" * 50 + " neu " + "B" * 50)
    trimmed = textdiff.trim_equal(segments, keep=10)
    assert trimmed[0] == ("equal", "…" + ("A" * 50 + " ")[-10:])
    assert trimmed[-1] == ("equal", (" " + "B" * 50)[:10] + "…")
    assert textdiff.trim_equal([("equal", "x" * 500)], keep=10) == [("equal", "x" * 500)]


def test_diff_macro_renders_escaped_ins_and_del():
    template = web.templates.env.from_string(
        '{% from "macros.html" import text_diff %}{{ text_diff(a|word_diff(b)|trim_diff, "klein") }}')
    html = template.render(a="Hallo <b>Welt</b>\nZeile", b="Hallo <i>Welt</i>\nZeile 2")
    assert html == ('<div class="diff klein">Hallo &lt;<del>b</del><ins>i</ins>&gt;Welt&lt;/<del>b</del><ins>i</ins>'
                    '&gt;\nZeile<ins> 2</ins></div>')


# --- profile versions ----------------------------------------------------------------------

def test_initial_version_and_noop_snapshots(fresh_db):
    (initial,) = db.list_versions()
    assert initial["source"] == "initial"
    assert initial["data"] == {key: DEFAULT_SETTINGS[key] for key in PROFILE_FIELDS}
    assert db.snapshot_profile("manual") is None  # nothing changed
    db.update_settings({"effort": "high"})  # not a profile field
    assert db.snapshot_profile("manual") is None
    db.update_settings({"style": "locker"})
    version_id = db.snapshot_profile("manual", "Notiz")
    assert db.get_version(version_id)["data"]["style"] == "locker" and db.get_version(version_id)["note"] == "Notiz"
    assert db.snapshot_profile("manual") is None
    fresh_db.init_db(str(fresh_db._db_path).rsplit("/", 1)[0])  # a restart adds no second "initial"
    assert [v["source"] for v in db.list_versions()] == ["manual", "initial"]


def test_restore_version(fresh_db):
    first = db.latest_version()["id"]
    db.update_settings({"profile": "Erika, 34", "target_words": "120"})
    second = db.snapshot_profile("manual")
    restored = db.restore_version(first)
    assert db.current_profile_values() == db.get_version(first)["data"]
    version = db.get_version(restored)
    assert (version["source"], version["note"]) == ("restore", f"aus Version #{first}")
    assert db.restore_version(first) is None  # already these values: no new version
    db.restore_version(second)
    assert db.current_profile_values()["profile"] == "Erika, 34"
    with pytest.raises(LookupError):
        db.restore_version(999)
    assert len(db.list_versions()) == 4  # versions are never deleted


# --- feedback capture --------------------------------------------------------------------------

def test_create_and_store_draft_records_feedback(lid, fake_generate):
    asyncio.run(llm.create_and_store_draft(lid, "initial", source="telegram"))
    asyncio.run(llm.create_and_store_draft(lid, "test", source="dashboard"))
    assert db.list_feedback() == []  # first drafts and test drafts are no feedback
    asyncio.run(llm.create_and_store_draft(lid, "shorter", source="telegram"))
    asyncio.run(llm.create_and_store_draft(lid, "regenerate", source="dashboard"))
    asyncio.run(llm.create_and_store_draft(lid, "custom", "  Erwähne mein Fahrrad  ", source="telegram"))
    asyncio.run(llm.create_and_store_draft(lid, "formal", source="editor", record_feedback=False))
    rows = [(f["source"], f["kind"], f["text"], f["before_text"], f["session_id"]) for f in reversed(db.list_feedback())]
    assert rows == [("telegram", "quick", "shorter", None, None), ("dashboard", "quick", "regenerate", None, None),
                    ("telegram", "critique", "Erwähne mein Fahrrad", None, None)]
    assert db.quick_feedback_counts() == {"shorter": 1, "regenerate": 1}
    with pytest.raises(ValueError):
        asyncio.run(llm.create_and_store_draft(lid, "shorter", source="sms"))


def test_failed_generation_records_no_feedback(lid, monkeypatch):
    async def fail(*args, **kwargs):
        raise llm.LLMError("kaputt")

    monkeypatch.setattr(llm, "generate_draft", fail)
    with pytest.raises(llm.LLMError):
        asyncio.run(llm.create_and_store_draft(lid, "shorter", source="telegram"))
    assert db.list_feedback() == []


# --- prompt assembly -----------------------------------------------------------------------------

def test_system_prompt_with_rules_and_examples():
    settings = {**DEFAULT_SETTINGS}
    examples = [{"final_text": "Hallo {Name},\nich bin Erika.", "listing_title": "Altbau", "listing_address": "Berlin"},
                {"final_text": "x" * 2000, "listing_title": None, "listing_address": None},
                {"final_text": "  ", "listing_title": "leer"}]
    prompt = llm.build_system_prompt(settings, ["Nenne kein  Datum.", " "], examples)
    assert "## Gelernte Vorlieben\n- Nenne kein Datum." in prompt
    assert prompt.index("## Schreibstil") < prompt.index("## Gelernte Vorlieben") < prompt.index("## Beispiele")
    assert "## Beispiele\n" + llm.EXAMPLES_INTRO in prompt
    assert "<beispiel>\nAltbau · Berlin\n\nHallo {Name},\nich bin Erika.\n</beispiel>" in prompt
    assert "<beispiel>\nInserat\n\n" + "x" * llm.EXAMPLE_MAX_CHARS + " …\n</beispiel>" in prompt
    assert prompt.count("<beispiel>") == 2  # an empty text is no example
    off = llm.build_system_prompt({**settings, "use_rules": "0", "use_examples": "0"}, ["Regel"], examples)
    assert "## Gelernte Vorlieben" not in off and "## Beispiele" not in off
    assert llm.build_system_prompt(settings) == off  # nothing learned yet: the prompt as before


def test_learned_context_reads_rules_and_examples(lid):
    db.add_rule("Regel A", status="active")
    db.add_rule("Regel B", status="inactive")
    db.add_rule("Regel C", status="proposed")
    first = done_session(lid, final="Erste Endfassung")
    other = db.insert_listing({**LISTING, "fredy_id": "L2", "title": "Zweite"})[0]
    second = done_session(other, final="Zweite Endfassung")
    rules, examples = llm.learned_context(db.get_settings())
    assert rules == ["Regel A"]
    assert [e["final_text"] for e in examples] == ["Zweite Endfassung", "Erste Endfassung"]
    assert examples[0]["listing_title"] == "Zweite"
    learning.set_session_example(second["id"], False)
    assert [e["id"] for e in llm.learned_context(db.get_settings())[1]] == [first["id"]]
    db.update_settings({"use_rules": "0", "use_examples": "0"})
    assert llm.learned_context(db.get_settings()) == ([], [])


def test_drafts_get_the_learned_prompt(lid, client):
    db.add_rule("Formuliere den Einzug als „ab sofort“.", status="active")
    done_session(lid, final="Meine Endfassung")
    client.responses.append(fake_response({"draft": "Hallo", "hint": ""}))
    asyncio.run(llm.create_and_store_draft(lid, "initial"))
    system = client.calls[0]["system"]
    assert "## Gelernte Vorlieben\n- Formuliere den Einzug als „ab sofort“." in system
    assert "Meine Endfassung" in system


# --- editor sessions ---------------------------------------------------------------------------

def test_session_lifecycle(lid):
    assert learning.open_or_create_session(lid) is None  # no draft yet
    db.add_draft(lid, "initial", "Erster Entwurf", "claude-opus-5-5")
    newest = db.add_draft(lid, "shorter", "Kurzer Entwurf", "claude-opus-5-5")
    db.add_draft(lid, "test", "Test", "claude-opus-5-5")  # test drafts are never the base
    session = learning.open_or_create_session(lid)
    assert (session["base_draft_id"], session["original_text"], session["current_text"]) == (newest, "Kurzer Entwurf",
                                                                                          "Kurzer Entwurf")
    assert session["status"] == "open" and session["use_as_example"] == 1
    assert session["profile_version_id"] == db.latest_version()["id"]
    assert learning.open_or_create_session(lid)["id"] == session["id"]  # continued, not a second one
    learning.save_text(session["id"], "Hand\r\nbearbeitet")
    assert db.get_session(session["id"])["current_text"] == "Hand\nbearbeitet"
    with pytest.raises(learning.LearningError, match="leer"):
        learning.finish(session["id"], "   ")
    done = learning.finish(session["id"], " Endfassung \n")
    assert (done["status"], done["final_text"], done["current_text"]) == ("done", "Endfassung", "Endfassung")
    assert done["finished_at"]
    with pytest.raises(learning.LearningError, match="Weiter bearbeiten"):
        learning.save_text(session["id"], "zu spät")
    assert learning.open_or_create_session(lid)["id"] == session["id"]  # the done session is shown again
    again = learning.reopen(session["id"])
    assert again["status"] == "open" and again["final_text"] == "Endfassung"
    assert learning.finish(session["id"], "Neue Endfassung")["final_text"] == "Neue Endfassung"
    with pytest.raises(learning.LearningError, match="Höchstens"):
        learning.save_text(learning.reopen(session["id"])["id"], "x" * (learning.TEXT_MAX_CHARS + 1))
    with pytest.raises(learning.LearningError, match="nicht gefunden"):
        learning.save_text(999, "x")


def test_only_one_open_session_per_listing(lid):
    db.add_draft(lid, "initial", "Entwurf", "claude-opus-5-5")
    first = learning.open_or_create_session(lid)
    learning.finish(first["id"], "Ende")
    second_id = db.create_session(lid, None, "Zweite Runde", None)
    with pytest.raises(learning.LearningError, match="schon eine andere Sitzung offen"):
        learning.reopen(first["id"])
    assert learning.open_or_create_session(lid)["id"] == second_id


def test_revise_in_editor(lid, fake_generate):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    db.add_rule("Regel", status="active")
    session = learning.open_or_create_session(lid)
    result = asyncio.run(learning.revise_in_editor(session["id"], "Original mit Handänderung", " Kürzer bitte "))
    assert fake_generate == [{"kind": "custom", "previous_text": "Original mit Handänderung",
                              "instruction": "Kürzer bitte", "rules": ["Regel"], "examples": []}]
    assert result["session"]["current_text"] == "Fassung 1" and result["session"]["original_text"] == "Original"
    draft = result["draft"]
    assert (draft["kind"], draft["instruction"], draft["text"]) == ("custom", "Kürzer bitte", "Fassung 1")
    (feedback,) = db.list_feedback()  # exactly one row: the editor's, not a second one from create_and_store_draft
    assert (feedback["source"], feedback["kind"], feedback["text"], feedback["before_text"], feedback["session_id"]) == (
        "editor", "critique", "Kürzer bitte", "Original mit Handänderung", session["id"])
    assert learning.editor_calls_today(lid) == 1
    with pytest.raises(learning.LearningError, match="ändern soll"):
        asyncio.run(learning.revise_in_editor(session["id"], "Text", "  "))
    learning.finish(session["id"], "Ende")
    with pytest.raises(learning.LearningError, match="Weiter bearbeiten"):
        asyncio.run(learning.revise_in_editor(session["id"], "Text", "Kürzer"))


def test_editor_daily_limit_and_budget(lid, fake_generate, monkeypatch):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    session = learning.open_or_create_session(lid)
    monkeypatch.setattr(learning, "EDITOR_DAILY_LIMIT", 2)
    asyncio.run(learning.revise_in_editor(session["id"], "Text", "eins"))
    db.add_llm_usage("rules", "anthropic", "claude-opus-5-5", listing_id=lid)  # rule proposals count too
    with pytest.raises(learning.LearningError, match="Tageslimit"):
        asyncio.run(learning.revise_in_editor(session["id"], "Text", "zwei"))
    monkeypatch.setattr(learning, "EDITOR_DAILY_LIMIT", 30)
    db.update_settings({"monthly_budget_eur": "0.005"})
    with pytest.raises(llm.BudgetExceeded):
        asyncio.run(learning.revise_in_editor(session["id"], "Text", "drei"))
    assert len(fake_generate) == 1


def test_revise_refuses_a_second_call_while_one_runs(lid, monkeypatch):
    db.add_draft(lid, "initial", "Original", "claude-opus-5-5")
    session = learning.open_or_create_session(lid)
    monkeypatch.setattr(learning, "_busy_sessions", {session["id"]})
    with pytest.raises(learning.LearningError, match="arbeitet schon"):
        asyncio.run(learning.revise_in_editor(session["id"], "Text", "Kürzer"))


# --- learned rules -------------------------------------------------------------------------------

def rules_answer(*actions):
    return fake_response({"actions": [dict(zip(("action", "rule_id", "text", "reason"), a)) for a in actions]})


def test_propose_rules_request_and_storage(lid, client):
    active = db.add_rule("Nenne kein Einzugsdatum.", status="active")
    other = db.add_rule("Keine Emojis.", status="active")
    db.add_rule("Schreib immer auf Englisch.", status="rejected")
    session = done_session(lid)
    db.add_feedback(lid, "editor", "critique", "Nicht so förmlich", session_id=session["id"], before_text="x")
    client.responses.append(rules_answer(
        ("add", None, "Stell dich mit Vornamen und Alter vor.", "Alter ergänzt"),
        ("update", active, "Formuliere den Einzug als „ab sofort“.", "präziser"),
        ("remove", other, "", "Person nutzt Emojis"),
        ("add", None, "Noch eine vierte.", "zu viel"),
    ))
    proposals = asyncio.run(learning.propose_rules(session["id"]))
    assert [(p["action"], p["replaces_rule_id"], p["text"], p["status"]) for p in proposals] == [
        ("add", None, "Stell dich mit Vornamen und Alter vor.", "proposed"),
        ("update", active, "Formuliere den Einzug als „ab sofort“.", "proposed"),
        ("remove", other, "Keine Emojis.", "proposed"),  # a remove shows the rule it would remove
    ]
    assert proposals[0]["proposed_text"] == proposals[0]["text"] and proposals[0]["listing_id"] == lid
    (call,) = client.beta.messages.calls  # Opus 5.5: with the refusal fallback, like drafts
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": learning.RULES_SCHEMA}
    assert call["thinking"] == {"type": "adaptive"} and call["system"] == learning.RULES_SYSTEM
    user = call["messages"][0]["content"]
    assert "<original>\nGuten Tag. Ich bin Erika.\n</original>" in user
    assert "<endversion>\nHallo. Ich bin Erika, 34.\n</endversion>" in user
    assert "<geaenderte_saetze>\n- Guten Tag.\n- Ich bin Erika.\n+ Hallo.\n+ Ich bin Erika, 34.\n</geaenderte_saetze>" in user
    assert "- „Nicht so förmlich“" in user and f"{active}: Nenne kein Einzugsdatum." in user
    assert "verworfen: „Schreib immer auf Englisch.“" in user
    assert "Helle 2-Zimmer-Wohnung · ImmoScout24 · 780 € · 54 m² · Musterstr. 12, Berlin" in user
    (usage,) = db.list_llm_usage()
    assert (usage["purpose"], usage["listing_id"], usage["input_tokens"]) == ("rules", lid, 2000)
    # Nothing changed since: no second call, the same proposals.
    assert [p["id"] for p in asyncio.run(learning.propose_rules(session["id"]))] == [p["id"] for p in proposals]
    assert len(client.calls) == 1


def test_propose_rules_drops_invalid_actions(lid, client):
    active = db.add_rule("Keine Emojis.", status="active")
    db.add_rule("Siez immer.", status="inactive")
    session = done_session(lid)
    client.responses.append(rules_answer(
        ("update", 999, "Unbekannte Regel", "x"),  # not an active rule
        ("add", None, "keine emojis", "x"),  # duplicate of an active rule
        ("add", None, "Siez immer", "x"),  # switched off by the user
        ("add", None, "   ", "x"),
        ("remove", str(active), "", "id as a string (DeutschlandGPT)"),
        ("remove", active, "", "same rule twice"),
        ("delete", active, "", "unknown action"),
    ))
    proposals = asyncio.run(learning.propose_rules(session["id"]))
    assert [(p["action"], p["replaces_rule_id"]) for p in proposals] == [("remove", active)]


def test_propose_rules_without_call(lid, client):
    unchanged = done_session(lid, original="Gleich.", final="Gleich.")
    assert asyncio.run(learning.propose_rules(unchanged["id"])) == []  # nothing to learn from
    other = db.insert_listing({**LISTING, "fredy_id": "L2"})[0]
    changed = done_session(other)
    db.update_settings({"use_rules": "0"})
    assert asyncio.run(learning.propose_rules(changed["id"])) == []
    assert client.calls == []
    open_one = learning.reopen(changed["id"])
    with pytest.raises(learning.LearningError, match="Erst „Fertig“"):
        asyncio.run(learning.propose_rules(open_one["id"]))


def test_new_round_of_rule_proposals_after_more_changes(lid, client):
    session = done_session(lid)
    client.responses += [rules_answer(("add", None, "Regel eins.", "a")), rules_answer(("add", None, "Regel zwei.", "b"))]
    asyncio.run(learning.propose_rules(session["id"]))
    learning.reopen(session["id"])
    learning.finish(session["id"], "Ganz andere Endfassung.")
    proposals = asyncio.run(learning.propose_rules(session["id"]))
    assert [p["text"] for p in proposals] == ["Regel zwei."]  # undecided proposals of the old round are replaced


def test_rules_through_deutschlandgpt(lid, client):
    db.update_settings({"provider": "deutschlandgpt", "model": "claude-haiku-5.5"})
    session = done_session(lid)
    client.responses.append(fake_response(text='Hier:\n```json\n{"actions": [{"action": "add", "rule_id": null, '
                                               '"text": "Kurz halten.", "reason": "gekürzt"}]}\n```',
                                          model="claude-haiku-5.5"))
    (proposal,) = asyncio.run(learning.propose_rules(session["id"]))
    assert proposal["text"] == "Kurz halten."
    (call,) = client.messages.calls  # no beta, no schema: JSON is asked for in the prompt
    assert "format" not in call["output_config"]
    assert call["system"].endswith(learning.JSON_INSTRUCTION + learning.RULES_SHAPE)
    (usage,) = db.list_llm_usage()
    assert (usage["provider"], usage["currency"]) == ("deutschlandgpt", "EUR")


def test_unreadable_answer_is_paid_for_and_reported(lid, client):
    session = done_session(lid)
    client.responses.append(fake_response(text="Leider kein JSON"))
    with pytest.raises(llm.LLMError, match="keine lesbare Antwort"):
        asyncio.run(learning.propose_rules(session["id"]))
    assert len(db.list_llm_usage()) == 1
    client.responses.append(fake_response(text="", stop_reason="refusal"))
    with pytest.raises(llm.LLMError, match="abgelehnt"):
        asyncio.run(learning.propose_rules(session["id"]))
    assert db.get_session(session["id"])["rules_basis"] is None  # a failed round can be retried


def test_decide_rules(lid):
    session = done_session(lid)
    target = db.add_rule("Alte Regel.", status="active")
    to_remove = db.add_rule("Weg damit.", status="active")
    add = db.add_rule("Neue Regel.", session_id=session["id"])
    update = db.add_rule("Bessere Regel.", action="update", replaces_rule_id=target, session_id=session["id"])
    remove = db.add_rule("Weg damit.", action="remove", replaces_rule_id=to_remove, session_id=session["id"])
    rejected = db.add_rule("Schlechte Regel.", session_id=session["id"])

    accepted = learning.decide_rule(add, True, "  Neue Regel, umformuliert. ")
    assert (accepted["status"], accepted["text"], accepted["proposed_text"]) == ("active", "Neue Regel, umformuliert.",
                                                                               "Neue Regel.")
    assert learning.decide_rule(update, True)["status"] == "active"
    assert db.get_rule(target)["status"] == "replaced"
    assert learning.decide_rule(remove, True)["id"] == to_remove
    assert db.get_rule(to_remove)["status"] == "inactive" and db.get_rule(remove) is None
    assert learning.decide_rule(rejected, False)["status"] == "rejected"
    with pytest.raises(learning.LearningError, match="schon entschieden"):
        learning.decide_rule(add, False)
    assert [r["text"] for r in learning.list_rules("active")] == ["Neue Regel, umformuliert.", "Bessere Regel."]
    assert learning.list_rules("replaced")[0]["status_label"] == "ersetzt"


def test_decide_rule_whose_target_is_gone(lid):
    target = db.add_rule("Alte Regel.", status="active")
    update = db.add_rule("Neu.", action="update", replaces_rule_id=target)
    learning.set_rule_active(target, False)
    with pytest.raises(learning.LearningError, match="nicht mehr aktiv"):
        learning.decide_rule(update, True)
    assert db.get_rule(update) is None


def test_rule_limit_and_editing(lid, monkeypatch):
    monkeypatch.setattr(learning, "MAX_ACTIVE_RULES", 2)
    first = db.add_rule("Eins.", status="active")
    db.add_rule("Zwei.", status="active")
    proposal = db.add_rule("Drei.")
    with pytest.raises(learning.LearningError, match="Höchstens 2 aktive Regeln"):
        learning.decide_rule(proposal, True)
    assert learning.set_rule_active(first, False)["status"] == "inactive"
    assert learning.decide_rule(proposal, True)["status"] == "active"
    with pytest.raises(learning.LearningError, match="Höchstens 2"):
        learning.set_rule_active(first, True)
    assert learning.update_rule_text(first, " Eins,  neu. ")["text"] == "Eins, neu."
    with pytest.raises(learning.LearningError, match="leer"):
        learning.update_rule_text(first, " ")
    with pytest.raises(learning.LearningError, match="Höchstens 300"):
        learning.update_rule_text(first, "x" * 301)
    pending = db.add_rule("Ändere Eins.", action="update", replaces_rule_id=first)
    learning.delete_rule(first)
    assert db.get_rule(first) is None and db.get_rule(pending) is None  # its open proposals go too
    with pytest.raises(learning.LearningError, match="nicht gefunden"):
        learning.delete_rule(first)


# --- profile rewrite ---------------------------------------------------------------------------

def rewrite_answer(*edits):
    keys = ("field", "old", "new", "reason", "evidence", "covers_rules", "is_fact")
    return fake_response({"edits": [dict(zip(keys, edit)) for edit in edits]})


def test_rewrite_needs_enough_feedback(lid, client):
    status = learning.rewrite_status()
    assert (status["feedback"], status["sessions"], status["rules"], status["enabled"]) == (0, 0, 0, False)
    assert status["base_version"]["source"] == "initial" and status["pending"] is None
    with pytest.raises(learning.LearningError, match="zu wenig"):
        asyncio.run(learning.propose_rewrite())
    db.add_feedback(lid, "telegram", "quick", "shorter")
    db.add_feedback(lid, "telegram", "quick", "shorter")
    assert not learning.rewrite_status()["enabled"]
    done_session(lid)  # a finished session with changes counts as feedback too
    assert learning.rewrite_status()["enabled"]
    db.mark_feedback_processed([f["id"] for f in db.list_feedback()], db.latest_version()["id"])
    db.add_rule("Regel.", status="active")
    assert learning.rewrite_status()["enabled"]  # one active rule is enough
    assert client.calls == []


def setup_rewrite(lid):
    db.update_settings({"style": "Freundlich und sachlich.", "profile": "Name: Erika\nAlter: 33",
                        "wishes": "- Balkon\n- Balkon"})
    db.snapshot_profile("manual")
    rule = db.add_rule("Nenne kein Einzugsdatum.", status="active")
    session = done_session(lid)
    critique = db.add_feedback(lid, "editor", "critique", "Immer per Du schreiben", session_id=session["id"])
    quick = [db.add_feedback(lid, "telegram", "quick", "shorter") for _ in range(3)]
    db.add_rule("Schreib auf Englisch.", status="rejected")
    return rule, session, critique, quick


def test_propose_rewrite_request_and_validation(lid, client):
    rule, session, critique, quick = setup_rewrite(lid)
    client.responses.append(rewrite_answer(
        ("style", "sachlich.", "sachlich, per Du.", "ausdrücklich gewünscht", [f"F{critique}", "F999"], [], False),
        ("system_prompt", "", "Nenne kein Einzugsdatum.", "Regel", [f"R{rule}"], [rule, 12345], False),
        ("profile", "Alter: 33", "Alter: 34", "korrigiert", [f"S{session['id']}"], [], False),
        ("target_words", "150", "110", "oft Kürzer", [], [], False),
        ("wishes", "- Balkon", "- Balkon oder Terrasse", "x", [], [], False),
        ("style", "gibt es nicht", "y", "x", [], [], False),
        ("target_words", "150", "5", "zu kurz", [], [], False),
        ("bogus", "a", "b", "x", [], [], False),
        ("style", "same", "same", "x", [], [], False),
    ))
    proposal = asyncio.run(learning.propose_rewrite())
    edits = {edit["id"]: edit for edit in proposal["edits"]}
    assert [edit["field"] for edit in proposal["edits"]] == ["style", "system_prompt", "profile", "target_words",
                                                             "wishes", "style", "target_words"]
    assert edits[1]["evidence"] == [f"F{critique}"] and edits[1]["applicable"] and not edits[1]["is_fact"]
    assert edits[2]["covers_rules"] == [rule]
    assert edits[3]["is_fact"]  # "Über mich" is always marked as fact
    assert (edits[5]["applicable"], edits[5]["problem"]) == (False, "Textstelle kommt mehrfach vor.")
    assert edits[6]["problem"] == "Textstelle nicht (mehr) im Feld gefunden."
    assert "Zahl von 30 bis 600" in edits[7]["problem"]
    assert all(edit["decision"] is None for edit in proposal["edits"])
    assert proposal["status"] == "pending" and proposal["base_version_id"] == db.latest_version()["id"]
    assert sorted(proposal["feedback_ids"]) == sorted([critique, *quick]) and proposal["session_ids"] == [session["id"]]

    (call,) = client.beta.messages.calls
    assert call["output_config"] == {"effort": "medium", "format": {"type": "json_schema",
                                                                  "schema": learning.REWRITE_SCHEMA}}
    assert call["max_tokens"] == learning.REWRITE_MAX_TOKENS and client.timeouts == [learning.REWRITE_TIMEOUT_S]
    user = call["messages"][0]["content"]
    assert "<style>\nFreundlich und sachlich.\n</style>" in user and "<target_words>\n150\n</target_words>" in user
    assert f"R{rule}: Nenne kein Einzugsdatum." in user
    assert f"F{critique} · Editor · Inserat: Helle 2-Zimmer-Wohnung" in user and "„Immer per Du schreiben“" in user
    assert f"S{session['id']} · Inserat:" in user and "+ Ich bin Erika, 34." in user
    assert "Kürzer: 3 · Förmlicher: 0 · Neu: 0" in user
    assert "- Regel: „Schreib auf Englisch.“" in user
    assert db.list_llm_usage()[0]["purpose"] == "rewrite" and db.list_llm_usage()[0]["listing_id"] is None
    assert learning.rewrite_status()["pending"]["id"] == proposal["id"]


def test_apply_rewrite(lid, client):
    rule, session, critique, quick = setup_rewrite(lid)
    client.responses.append(rewrite_answer(
        ("style", "sachlich.", "sachlich, per Du.", "gewünscht", [], [], False),
        ("system_prompt", "", "Nenne kein Einzugsdatum.", "Regel", [], [rule], False),
        ("profile", "Alter: 33", "Alter: 34", "korrigiert", [], [], True),
        ("target_words", "150", "110", "oft Kürzer", [], [], False),
    ))
    proposal = asyncio.run(learning.propose_rewrite())
    result = learning.apply_rewrite(proposal["id"], ["1", 2, 4])
    assert result["applied"] == [1, 2, 4] and result["failed"] == []
    values = db.current_profile_values()
    assert values["style"] == "Freundlich und sachlich, per Du."
    assert values["system_prompt"].endswith("\nNenne kein Einzugsdatum.")
    assert values["profile"] == "Name: Erika\nAlter: 33" and values["target_words"] == "110"
    version = db.get_version(result["version_id"])
    assert (version["source"], version["note"], version["data"]) == ("feedback", "3 Änderungen aus Feedback", values)
    decisions = {edit["id"]: edit["decision"] for edit in result["proposal"]["edits"]}
    assert decisions == {1: "accepted", 2: "accepted", 3: "rejected", 4: "accepted"}
    assert result["proposal"]["status"] == "accepted" and result["proposal"]["applied_version_id"] == version["id"]
    assert all(f["processed_version_id"] == version["id"] for f in db.list_feedback())
    assert db.get_session(session["id"])["processed_version_id"] == version["id"]
    learning.reopen(session["id"])
    learning.finish(session["id"], "Hallo. Ich bin Erika, 34.")  # same final text: stays processed
    assert db.get_session(session["id"])["processed_version_id"] == version["id"]
    learning.reopen(session["id"])
    learning.finish(session["id"], "Hallo. Ich bin Erika, 34, Ärztin.")  # changed: counts again
    assert db.get_session(session["id"])["processed_version_id"] is None
    db.mark_sessions_processed([session["id"]], version["id"])
    assert (db.get_rule(rule)["status"], db.get_rule(rule)["merged_version_id"]) == ("merged", version["id"])
    status = learning.rewrite_status()
    assert (status["feedback"], status["sessions"], status["rules"], status["enabled"]) == (0, 0, 0, False)
    with pytest.raises(learning.LearningError, match="schon entschieden"):
        learning.apply_rewrite(proposal["id"], [3])
    # The rejected edit is remembered for the next proposal.
    db.add_rule("Andere Regel.", status="active")
    client.responses.append(rewrite_answer())
    asyncio.run(learning.propose_rewrite())
    assert "- Änderung an profile: „Alter: 33“ → „Alter: 34“" in client.calls[-1]["messages"][0]["content"]


def test_apply_rewrite_without_selection_marks_feedback_processed(lid, client):
    setup_rewrite(lid)
    client.responses.append(rewrite_answer())  # "lieber 0 Änderungen als schwache"
    proposal = asyncio.run(learning.propose_rewrite())
    before = db.latest_version()["id"]
    result = learning.apply_rewrite(proposal["id"], [])
    assert result["version_id"] is None and db.latest_version()["id"] == before
    assert all(f["processed_version_id"] == before for f in db.list_feedback())


def test_apply_rewrite_rechecks_each_edit(lid, client):
    setup_rewrite(lid)
    client.responses.append(rewrite_answer(
        ("style", "sachlich.", "sachlich!", "a", [], [], False),
        ("style", "sachlich.", "nüchtern.", "b", [], [], False),  # fits alone, not after the first one
    ))
    proposal = asyncio.run(learning.propose_rewrite())
    result = learning.apply_rewrite(proposal["id"], [1, 2])
    assert (result["applied"], result["failed"]) == ([1], [2])
    assert result["proposal"]["edits"][1]["decision"] == "inapplicable"
    assert db.current_profile_values()["style"] == "Freundlich und sachlich!"


def test_rewrite_turns_stale_when_the_profile_changes(lid, client):
    setup_rewrite(lid)
    client.responses.append(rewrite_answer(("style", "sachlich.", "sachlich, per Du.", "x", [], [], False)))
    proposal = asyncio.run(learning.propose_rewrite())
    db.update_settings({"style": "Ganz anders."})
    db.snapshot_profile("manual")
    with pytest.raises(learning.LearningError, match="inzwischen geändert"):
        learning.apply_rewrite(proposal["id"], [1])
    assert learning.get_proposal(proposal["id"])["status"] == "stale"
    assert db.current_profile_values()["style"] == "Ganz anders."
    assert learning.rewrite_status()["pending"] is None
    assert all(f["processed_version_id"] is None for f in db.list_feedback())


def test_reject_rewrite_and_newer_proposal_makes_the_older_stale(lid, client):
    setup_rewrite(lid)
    client.responses += [rewrite_answer(("style", "sachlich.", "kühl.", "x", [], [], False)), rewrite_answer()]
    first = asyncio.run(learning.propose_rewrite())
    second = asyncio.run(learning.propose_rewrite())
    assert db.get_rewrite_proposal(first["id"])["status"] == "stale"
    rejected = learning.reject_rewrite(second["id"])
    assert rejected["status"] == "rejected"
    assert all(f["processed_version_id"] is None for f in db.list_feedback())  # feedback stays for the next try
    with pytest.raises(learning.LearningError, match="inzwischen geändert"):
        learning.reject_rewrite(first["id"])


def test_edit_preview_and_apply_edit():
    values = {"style": "Freundlich.", "target_words": "150"}
    edit = {"field": "style", "old": "", "new": "Kurze Sätze."}
    assert learning.edit_preview(edit, values) == ("Freundlich.", "Freundlich.\nKurze Sätze.")
    assert values["style"] == "Freundlich."  # the preview doesn't change the values passed in
    assert learning.apply_edit(values, {"field": "target_words", "old": "120", "new": "100"}) is not None
    assert learning.apply_edit(values, {"field": "style", "old": "", "new": "  "}) == "Nichts anzuhängen."
    assert learning.apply_edit({"system_prompt": "Alles"}, {"field": "system_prompt", "old": "Alles", "new": ""}) == (
        "Der System-Prompt darf nicht leer werden.")


# --- costs ----------------------------------------------------------------------------------------

def test_learning_costs_count_everywhere(lid, client):
    db.add_draft(lid, "initial", "x", "claude-opus-5-5", cost_usd=0.11)  # 0,10 € at 1,10
    db.add_llm_usage("rules", "anthropic", "claude-opus-5-5", listing_id=lid, cost_usd=0.22)  # 0,20 €
    db.add_llm_usage("rewrite", "deutschlandgpt", "claude-haiku-5.5", cost_usd=0.055, currency="EUR",
                     cost_native=0.05)  # 0,05 €, no listing
    assert db.cost_this_month() == pytest.approx(0.35)
    assert db.stats()["cost_total"] == pytest.approx(0.35) and db.stats()["cost_month"] == pytest.approx(0.35)
    assert db.list_listings()[0]["cost_eur"] == pytest.approx(0.30) and db.list_listings()[0]["draft_count"] == 1
    db.update_settings({"monthly_budget_eur": "0.35"})
    session = done_session(lid)
    with pytest.raises(llm.BudgetExceeded):
        asyncio.run(learning.propose_rules(session["id"]))
    assert client.calls == []
