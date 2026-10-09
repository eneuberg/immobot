import pytest

from app.portals import contact_url, provider_label, safe_url


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


def test_provider_label():
    assert provider_label("immoscout") == "ImmoScout24"
    assert provider_label("wgGesucht") == "WG-Gesucht"
    assert provider_label("kleinanzeigen") == "Kleinanzeigen"
    assert provider_label("immowelt") == "Immowelt"
    assert provider_label("ohneMakler") == "Ohne-Makler"
    assert provider_label("brandNewPortal") == "brandNewPortal"
    assert provider_label(None) == "?"
    assert provider_label("") == "?"
