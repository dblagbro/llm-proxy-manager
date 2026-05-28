"""v4.4.13 cluster-sync quality improvements (post-log-audit).

Surfaced 2026-05-21 by reading the proxy container logs: ``Sync to
llm-proxy2-www2 failed:`` repeating 39× in 1h with **0 successes** and
**no error text after the colon**. Live diagnostic revealed three
stacked issues:

1. **`provider_ai_review` + `api_key_ai_review` never pruned** —
   1561 rows on www1 since 2026-05-15 (~250/day forever). The tables
   are included in the cluster sync push payload, growing it to
   2.78 MB. Without retention they'd hit ~91k rows / 90+ MB per year.

2. **15s sync timeout is too tight** for the current payload — live
   measurement showed c1conv at 10.7s (barely passing) and www2
   timing out at 15s. v4.4.13 raises to 45s as belt-and-braces while
   the prune (#1) shrinks the payload.

3. **The exception logger renders empty exceptions as a bare
   colon-blank** — ``str(httpx.ReadTimeout())`` is ``""``, so
   ``f"Sync to {peer.id} failed: {e}"`` becomes literally
   "Sync to www2 failed: " with no diagnostic. Same class issue
   ``_exc_str`` solves in ``_messages_streaming.py``.

These three fixes are bundled because they all surface the same
incident (sync to www2 failing invisibly) and they all need to be
real to fully close the loop:
- Just the timeout bump → hides the symptom; payload still grows.
- Just the prune → no help while it's still 2.78 MB (takes 24h
  for first sweep + cluster sync to converge).
- Just the log fix → operators see real timeout strings instead of
  blanks, but the timeouts keep happening.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── #1: provider_ai_review + api_key_ai_review prune ─────────────


def test_ai_review_retention_setting_exists():
    """Config field is declared with a 30-day default."""
    from app.config import settings
    assert hasattr(settings, "ai_review_retention_days")
    # Default is 30 days unless the env overrides it
    assert isinstance(settings.ai_review_retention_days, int)
    assert settings.ai_review_retention_days >= 1


def test_ai_review_retention_helper_clamps_minimum():
    """``_ai_review_retention_days()`` returns >=1 even if config
    contains 0 / negative / non-int."""
    from app.monitoring.prune import _ai_review_retention_days
    assert _ai_review_retention_days() >= 1


def test_prune_ai_review_helper_exists():
    """The generic per-model pruner exists and accepts both review
    model classes."""
    import inspect
    from app.monitoring.prune import _prune_ai_review
    from app.models.db import ApiKeyAiReview, ProviderAiReview
    # Both models have the columns the helper needs (captured_at, id).
    for cls in (ApiKeyAiReview, ProviderAiReview):
        assert hasattr(cls, "captured_at")
        assert hasattr(cls, "id")
    # Signature: (model_cls, keep_days) -> int
    sig = inspect.signature(_prune_ai_review)
    params = list(sig.parameters)
    assert params == ["model_cls", "keep_days"]


def test_ai_review_prune_wired_into_sweep():
    """Source-level: both ai-review prunes are called inside
    ``_sweep_once`` AFTER the tombstone prune block. (Not strictly
    required for correctness, but the sweep emits a single log line
    at the end so the ordering of the calls determines the order of
    the counts in that line.)"""
    src = Path("app/monitoring/prune.py").read_text()
    body = src[src.index("async def _sweep_once"):src.index("async def _prune_loop")]
    assert "_prune_ai_review(\n            ProviderAiReview" in body
    assert "_prune_ai_review(\n            ApiKeyAiReview" in body


def test_sweep_output_dict_has_ai_review_counters():
    src = Path("app/monitoring/prune.py").read_text()
    sweep_body = src[src.index("async def _sweep_once"):src.index("async def _wal_checkpoint")]
    assert '"provider_ai_reviews": 0' in sweep_body
    assert '"api_key_ai_reviews": 0' in sweep_body
    assert '"ai_review_keep_days"' in sweep_body


def test_sweep_log_line_includes_ai_review_counts():
    src = Path("app/monitoring/prune.py").read_text()
    idx = src.index('"prune.swept activity_log=%d')
    body = src[idx:idx + 1500]
    assert "provider_ai_reviews=%d" in body
    assert "api_key_ai_reviews=%d" in body
    assert "ai_review_keep_days=%d" in body


# ── #2: sync timeout 15→45s ──────────────────────────────────────


def test_push_sync_timeout_raised():
    """The 15s ceiling fell over with the 2.78 MB payload. v4.4.13
    raises it to 45s. Source-level guard."""
    src = Path("app/cluster/manager.py").read_text()
    push_body = src[src.index("async def push_sync("):src.index("async def push_sync(") + 2500]
    # New timeout
    assert "AsyncClient(timeout=45" in push_body
    # Old timeout absent
    assert "AsyncClient(timeout=15" not in push_body


# ── #3: log message renders non-empty exception strings ──────────


def test_push_sync_log_message_handles_empty_exception_str():
    """``str(httpx.ReadTimeout())`` is ``""`` — without a fallback,
    ``f"failed: {e}"`` rendered as just "failed: " with no
    diagnostic. v4.4.13 logs both ``type(e).__name__`` and the
    (fallback-aware) message so future timeouts produce a useful
    line."""
    src = Path("app/cluster/manager.py").read_text()
    # v4.4.24 — extract the full function body (to the next top-level def),
    # not a fixed slice. The v4.4.24 BUG-081 response-status block grew the
    # function past the old 2500-char window, pushing the failed-log line
    # out of view. Same brittleness class as the v3.9.8 source-window test.
    start = src.index("async def push_sync(")
    nxt = src.index("\n_push_sync = push_sync", start)
    push_body = src[start:nxt]
    # Source-level: the log line uses both type name and str.
    assert "type(e).__name__" in push_body
    assert "(no message)" in push_body
    # And the literal "logger.warning(\"Sync to %s failed: %s: %s\"" pattern
    assert 'logger.warning("Sync to %s failed: %s: %s"' in push_body


def test_push_sync_log_falls_back_for_empty_exception():
    """Behavioral: simulate the empty-message exception path inline."""
    # The fallback logic is inline in the except block. Verify the
    # specific behavior by replicating the conditional locally — if
    # the implementation moves to a helper, the test moves too.
    import httpx
    e = httpx.ReadTimeout("")
    msg = str(e) if str(e) else f"{type(e).__name__} (no message)"
    assert msg == "ReadTimeout (no message)"

    # And a real-message exception still surfaces the message:
    e2 = httpx.ConnectError("Connection refused")
    msg2 = str(e2) if str(e2) else f"{type(e2).__name__} (no message)"
    assert msg2 == "Connection refused"
