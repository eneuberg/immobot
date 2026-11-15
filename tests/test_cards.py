"""Tests for app/cards.py: what a main-page card says beyond the title (rent, rooms, district, one flag)."""

import pytest

from app import cards


@pytest.mark.parametrize("listing,expected", [
    ({"details": "Kaltmiete: 750 €\nZimmer: 1\nWarmmiete: 810 €"}, "1 Zimmer"),
    ({"details": "Zimmer: 2,5"}, "2,5 Zimmer"),
    ({"details": "Zimmer: 3.0"}, "3 Zimmer"),
    ({"title": "Gepflegte 3-Zimmer-Wohnung mit Balkon"}, "3 Zimmer"),
    ({"title": "Helle 2 Zimmer Wohnung"}, "2 Zimmer"),
    ({"title": "Schöne 2,5-Zi.-Wohnung"}, "2,5 Zimmer"),
    ({"title": "Apartment mitten in Ehrenfeld"}, None),
    ({"title": "Wohnung 12 Zimmerpflanzen inklusive"}, None),  # no single digit before "Zimmer"
])
def test_rooms(listing, expected):
    assert cards.rooms(listing) == expected


@pytest.mark.parametrize("address,expected", [
    ("Sonderburger Straße 63, 51065 Köln, Mülheim", "Mülheim"),
    ("50670 Köln, Altstadt & Neustadt-Nord", "Altstadt & Neustadt-Nord"),
    ("50667 Köln", "Köln"),
    ("", None),
    (None, None),
])
def test_district(address, expected):
    assert cards.district({"address": address}) == expected


def test_rent_text_prefers_warm_then_total_and_drops_qualifiers():
    assert cards.rent_text({"price": "750 €", "details": "Warmmiete: 810 €\nGesamtmiete: 860 €"}) == "810 € warm"
    assert cards.rent_text({"price": "700 €", "details": "Gesamtmiete: 900 € zzgl. Heizkosten"}) == "900 € gesamt"
    assert cards.rent_text({"price": "700 €"}) == "700 €"
    # without an exposé: the same lines in Fredy's description count too (rent.info)
    assert cards.rent_text({"price": "450 €", "description": "Kaltmiete: 450 €\nGesamtmiete: 600 €\n"}) == "600 € gesamt"
    assert cards.rent_text({"price": None}) is None


def test_flag_picks_the_warning_else_the_first_point():
    hint = ("- **Kontakt per E-Mail:** bitte direkt anschreiben.\n"
            "- **Vorsicht:** typisches Muster bei Fake-Inseraten.\n"
            "- 4. Stock **ohne Aufzug**.")
    assert cards.flag(hint) == {"label": "Vorsicht", "count": 3, "alarm": True}
    assert cards.flag("- **Ohne Küche**: selbst einbauen.\n- Kaution 2.100 €") == {
        "label": "Ohne Küche", "count": 2, "alarm": False}
    assert cards.flag("- Die Gesamtmiete liegt bei 980 € warm") == {
        "label": "Die Gesamtmiete liegt bei", "count": 1, "alarm": False}
    long = cards.flag("- **" + "Sehr langer Hinweis " * 3 + "**: Text")
    assert len(long["label"]) <= cards.LABEL_MAX and long["label"].endswith("…")
    assert cards.flag("Bitte **Codewort** nennen") == {"label": "Codewort", "count": 1, "alarm": False}
    assert cards.flag(None) is None and cards.flag("  ") is None


def test_facts_bundle():
    facts = cards.facts({"price": "750 €", "title": "1-Zimmer-Wohnung", "address": "50735 Köln, Niehl"},
                        "- **Kontaktdaten:** gewünscht")
    assert facts == {"rent": "750 €", "rooms": "1 Zimmer", "district": "Niehl",
                     "flag": {"label": "Kontaktdaten", "count": 1, "alarm": False}, "tags": [], "dream": False}
