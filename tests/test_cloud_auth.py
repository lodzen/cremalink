"""Gigya consent-page parsing for ``authenticate_cloud`` (step 5)."""

import pytest
from cremalink.clients.auth import _extract_consent_signature


def test_double_quoted_signature_current_page_format():
    # Format observed on aylaopenid.delonghigroup.com on 2026-11-12.
    html = '<script>\n  const consentObj2Sig = "abc-DEF_123";\n  const x = 1;</script>'
    assert _extract_consent_signature(html) == "abc-DEF_123"


def test_single_quoted_signature_legacy_page_format():
    html = "const consentObj2Sig = 'abc-DEF_123';"
    assert _extract_consent_signature(html) == "abc-DEF_123"


def test_missing_signature_raises_clear_error_not_index_error():
    with pytest.raises(ValueError, match="consent page changed"):
        _extract_consent_signature("<html>error page</html>")
