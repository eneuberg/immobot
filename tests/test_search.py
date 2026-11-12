"""app/search.py and the search on the listings page (word search)."""

import dataclasses

import pytest
from fastapi.testclient import TestClient

from app import db, main, search

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
