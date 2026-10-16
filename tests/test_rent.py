"""Tests for app/rent.py: warm and total rent from the exposé text."""

import pytest

from app import rent


def listing(price="570 €", details=None):
    return {"price": price, "details": details}


@pytest.mark.parametrize("details,expected", [
    (None, ["570 €"]),  # no exposé: Fredy's price as it is
    ("Warmmiete: 690 €\nGesamtmiete: 690 €", ["570 € kalt", "690 € warm"]),
    ("Warmmiete: 690 €\nGesamtmiete: 690,00 €", ["570 € kalt", "690 € warm"]),
    ("Warmmiete: 690 €\nGesamtmiete: 740 €", ["570 € kalt", "690 € warm", "740 € gesamt"]),
    ("Warmmiete: keine Angabe\nGesamtmiete: 790 €", ["570 € kalt", "790 € gesamt"]),
    ("Warmmiete: keine Angabe\nGesamtmiete: 900 € zzgl. Heizkosten", ["570 € kalt", "900 € gesamt zzgl. Heizkosten"]),
    ("Kaltmiete: 570 €\nNebenkosten: 120 €", ["570 €"]),
    # the first value counts (a text section may repeat it further down)
    ("Warmmiete: 930 €\nGesamtmiete: 930 €\n## Sonstiges\nGesamtmiete: 930,00 € inkl. Möbel", ["570 € kalt", "930 € warm"]),
])
def test_parts(details, expected):
    assert rent.parts(listing(details=details)) == expected


def test_price_text_and_info():
    item = listing(details="Warmmiete: 1.077 €\nGesamtmiete: 1.077 €")
    assert rent.price_text(item) == "570 € kalt · 1.077 € warm"
    assert rent.info(item) == {"cold": "570 €", "warm": "1.077 €", "total": None}
    assert rent.price_text(listing(price=None)) == "" and rent.parts(listing(price="", details="Warmmiete: 1 €")) == ["1 € warm"]
