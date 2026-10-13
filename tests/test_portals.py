import pytest

from app.portals import contact_url, fredy_url, provider_label, safe_url


@pytest.mark.parametrize("url", [
    "https://www.immobilienscout24.de/expose/123",
    "http://example.com",
    "HTTPS://Example.com/path?q=1#frag",
    "https://example.com:8443/x",
])
def test_safe_url_accepts_http_links(url):
    assert safe_url(url) == url


def test_safe_url_strips_surrounding_whitespace():
    assert safe_url("  https://example.com/a  ") == "https://example.com/a"


@pytest.mark.parametrize("url", [
    None,
    "",
    "   ",
    "javascript:alert(1)",
    "JaVaScRiPt:alert(document.cookie)",
    " javascript:alert(1)",
    "javascript://example.com/%0aalert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "file:///etc/passwd",
    "ftp://example.com/file",
    "//example.com/path",
    "/relative/path",
    "example.com",
    "https://",
    "https:///path-only",
    "https://exa mple.com",
    "https://example.com/a\nb",
    "https://example.com/\x00",
    "https://immobilienscout24.de@evil.example/expose/1",
    "https://example.com:notaport/",
    "https://[::1",
    123,
])
def test_safe_url_rejects_everything_else(url):
    assert safe_url(url) is None


def test_contact_url_immoscout_expose_gets_contact_fragment():
    url = "https://www.immobilienscout24.de/expose/123456789"
    assert contact_url("immoscout", url) == url + "#/basicContact/email"


def test_contact_url_immoscout_replaces_existing_fragment_and_keeps_query():
    url = "https://www.immobilienscout24.de/expose/123?referrer=RESULT_LIST#/gallery"
    assert contact_url("immoscout", url) == "https://www.immobilienscout24.de/expose/123?referrer=RESULT_LIST#/basicContact/email"


def test_contact_url_immoscout_other_path_unchanged():
    url = "https://www.immobilienscout24.de/Suche/de/berlin/wohnung-mieten"
    assert contact_url("immoscout", url) == url


def test_contact_url_wg_gesucht_listing_gets_message_path():
    url = "https://www.wg-gesucht.de/wohnungen-in-Berlin-Mitte.12345.html"
    assert contact_url("wgGesucht", url) == "https://www.wg-gesucht.de/nachricht-senden/wohnungen-in-Berlin-Mitte.12345.html"


@pytest.mark.parametrize("url", [
    "https://www.wg-gesucht.de/nachricht-senden/wohnungen-in-Berlin-Mitte.12345.html",
    "https://www.wg-gesucht.de/wohnungen-in-Berlin.8.2.1.0.html/extra",
    "https://www.wg-gesucht.de/",
])
def test_contact_url_wg_gesucht_other_paths_unchanged(url):
    assert contact_url("wgGesucht", url) == url


def test_contact_url_mcmakler_gets_form_anchor():
    url = "https://www.mcmakler.de/immobilien/expose/abc-123"
    assert contact_url("mcMakler", url) == url + "#expose-section-anfrage"


@pytest.mark.parametrize("provider", ["kleinanzeigen", "immowelt", "ohneMakler", None, "somethingNew"])
def test_contact_url_other_providers_return_listing_url(provider):
    url = "https://www.example-portal.de/expose/42"
    assert contact_url(provider, url) == url


@pytest.mark.parametrize("provider", ["immoscout", "wgGesucht", "mcMakler", "kleinanzeigen"])
def test_contact_url_rejects_unsafe_urls(provider):
    assert contact_url(provider, "javascript:alert(1)") is None
    assert contact_url(provider, None) is None


def test_contact_url_ignores_provider_host_mismatch():
    url = "https://evil.example/expose/123"
    assert contact_url("immoscout", url) == url


# A listing from a Fredy job, with the link Fredy sent from its own (old) base URL.
FROM_FREDY = {"fredy_id": "abc123", "job_id": "job1", "fredy_url": "https://old-fredy.example.com/#/listings/listing/abc123"}


@pytest.mark.parametrize("base", ["https://fredy.example.com", "https://fredy.example.com/"])
def test_fredy_url_built_from_base(base):
    assert fredy_url(FROM_FREDY, base) == "https://fredy.example.com/#/listings/listing/abc123"


def test_fredy_url_keeps_path_prefix():
    assert fredy_url(FROM_FREDY, "https://example.com/fredy") == "https://example.com/fredy/#/listings/listing/abc123"


@pytest.mark.parametrize("base", ["", None, "javascript:alert(1)", "https://user:pw@evil.example"])
def test_fredy_url_without_usable_base_uses_stored_link(base):
    assert fredy_url(FROM_FREDY, base) == FROM_FREDY["fredy_url"]


@pytest.mark.parametrize("missing", ["job_id", "fredy_id"])
def test_fredy_url_only_for_listings_from_fredy(missing):
    """A listing that didn't come from a Fredy job (e.g. added by hand) has no page in Fredy to build a link to."""
    listing = {**FROM_FREDY, missing: None}
    assert fredy_url(listing, "https://fredy.example.com") == FROM_FREDY["fredy_url"]
    assert fredy_url({**listing, "fredy_url": None}, "https://fredy.example.com") is None


def test_fredy_url_escapes_the_id():
    listing = {**FROM_FREDY, "fredy_id": "a/b?c#d e&f"}
    assert fredy_url(listing, "https://fredy.example.com") == "https://fredy.example.com/#/listings/listing/a%2Fb%3Fc%23d%20e%26f"


def test_fredy_url_stored_link_must_be_safe():
    assert fredy_url({**FROM_FREDY, "job_id": None, "fredy_url": "JavaScript:alert(1)"}, "https://fredy.example.com") is None


def test_provider_label():
    assert provider_label("immoscout") == "ImmoScout24"
    assert provider_label("wgGesucht") == "WG-Gesucht"
    assert provider_label("kleinanzeigen") == "Kleinanzeigen"
    assert provider_label("immowelt") == "Immowelt"
    assert provider_label("ohneMakler") == "Ohne-Makler"
    assert provider_label("brandNewPortal") == "brandNewPortal"
    assert provider_label(None) == "?"
    assert provider_label("") == "?"
