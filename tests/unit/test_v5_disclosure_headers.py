"""v5.0.0 — disclosure header injection on substituted responses.

Verifies that when ``RouteResult.compliance_substituted=True``, the
messages.py / completions.py handlers merge the 7 ``X-Compliance-*``
headers into the response and emit one ``model_substitution``
compliance event.

The full request handler is heavy to stand up; these tests exercise the
header-building + audit-event-shape code directly via the compliance
helpers (the integration surface the dispatchers call).
"""
from __future__ import annotations

import pytest

from app.compliance import (
    compliance_headers,
    build_disclosure_payload,
    refusal_headers_ua,
    refusal_headers_no_substitute,
    wants_sse_prelude,
    sse_prelude_anthropic,
    sse_prelude_openai_inject,
    generate_audit_id,
)


def test_substitution_headers_carry_seven_fields():
    audit_id = generate_audit_id()
    headers = compliance_headers(
        blocked_company="anthropic",
        requested_model="claude-haiku",
        served_model="gpt-4o-mini",
        served_company="openai",
        served_provider_id="prov-openai-1",
        audit_id=audit_id,
    )
    assert headers["X-Compliance-Substitution"] == "true"
    assert headers["X-Compliance-Substitution-Code"] == (
        "api-key-policy:blocked-company:anthropic"
    )
    assert headers["X-Compliance-Requested-Model"] == "claude-haiku"
    assert headers["X-Compliance-Served-Model"] == "gpt-4o-mini"
    assert headers["X-Compliance-Served-Provider"] == "prov-openai-1"
    assert "OpenAI" in headers["X-Compliance-Note"]
    assert headers["X-Compliance-Audit-Id"] == audit_id
    assert len(headers) == 7


def test_refusal_headers_ua_path():
    audit_id = generate_audit_id()
    headers = refusal_headers_ua(
        matched_product="claude-cli",
        matched_company="anthropic",
        audit_id=audit_id,
    )
    assert headers["X-Compliance-Refusal"] == "true"
    assert headers["X-Compliance-Refusal-Reason"] == "client-product-banned"
    assert headers["X-Compliance-Matched-Product"] == "claude-cli"
    assert headers["X-Compliance-Matched-Company"] == "anthropic"
    assert headers["X-Compliance-Audit-Id"] == audit_id


def test_refusal_headers_no_substitute_path():
    audit_id = generate_audit_id()
    headers = refusal_headers_no_substitute(audit_id=audit_id)
    assert headers["X-Compliance-Refusal"] == "true"
    assert headers["X-Compliance-Refusal-Reason"] == "no-compliant-provider-available"
    assert headers["X-Compliance-Audit-Id"] == audit_id


def test_disclosure_payload_shape_matches_headers():
    audit_id = generate_audit_id()
    payload = build_disclosure_payload(
        blocked_company="anthropic",
        requested_model="claude-haiku",
        served_model="gpt-4o-mini",
        served_company="openai",
        served_provider_id="prov-openai-1",
        audit_id=audit_id,
    )
    assert payload["substituted"] is True
    assert payload["blocked_company"] == "anthropic"
    assert payload["requested_model"] == "claude-haiku"
    assert payload["served_model"] == "gpt-4o-mini"
    assert payload["served_company"] == "openai"
    assert payload["served_provider_id"] == "prov-openai-1"
    assert payload["audit_id"] == audit_id
    assert "OpenAI" in payload["note"]


def test_wants_sse_prelude_is_opt_in():
    # Default: no header → no prelude.
    assert wants_sse_prelude({}) is False
    # Wrong value → no prelude.
    assert wants_sse_prelude({"accept-compliance-events": "false"}) is False
    assert wants_sse_prelude({"accept-compliance-events": "1"}) is False
    # Only literal "true" opts in (case-insensitive).
    assert wants_sse_prelude({"accept-compliance-events": "true"}) is True
    assert wants_sse_prelude({"accept-compliance-events": "TRUE"}) is True


