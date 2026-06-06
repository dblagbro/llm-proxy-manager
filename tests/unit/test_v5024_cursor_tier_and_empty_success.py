"""v5.0.24 / remediation Batch 3 — Cursor membership-tier downgrade
detection + generic empty-success guard (BUG-053).

Operator-confirmed behavior (Q1 answer 2026-06-05):
  - Cursor account is Free now but will upgrade later.
  - Support BOTH tiers gracefully.

Implementation:
  1. Cursor billing scrape captures ``membership_tier`` in the
     ExternalUsageSnapshot row.
  2. On Pro→Free downgrade, the next scrape sets the Provider's
     ``auto_skip_until = now + 24h`` so routing skips Cursor.
  3. On Free→Pro upgrade, the auto-skip is cleared if it was set by
     our downgrade marker (idempotent — never overwrites a
     human-set skip).
  4. Generic empty-success guard catches the bridge's masking pattern
     (HTTP 200 with empty content + zero tokens + embedded error)
     and converts to HTTP 502 so callers see the real failure and
     the circuit breaker fires.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest


# ── Schema pins ─────────────────────────────────────────────────────


def test_external_usage_snapshot_has_membership_tier():
    from app.models.db import ExternalUsageSnapshot
    cols = {c.name for c in ExternalUsageSnapshot.__table__.columns}
    assert "membership_tier" in cols, (
        "ExternalUsageSnapshot.membership_tier missing — BUG-053 "
        "downgrade detection has nothing to compare against."
    )


def test_alter_table_external_usage_snapshot_in_bootstrap():
    src = Path("app/models/database.py").read_text()
    assert (
        "ALTER TABLE external_usage_snapshot ADD COLUMN membership_tier"
        in src
    )


# ── Source pins ─────────────────────────────────────────────────────


def test_cursor_billing_parses_membership_type():
    src = Path("app/providers/cursor_billing.py").read_text()
    assert 'summary.get("membershipType")' in src
    assert '"membership_tier"' in src


def test_cursor_billing_downgrade_sets_auto_skip():
    """Downgrade logic must set Provider.auto_skip_until + a clear
    skip reason that future scrapes can recognize for the upgrade
    auto-clear path."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "auto_skip_until = datetime.utcnow() + timedelta(hours=24)" in src
    assert "cursor membership downgraded" in src
    assert 'membership_tier.is_not(None)' in src, (
        "prev-tier lookup must only consider snapshots that captured "
        "membership_tier (early v5.0.24 rows had NULL)."
    )


def test_cursor_billing_upgrade_clears_only_our_skip():
    """Upgrade clears only the auto-skip whose reason matches the
    downgrade marker. Operator-set skips MUST NOT be auto-cleared."""
    src = Path("app/providers/cursor_billing.py").read_text()
    assert "startswith(" in src and "cursor membership downgraded" in src


def test_response_validator_module_exists():
    """The empty-success guard module must exist and expose the two
    public callables wired in completions.py."""
    from app.api._response_validators import (
        looks_like_empty_success_failure,
        empty_success_failure_message,
    )
    assert callable(looks_like_empty_success_failure)
    assert callable(empty_success_failure_message)


def test_completions_wires_empty_success_guard():
    src = Path("app/api/completions.py").read_text()
    assert "from app.api._response_validators import" in src
    assert "looks_like_empty_success_failure" in src
    assert "raise HTTPException(502" in src


# ── Behavioral: validator ───────────────────────────────────────────


def test_validator_flags_cursor_empty_success_pattern():
    """The exact pattern observed in BUG-053: HTTP 200 with empty
    content + zero tokens + ERROR_RATE_LIMITED_CHANGEABLE in the
    body."""
    from app.api._response_validators import (
        looks_like_empty_success_failure,
    )
    resp = {
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0,
                  "total_tokens": 0},
    }
    raw = ('{"error":{"code":"resource_exhausted",'
           '"details":[{"debug":{"error":'
           '"ERROR_RATE_LIMITED_CHANGEABLE"}}]}}')
    assert looks_like_empty_success_failure(
        response_dict=resp, raw_body=raw
    )


def test_validator_does_not_flag_real_success():
    from app.api._response_validators import (
        looks_like_empty_success_failure,
    )
    resp = {
        "choices": [{"message": {"content": "Hello"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1,
                  "total_tokens": 6},
    }
    assert not looks_like_empty_success_failure(response_dict=resp)


def test_validator_does_not_flag_legit_empty_no_marker():
    """A completion that returned an empty string with finish_reason=
    stop, real prompt tokens recorded, and NO error marker is a
    legitimate (if odd) response. Must not be flagged."""
    from app.api._response_validators import (
        looks_like_empty_success_failure,
    )
    resp = {
        "choices": [{"message": {"content": ""}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0,
                  "total_tokens": 5},
    }
    assert not looks_like_empty_success_failure(response_dict=resp)


def test_validator_flags_anthropic_shape_with_error():
    """Anthropic /v1/messages shape: content is list[{type,text}]."""
    from app.api._response_validators import (
        looks_like_empty_success_failure,
    )
    resp = {
        "content": [{"type": "text", "text": ""}],
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "error": {"code": "resource_exhausted", "message": "limit"},
    }
    assert looks_like_empty_success_failure(response_dict=resp)
