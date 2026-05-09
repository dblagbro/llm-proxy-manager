"""v3.5.8 — input validation + upstream-error sanitization tests.

Closes BUG-004 (completions missing model/messages), BUG-005 (messages
empty body), BUG-007 (role stack-trace leak), BUG-008 (max_tokens
stack-trace leak).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api._input_validation import (
    validate_completion_request,
    sanitize_upstream_error,
)


# ── validate_completion_request ─────────────────────────────────────


def test_validate_rejects_non_dict_body():
    """A list, string, or None body is unambiguously broken."""
    for bad in [None, [], "not json", 42]:
        with pytest.raises(HTTPException) as ex:
            validate_completion_request(bad, endpoint="messages")  # type: ignore[arg-type]
        assert ex.value.status_code == 400
        assert "object" in str(ex.value.detail).lower()


def test_validate_rejects_empty_body():
    """BUG-005: ``{}`` was accepted pre-fix and burned provider quota."""
    with pytest.raises(HTTPException) as ex:
        validate_completion_request({}, endpoint="messages")
    assert ex.value.status_code == 400
    assert "model" in str(ex.value.detail).lower()


def test_validate_rejects_missing_model():
    """BUG-004: missing model field on completions hit upstream 429."""
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {"messages": [{"role": "user", "content": "hi"}]},
            endpoint="completions",
        )
    assert ex.value.status_code == 400
    assert "model" in str(ex.value.detail).lower()


def test_validate_accepts_auto_model():
    """``model: "auto"`` and ``"llmp-auto"`` are valid (auto-routing)."""
    validate_completion_request(
        {"model": "auto", "messages": [{"role": "user", "content": "hi"}]},
        endpoint="messages",
    )
    validate_completion_request(
        {"model": "llmp-auto", "messages": [{"role": "user", "content": "hi"}]},
        endpoint="messages",
    )


def test_validate_rejects_empty_messages():
    """BUG-005: empty messages list was accepted, returned 200 with empty content."""
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {"model": "x", "messages": []},
            endpoint="messages",
        )
    assert ex.value.status_code == 400
    assert "messages" in str(ex.value.detail).lower()


def test_validate_rejects_messages_not_list():
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {"model": "x", "messages": "hi"},
            endpoint="messages",
        )
    assert ex.value.status_code == 400


def test_validate_rejects_message_without_role():
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {"model": "x", "messages": [{"content": "hi"}]},
            endpoint="messages",
        )
    assert ex.value.status_code == 400
    assert "role" in str(ex.value.detail).lower()


def test_validate_rejects_invalid_role():
    """BUG-007: 'banana' role used to leak a litellm stack trace."""
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {"model": "x", "messages": [{"role": "banana", "content": "hi"}]},
            endpoint="messages",
        )
    assert ex.value.status_code == 400
    assert "banana" in str(ex.value.detail)


def test_validate_accepts_all_standard_roles():
    """system / user / assistant / tool / function — all must pass."""
    for role in ("system", "user", "assistant", "tool", "function"):
        validate_completion_request(
            {"model": "x", "messages": [{"role": role, "content": "hi"}]},
            endpoint="messages",
        )


def test_validate_rejects_negative_max_tokens():
    """BUG-008: max_tokens=-5 leaked a Gemini error."""
    with pytest.raises(HTTPException) as ex:
        validate_completion_request(
            {
                "model": "x",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": -5,
            },
            endpoint="messages",
        )
    assert ex.value.status_code == 400
    assert "max_tokens" in str(ex.value.detail)


def test_validate_rejects_zero_max_tokens():
    with pytest.raises(HTTPException):
        validate_completion_request(
            {"model": "x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 0},
            endpoint="messages",
        )


def test_validate_rejects_max_tokens_string():
    """``max_tokens: "100"`` (string) is broken — Anthropic rejects it
    upstream; we should reject earlier."""
    with pytest.raises(HTTPException):
        validate_completion_request(
            {"model": "x", "messages": [{"role": "user", "content": "hi"}], "max_tokens": "100"},
            endpoint="messages",
        )


def test_validate_accepts_no_max_tokens():
    """Missing max_tokens is allowed (the proxy defaults to 4096
    for claude-oauth via _prepare_claude_oauth_request)."""
    validate_completion_request(
        {"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        endpoint="messages",
    )


def test_validate_happy_path_passes_silently():
    """A well-formed request must NOT raise."""
    validate_completion_request(
        {
            "model": "x-ai/grok-3",
            "max_tokens": 100,
            "messages": [
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "Hello"},
            ],
        },
        endpoint="messages",
    )


# ── sanitize_upstream_error ─────────────────────────────────────────


def test_sanitize_strips_traceback_lines():
    """BUG-007: 'Traceback (most recent call last):' line should be dropped."""
    raw = (
        "Traceback (most recent call last):\n"
        '  File "/usr/local/lib/python3.13/litellm/main.py", line 622, in acompletion\n'
        "    response = await init_response\n"
        "Exception: Invalid Message"
    )
    out = sanitize_upstream_error(raw)
    assert "Traceback" not in out
    assert "/usr/local" not in out
    assert "litellm/main.py" not in out
    assert "Invalid Message" in out


def test_sanitize_strips_inline_file_paths():
    raw = "Error in /usr/local/lib/python3.13/site-packages/litellm/foo.py:42 — bad input"
    out = sanitize_upstream_error(raw)
    assert "/usr/local" not in out
    assert "litellm/foo.py" not in out
    assert "bad input" in out


def test_sanitize_truncates_long_text():
    raw = "x" * 1000
    out = sanitize_upstream_error(raw, max_chars=100)
    assert len(out) == 100
    assert out.endswith("...")


def test_sanitize_handles_empty_input():
    assert sanitize_upstream_error("") == "upstream provider error (empty)"
    assert sanitize_upstream_error(None) == "upstream provider error (empty)"  # type: ignore[arg-type]


def test_sanitize_preserves_short_clean_messages():
    raw = "GeminiException BadRequestError: max_output_tokens must be positive"
    out = sanitize_upstream_error(raw)
    assert "max_output_tokens must be positive" in out
    assert len(out) < 200
