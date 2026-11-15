"""Tests for parsing the consent signature out of the OIDC consent page."""

import pytest

from cremalink.clients.auth import _extract_consent_signature


@pytest.mark.parametrize(
    "line",
    [
        "const consentObj2Sig = 'abc123+/=';",
        'const consentObj2Sig = "abc123+/=";',
        "  const  consentObj2Sig='abc123+/=' ;",
    ],
)
def test_extracts_signature_with_either_quote_style(line):
    page = (
        "<script>\nconst other = 1;\n"
        f"{line}\n"
        'const b_js_consentOBJString = "x";\n</script>'
    )
    assert _extract_consent_signature(page) == "abc123+/="


@pytest.mark.parametrize(
    "page",
    [
        "<html>no marker here</html>",
        '<script>const consentObj2Sig = "";</script>',
        "<script>const consentObj2Sig = '';</script>",
    ],
)
def test_missing_or_empty_signature_raises_clear_error(page):
    with pytest.raises(ValueError, match="consent signature"):
        _extract_consent_signature(page)
