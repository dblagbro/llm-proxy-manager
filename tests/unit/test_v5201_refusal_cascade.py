"""v5.20.1 — Proxy-side on-refusal cascade.

DevinGPT team's 2026-07-05 memo ranked this #3 (highest-value ask).
When ``refusal_retry_enabled`` is on for the key AND the initial
response triggers a refusal pattern, cascade walks alternate providers
via LMRH-ranked selection with the current provider(s) excluded, until
one produces a clean response or ``refusal_retry_max_attempts`` is
exhausted.
"""
from __future__ import annotations
from pathlib import Path


# ---- module + wire pins ----

def test_cascade_module_present():
    p = Path("app/api/_refusal_cascade.py")
    assert p.exists()
    src = p.read_text()
    assert "async def maybe_cascade_on_refusal" in src
    assert "class CascadeResult" in src


def test_cascade_wired_in_messages():
    src = Path("app/api/messages.py").read_text()
    assert "maybe_cascade_on_refusal" in src
    assert "from app.api._refusal_cascade import" in src


def test_new_column_present_in_orm():
    src = Path("app/models/db_apikey.py").read_text()
    assert "refusal_retry_max_attempts = Column(Integer" in src


def test_new_column_added_via_alter_table():
    src = Path("app/models/database.py").read_text()
    assert "ADD COLUMN refusal_retry_max_attempts INTEGER" in src


# ---- cascade behavior pins ----

def test_cascade_emits_all_three_headers():
    """X-Refusal-Retry-Attempted / X-Refusal-Retry-Provider /
    X-Refusal-Chain-Exhausted MUST all be emitted so the caller has
    full attribution regardless of outcome."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert "X-Refusal-Retry-Attempted" in src
    assert "X-Refusal-Retry-Provider" in src
    assert "X-Refusal-Chain-Exhausted" in src


def test_cascade_writes_start_and_outcome_activity_rows():
    """No silent substitution: cascade start + per-attempt outcome +
    final outcome all get activity_log rows so the operator can trace
    what happened."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert 'event_type="refusal_retry_start"' in src or "'refusal_retry_start'" in src or '"refusal_retry_start"' in src
    assert "refusal_retry_success" in src
    assert "refusal_retry_exhausted" in src
    assert "refusal_retry_attempt_refused" in src


def test_cascade_reuses_v5200_detection_regex():
    """The cascade MUST reuse detect_refusal so patterns stay
    consistent between the response-tail detection and the cascade
    trigger. A drift would mean the cascade fires on responses the
    response-tail says are fine (or vice versa)."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert "from app.refusal_detection import" in src
    assert "detect_refusal" in src


def test_cascade_disabled_by_default():
    """The cascade wrapper MUST return the initial route/result
    unchanged when refusal_retry_enabled is off — no cost, no
    behavior change for keys that haven't opted in."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert 'getattr(key_record, "refusal_retry_enabled", False)' in src


def test_cascade_uses_exclude_provider_ids():
    """The retry loop MUST exclude every provider it's already tried
    so the LMRH ranker doesn't hand back the same one and the loop
    can't infinite-loop."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert "exclude_provider_ids=attempted_ids" in src


def test_max_attempts_defaults_to_three():
    src = Path("app/api/_refusal_cascade.py").read_text()
    assert "DEFAULT_MAX_ATTEMPTS = 3" in src


def test_cascade_only_runs_when_refusal_detected_on_initial():
    """If the initial response is clean, cascade must be a no-op
    (returns immediately with swapped=False, no LLM calls)."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    # The early-return pattern: after detect_refusal(initial), if
    # _match is None, return _no_op.
    assert "if _match is None" in src


def test_cascade_dispatch_closure_defined_in_messages():
    """messages.py provides the ``dispatch`` closure — the cascade
    module is dispatch-agnostic (takes the closure as a param) so it
    can be unit-tested without a real LLM."""
    src = Path("app/api/messages.py").read_text()
    assert "async def _cascade_dispatch(alt_route):" in src
    assert "acompletion_with_retry" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 1), (
        f"expected >= 5.20.1, got {major}.{minor}.{patch}"
    )
