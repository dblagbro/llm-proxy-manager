"""v3.8.2 — proactive findings shipped together (#260 + #261 + #262).

#260: Add classifier patterns for "Invalid user message" (caller-side
      malformed messages array from litellm) so they bucket as
      bad_request instead of unknown.
#261: Backfill manual_override_until on pre-v3.7.28 disabled providers
      so the AI supervisor (when enabled) doesn't auto-re-enable them.
#262: Guard to_anthropic_response() against empty choices[] arrays
      that gemini safety-block paths produce — surface a clear error
      instead of the generic IndexError that bucketed as unknown.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


# ── #260: classifier patterns ──────────────────────────────────────


def test_classify_invalid_user_message_as_bad_request():
    from app.routing.circuit_breaker import classify_error
    # Real upstream errors observed in 24h scan
    samples = [
        "litellm.APIConnectionError: Invalid user message at index 2",
        "litellm.APIConnectionError: APIConnectionError: XaiException - Invalid user message at index 4",
        "litellm.APIConnectionError: APIConnectionError: OpenrouterException - Invalid user message at index 1",
    ]
    for s in samples:
        assert classify_error(s) == "bad_request", f"{s!r} -> {classify_error(s)!r}"


def test_classify_empty_choices_as_bad_request():
    from app.routing.circuit_breaker import classify_error
    assert classify_error(
        "ValueError: upstream returned no choices — likely safety-block or refusal"
    ) == "bad_request"


def test_classify_other_errors_unchanged():
    """Regression check: the new patterns don't accidentally swallow
    legitimate auth / rate_limit / upstream_5xx errors."""
    from app.routing.circuit_breaker import classify_error
    assert classify_error("401 Unauthorized") == "auth"
    assert classify_error("429 Too Many Requests") == "rate_limit"
    assert classify_error("500 Internal Server Error") == "upstream_5xx"
    assert classify_error("timed out after 30s") == "timeout"


# ── #261: manual_override_until backfill migration ────────────────


def test_backfill_migration_present():
    src = Path("app/models/database.py").read_text()
    # The UPDATE statement should be in the migrations list
    assert "UPDATE providers SET manual_override_until='9999-12-31 23:59:59'" in src
    # Conditions: enabled=0 AND manual_override_until IS NULL
    assert "enabled=0" in src
    assert "manual_override_until IS NULL" in src


def test_backfill_migration_safe_to_rerun():
    """The migration's WHERE clause filters on manual_override_until IS NULL,
    so re-running it produces a no-op on already-backfilled rows."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("UPDATE providers SET manual_override_until")
    stmt = src[idx:idx + 300]
    assert "manual_override_until IS NULL" in stmt


def test_backfill_migration_does_not_touch_deleted_rows():
    """Soft-deleted providers shouldn't be touched — they're tombstones
    awaiting cluster-sync GC. Backfilling manual_override on them would
    block the v3.0.13 tombstone GC path."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("UPDATE providers SET manual_override_until")
    stmt = src[idx:idx + 300]
    assert "deleted_at IS NULL" in stmt


# ── #262: to_anthropic_response empty-choices guard ──────────────


def test_to_anthropic_response_raises_on_empty_choices():
    """The Phase 1 fix from gemini safety-block diagnosis. Empty
    choices[] now raises a classifier-recognized ValueError instead
    of bubbling as a generic IndexError."""
    from app.cot.sse import to_anthropic_response

    fake_response = MagicMock()
    fake_response.choices = []  # empty — what gemini sometimes returns
    with pytest.raises(ValueError) as ex:
        to_anthropic_response(fake_response)
    assert "no choices" in str(ex.value)


def test_to_anthropic_response_raises_on_none_choices():
    """Some response objects use None instead of empty list."""
    from app.cot.sse import to_anthropic_response

    fake_response = MagicMock()
    fake_response.choices = None
    with pytest.raises(ValueError):
        to_anthropic_response(fake_response)


def test_to_anthropic_response_happy_path_unchanged():
    """Normal (non-empty choices) path must still work — the guard is
    additive, not a behavior change for legitimate responses."""
    from app.cot.sse import to_anthropic_response

    fake_msg = MagicMock()
    fake_msg.content = "hello"
    fake_msg.tool_calls = None
    fake_choice = MagicMock()
    fake_choice.finish_reason = "stop"
    fake_choice.message = fake_msg
    fake_response = MagicMock()
    fake_response.choices = [fake_choice]
    fake_response.usage.prompt_tokens = 10
    fake_response.usage.completion_tokens = 5
    fake_response.id = "msg_test"
    fake_response.model = "test-model"
    # Ensure prompt_tokens_details path returns no cache tokens
    fake_response.usage.prompt_tokens_details = None
    out = to_anthropic_response(fake_response)
    assert out["content"][0]["text"] == "hello"
    assert out["stop_reason"] == "end_turn"
    assert out["model"] == "test-model"


# ── Cross-fix consistency ──────────────────────────────────────────


def test_empty_choices_error_message_matches_classifier_pattern():
    """The error string raised by to_anthropic_response MUST contain a
    substring the classifier recognizes — otherwise the bucketing fix
    doesn't help. End-to-end sanity check on the contract."""
    from app.cot.sse import to_anthropic_response
    from app.routing.circuit_breaker import classify_error

    fake_response = MagicMock()
    fake_response.choices = []
    try:
        to_anthropic_response(fake_response)
        assert False, "expected ValueError"
    except ValueError as e:
        assert classify_error(str(e)) == "bad_request", (
            f"empty-choices error must classify as bad_request, "
            f"got {classify_error(str(e))!r}"
        )


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 2)