def test_sse_prelude_anthropic_emits_compliance_event_before_message_start():
    disclosure = build_disclosure_payload(
        blocked_company="anthropic",
        requested_model="claude-haiku",
        served_model="gpt-4o-mini",
        served_company="openai",
        served_provider_id="prov-1",
        audit_id="comp_test123",
    )
    frame = sse_prelude_anthropic(disclosure)
    text = frame.decode("utf-8")
    assert text.startswith("event: compliance_substitution\n")
    assert "data: {" in text
    assert text.endswith("\n\n")
    assert "comp_test123" in text


def test_sse_prelude_openai_injects_top_level_key():
    first = {"id": "chatcmpl-x", "choices": []}
    disclosure = {"substituted": True, "audit_id": "comp_test123"}
    result = sse_prelude_openai_inject(first, disclosure)
    assert result is first  # mutates in place
    assert first["compliance_substitution"] == disclosure


# ── Source-level guards ────────────────────────────────────────────────


def test_messages_py_calls_compliance_headers_helper():
    from pathlib import Path
    src = Path("app/api/messages.py").read_text()
    assert "compliance_headers(" in src
    assert "build_disclosure_payload(" in src
    assert "compliance_substituted" in src
    assert "model_substitution" in src


def test_completions_py_calls_compliance_headers_helper():
    from pathlib import Path
    src = Path("app/api/completions.py").read_text()
    assert "compliance_headers(" in src
    assert "build_disclosure_payload(" in src
    assert "compliance_substituted" in src
    assert "model_substitution" in src


def test_messages_streaming_threads_compliance_into_stream_anthropic():
    from pathlib import Path
    src = Path("app/api/_messages_streaming.py").read_text()
    assert "compliance_disclosure" in src
    assert "accept_compliance_events" in src
    assert "sse_prelude_anthropic" in src


def test_completions_streaming_threads_compliance_into_stream_openai():
    from pathlib import Path
    src = Path("app/api/_completions_streaming.py").read_text()
    assert "compliance_disclosure" in src
    assert "accept_compliance_events" in src
    assert "sse_prelude_openai_inject" in src


def test_record_outcome_accepts_compliance_kwargs():
    """v5.0.0 decision 9 — activity_log enrichment via event_meta, not a
    new column. record_outcome must accept compliance_substituted /
    blocked_company / served_company as keyword args + propagate them
    into _build_event_meta_base."""
    import inspect
    from app.monitoring.helpers import record_outcome, _build_event_meta_base
    rs = inspect.signature(record_outcome)
    assert "compliance_substituted" in rs.parameters
    assert "blocked_company" in rs.parameters
    assert "served_company" in rs.parameters
    bs = inspect.signature(_build_event_meta_base)
    assert "compliance_substituted" in bs.parameters
    assert "blocked_company" in bs.parameters
    assert "served_company" in bs.parameters


def test_event_meta_includes_compliance_block_when_substituted():
    """When compliance_substituted=True, _build_event_meta_base emits a
    ``compliance`` sub-dict with the three fields."""
    from app.monitoring.helpers import _build_event_meta_base
    meta = _build_event_meta_base(
        model="openai/gpt-4o-mini",
        provider_name="openai-1",
        api_key_prefix="llmp-xxx",
        key_record_id="key-1",
        is_subscription=False,
        is_probe=False,
        requested_model="claude-haiku",
        had_lmrh_hint=False,
        lmrh_hint_raw=None,
        lmrh_warnings=None,
        compliance_substituted=True,
        blocked_company="anthropic",
        served_company="openai",
    )
    assert meta["compliance"]["substituted"] is True
    assert meta["compliance"]["blocked_company"] == "anthropic"
    assert meta["compliance"]["served_company"] == "openai"


def test_event_meta_omits_compliance_block_when_not_substituted():
    """No compliance sub-dict on requests that weren't substituted."""
    from app.monitoring.helpers import _build_event_meta_base
    meta = _build_event_meta_base(
        model="openai/gpt-4o",
        provider_name="openai-1",
        api_key_prefix="llmp-xxx",
        key_record_id="key-1",
        is_subscription=False,
        is_probe=False,
        requested_model="gpt-4o",
        had_lmrh_hint=False,
        lmrh_hint_raw=None,
        lmrh_warnings=None,
    )
    assert "compliance" not in meta
