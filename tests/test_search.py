"""app/search.py and the search on the listings page: word search (free) and "Mit Claude suchen"."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db, learning, llm, main, search, web

from .conftest import TEST_SECRETS


@pytest.mark.parametrize("query,terms", [
    ("wohnung in der Montanusstraße", ["montanusstr"]),
    ("Montanus Str.", ["montanusstr"]),
    ("Wohnung mit Balkon und Garten", ["balkon", "garten"]),
    ("3 Zimmer in Köln", ["3", "koeln"]),
    ("mit der", []),
])
def test_query_terms(query, terms):
    assert search.query_terms(query) == terms


def listing(**fields):
    return {"id": 1, "title": "", "address": "", "price": "", "size": "", "description": "", "details": None, **fields}


def test_keyword_search_normalizes_spelling():
    rows = [listing(id=1, address="Montanusstr. 5, 50737 Köln"), listing(id=2, title="Altbau mit Balkon",
                                                                           details="## Lage\nRuhiger Garten"),
            listing(id=3, title="Neubau")]
    assert [r["id"] for r in search.keyword_search("Wohnung in der Montanusstraße", rows)] == [1]
    assert [r["id"] for r in search.keyword_search("montanus strasse", rows)] == [1]
    assert [r["id"] for r in search.keyword_search("Balkon Garten", rows)] == [2]
    assert search.keyword_search("Balkon Sauna", rows) == []
    assert search.keyword_search("die wohnung", rows) == []


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = dataclasses.replace(main.config, data_dir=str(tmp_path))
    monkeypatch.setattr(main, "config", cfg)
    monkeypatch.setattr(main.app.state, "config", cfg)
    with TestClient(main.app, follow_redirects=False) as c:
        r = c.post("/login", data={"password": TEST_SECRETS["DASHBOARD_PASSWORD"]})
        assert r.status_code == 303
        yield c


def add(title, address="", **extra):
    return db.insert_listing({"fredy_id": title, "provider": "immoscout", "title": title, "address": address, **extra})[0]


def test_word_search_on_the_listings_page(client):
    add("Helle 2-Zimmer-Wohnung", "Montanusstraße 3, Köln")
    add("Altbau in Ehrenfeld", "Venloer Str. 1")
    page = client.get("/", params={"q": "wohnung in der Montanusstr."}).text
    assert "1 Treffer</strong> für „wohnung in der Montanusstr.“" in page and "Helle 2-Zimmer-Wohnung" in page
    assert "Altbau in Ehrenfeld" not in page and "Suche zurücksetzen" in page
    page = client.get("/", params={"q": "Sauna"}).text
    assert "0 Treffer" in page and "Kein Inserat enthält alle Wörter." in page


def test_claude_search(client, monkeypatch):
    first, second = add("Ruhige Wohnung", "Am Park 1"), add("Laute Wohnung", "Hauptstraße 2")
    seen = {}

    async def ask_json(settings, **kwargs):
        seen.update(kwargs)
        db.add_llm_usage("search", "anthropic", kwargs["model"], input_tokens=1000, output_tokens=50, cost_usd=0.002,
                         currency="USD", cost_native=0.002)
        return {"matches": [{"id": first, "why": "liegt  am Park"}, {"id": 999, "why": "gibt es nicht"},
                            {"id": first, "why": "doppelt"}]}

    monkeypatch.setattr(learning, "_ask_json", ask_json)
    r = client.post("/search", data={"q": "ruhig und grün"})
    assert r.status_code == 200
    assert "1 Treffer</strong> für „ruhig und grün“ · mit Claude" in r.text and "🔎 liegt am Park" in r.text
    assert "Laute Wohnung" not in r.text
    assert seen["purpose"] == "search" and seen["model"] == "claude-haiku-5-5" and f"[{second}] Laute Wohnung" in seen["user"]


def test_claude_search_errors_are_shown(client, monkeypatch):
    add("Wohnung")

    async def broke(settings, **kwargs):
        raise llm.BudgetExceeded("Monatsbudget von 15,00 € erreicht – im Dashboard erhöhen.")

    monkeypatch.setattr(learning, "_ask_json", broke)
    page = client.post("/search", data={"q": "irgendwas"}).text
    assert "Monatsbudget von 15,00 € erreicht" in page and "0 Treffer" in page
    assert client.post("/search", data={"q": "  "}).headers["location"] == "/"
    assert client.post("/search", data={"q": "x"}, headers={"Origin": "https://evil.example"}).status_code == 403
