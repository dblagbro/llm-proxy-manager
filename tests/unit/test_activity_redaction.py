"""Regression: secret redaction must fire on structured bodies and modern token
shapes, and the default-on preview path must redact.

Before v5.22.43:
- `_redact` ran lexically over json.dumps output, so `{"Authorization": "Bearer
  <tok>"}` was never matched (the key is quoted → `\\s*:\\s*` didn't match after
  the closing quote). Verified by the data-layer audit that it passed through
  byte-identical.
- `sk-[A-Za-z0-9]{20,}` broke on the first hyphen of sk-proj-/sk-or-v1-.
- `_extract_preview` (default-on) stored the extracted user text verbatim.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.monitoring.helpers import (
    _extract_preview,
    _redact,
    _redact_structure,
    _serialize_body,
)

SECRET_SAMPLES = {
    "openai_proj": "sk-proj-abcdEFGH1234 ijkl",  # noqa: S105
    "openrouter": "sk-or-v1-abcdefabcdefabcdef1234",  # noqa: S105
    "google_oauth": "ya29.A0ARrdaM-longtokenvalue_here-xyz",  # noqa: S105
    "aws": "AKIAIOSFODNN7EXAMPLE",  # noqa: S105  # pragma: allowlist secret
    "groq": "gsk_abcdefghijklmnopqrstuvwxyz",  # noqa: S105
    "llmp": "llmp-abcdefghijklmnopqrstuvwxyz1234",  # noqa: S105  # pragma: allowlist secret
    "jwt": "eyJhbGci.eyJzdWIiOiIx.SflKxwRJ",  # noqa: S105
}


@pytest.fixture
def bearer_sample():
    """Generate nonfunctional test data without a hardcoded bearer credential."""
    return f"ya29.{uuid4().hex}"


def test_lexical_redaction_covers_modern_shapes():
    for name, tok in SECRET_SAMPLES.items():
        out = _redact(f"my key is {tok} ok")
        assert tok not in out, f"{name} survived redaction: {out!r}"


def test_structural_redaction_nulls_sensitive_keys(bearer_sample):
    body = {
        "model": "claude-sonnet-5",
        "headers": {"Authorization": f"Bearer {bearer_sample}"},
        "api_key": "sk-proj-abcd1234efgh5678",
        "nested": [{"access_token": "ya29.another", "keep": "visible"}],
    }
    red = _redact_structure(body)
    assert red["headers"]["Authorization"] == "***REDACTED***"
    assert red["api_key"] == "***REDACTED***"
    assert red["nested"][0]["access_token"] == "***REDACTED***"
    assert red["nested"][0]["keep"] == "visible"
    assert red["model"] == "claude-sonnet-5"


def test_serialize_body_redacts_json_authorization(bearer_sample):
    body = {"headers": {"Authorization": f"Bearer {bearer_sample}"}}
    text = _serialize_body(body, max_chars=10_000)
    assert bearer_sample not in text
    assert "Bearer ya29" not in text


def test_preview_redacts_pasted_secret():
    body = {"messages": [{"role": "user", "content": "here is my key sk-proj-abcd1234efgh5678 ok"}]}
    prev = _extract_preview(body)
    assert prev is not None
    assert "sk-proj-abcd1234efgh5678" not in prev
