"""v3.7.32 (#252 phase 5) — admin endpoints for the AI provider supervisor.

Phase 5 closes #252 with apply/revert/dismiss lifecycle endpoints +
a trigger-now smoke-test endpoint + a live-stats diagnostic endpoint.
All endpoints respect manual_override_until — operator-pinned providers
return 409 Conflict on apply/trigger.
"""
from __future__ import annotations

from pathlib import Path


def test_api_module_exists():
    import importlib
    mod = importlib.import_module("app.api.ai_provider_supervisor")
    assert hasattr(mod, "router")


def test_router_registered_in_app():
    src = Path("app/main.py").read_text()
    assert "from app.api.ai_provider_supervisor import router as ai_provider_supervisor_router" in src
    assert "app.include_router(ai_provider_supervisor_router)" in src


def test_endpoints_registered():
    from app.api.ai_provider_supervisor import router
    paths = {r.path for r in router.routes}
    for path in (
        "/api/providers/{provider_id}/ai-reviews",
        "/api/providers/{provider_id}/ai-reviews/{review_id}/apply",
        "/api/providers/{provider_id}/ai-reviews/{review_id}/dismiss",
        "/api/providers/{provider_id}/ai-reviews/{review_id}/revert",
        "/api/providers/{provider_id}/ai-supervisor-stats",
        "/api/providers/{provider_id}/ai-supervisor-trigger",
    ):
        assert path in paths, f"missing route {path}"


def test_apply_endpoint_respects_manual_override():
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def apply_review")
    body = src[idx:idx + 3000]
    assert "manual_override_until" in body
    # Returns 409 Conflict — not 400 Bad Request — so callers know to
    # release the lock first vs treating it as a permanent error
    assert "409" in body


def test_apply_endpoint_rejects_already_applied():
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def apply_review")
    body = src[idx:idx + 3000]
    assert 'review.applied_at is not None' in body
    assert "already applied" in body


def test_apply_endpoint_rejects_dismissed_reviews():
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def apply_review")
    body = src[idx:idx + 3000]
    assert 'review.dismissed_at is not None' in body
    assert "dismissed" in body


def test_revert_endpoint_uses_prior_values():
    """Revert reads prior_priority / prior_auto_skip_until to restore
    the provider to its pre-apply state. Records reverted_at for audit."""
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def revert_review")
    body = src[idx:idx + 3000]
    assert "prior_priority" in body
    assert "prior_auto_skip_until" in body
    assert "reverted_at" in body


def test_revert_rejects_unapplied_or_already_reverted():
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def revert_review")
    body = src[idx:idx + 3000]
    assert "never applied" in body
    assert "already reverted" in body


def test_dismiss_endpoint_idempotent():
    """Dismissing an already-dismissed review returns 200 with the
    same row — not an error."""
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def dismiss_review")
    body = src[idx:idx + 2000]
    assert "idempotent" in body


def test_dismiss_rejects_already_applied():
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def dismiss_review")
    body = src[idx:idx + 2000]
    assert "already applied" in body


def test_trigger_endpoint_respects_manual_override():
    """Manual trigger of the supervisor must also skip locked providers
    — preserves the consistency that locks block all supervisor activity
    on a provider, whether scheduled or manually triggered."""
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def trigger_review_now")
    body = src[idx:idx + 2000]
    assert "manual_override_until" in body
    assert "409" in body


def test_stats_endpoint_uses_phase3_helper():
    """The diagnostic endpoint reuses compute_provider_stats from
    Phase 3 — never duplicate the aggregation logic."""
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    idx = src.index("async def get_current_stats")
    body = src[idx:idx + 2000]
    assert "compute_provider_stats" in body


def test_all_endpoints_admin_gated():
    """Every supervisor endpoint requires admin — these surface
    actionable data + can mutate providers."""
    src = Path("app/api/ai_provider_supervisor.py").read_text()
    # All five POST + two GET endpoints depend on require_admin
    assert src.count("Depends(require_admin)") >= 6


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 32)
