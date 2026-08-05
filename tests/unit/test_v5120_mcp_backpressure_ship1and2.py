"""v5.12.0 — MCP capability back-pressure Ship 1+2 (from v5.10 design doc).

Operator-confirmed decisions (2026-06-30 interview):
- Wire format: dual-emit (X-Proxy-MCP-Suggestion header for REST + MCP
  notifications/message for Path A users with open /mcp connection).
  This file tests the REST half; MCP-native emission is a follow-up.
- Default threshold: 50 (= 0.5 in design-doc notation, ≈ ~3 refusals).
- Audit scope: api_key (compliance_policy_changes scope='per_key').

Ship 1 = X-Proxy-MCP-Suggestion header emission gated by score.
Ship 2 = caller_capability_score table + bump on refusal + 6h decay
worker.
"""
from __future__ import annotations

import importlib
from pathlib import Path


# ── (1) ORM model present + re-exported ────────────────────────────────


def test_caller_capability_score_model_exists():
    from app.models.db import CallerCapabilityScore
    assert CallerCapabilityScore.__tablename__ == "caller_capability_score"
    cols = {c.name for c in CallerCapabilityScore.__table__.columns}
    assert {"id", "api_key_id", "suggested_tool", "score", "last_bumped_at", "created_at"} <= cols


def test_orm_reexport_in_models_db_dunder_all():
    src = Path("app/models/db.py").read_text()
    assert '"CallerCapabilityScore"' in src or "'CallerCapabilityScore'" in src


# ── (2) Score module surface ───────────────────────────────────────────


def test_score_module_imports_and_constants():
    from app.capability_scout import score
    assert score.BUMP_AMOUNT == 20      # +0.2 in design notation
    assert score.SCORE_CAP == 100       # 1.0 in design notation
    assert score.DEFAULT_THRESHOLD == 50  # 0.5 chosen by operator
    assert score.GC_BELOW == 5


def test_score_module_exposes_required_functions():
    from app.capability_scout.score import (
        bump_score, best_suggestion_for_key, decay_all_scores,
        is_emission_enabled, _threshold,
    )
    # Just import-checks; behavior is exercised in integration.


# ── (3) Suggestion-emit middleware ─────────────────────────────────────


def test_suggestion_emit_module_imports():
    from app.capability_scout.suggestion_emit import (
        apply_suggestion_header, HEADER_NAME,
    )
    assert HEADER_NAME == "X-Proxy-MCP-Suggestion"


def test_suggestion_emit_records_audit_per_key_scope():
    """Audit row scope MUST be ``per_key`` (operator decision 2026-06-30
    — matches v5.1.2 retention-edit pattern). The reason MUST be
    ``mcp_suggestion_emitted`` so consumers can grep for it."""
    src = Path("app/capability_scout/suggestion_emit.py").read_text()
    assert 'scope="per_key"' in src
    assert 'reason="mcp_suggestion_emitted"' in src
    assert "target_id=api_key_id" in src


# ── (4) Wire-up on response handlers ───────────────────────────────────


def test_emit_wired_into_messages_handler():
    src = Path("app/api/messages.py").read_text()
    assert "from app.capability_scout.suggestion_emit import apply_suggestion_header" in src
    assert "await apply_suggestion_header(db, key_record.id, resp_headers)" in src


def test_emit_wired_into_completions_handler():
    src = Path("app/api/completions.py").read_text()
    assert "from app.capability_scout.suggestion_emit import apply_suggestion_header" in src


# ── (5) Score-bumping wired into scout emission ───────────────────────


def test_scout_bumps_score_on_emit():
    src = Path("app/capability_scout/scout.py").read_text()
    assert "from app.capability_scout.score import bump_score" in src
    assert "await bump_score(db, api_key_id, tool)" in src


# ── (6) Decay worker ───────────────────────────────────────────────────


def test_decay_worker_module_exists():
    from app.monitoring.caller_score_decay import (
        start_decay_worker, stop_decay_worker, SWEEP_INTERVAL_SEC,
    )
    assert SWEEP_INTERVAL_SEC == 6 * 60 * 60  # 6 hours


def test_decay_worker_started_in_main():
    src = Path("app/main.py").read_text()
    assert "caller_score_decay" in src
    assert "start_caller_score_decay" in src


# ── (7) Settings ───────────────────────────────────────────────────────


def test_settings_exposed():
    from app.config import settings
    assert hasattr(settings, "mcp_suggestion_emission_enabled")
    assert hasattr(settings, "mcp_suggestion_threshold")
    # Defaults match operator decision
    assert settings.mcp_suggestion_threshold == 50
    assert settings.mcp_suggestion_emission_enabled is True


# ── (8) Version ────────────────────────────────────────────────────────


def test_version_bumped():
    """v5.12.x line — exact patch version is pinned in the test file for
    each subsequent ship (e.g. v5.12.1, v5.12.2). Here we just assert
    the minor."""
    import re
    from app import __version__ as v
    importlib.reload(v)
    m = re.match(r'(\d+)\.(\d+)\.(\d+)', v.__version__)
    assert m and (int(m[1]), int(m[2]), int(m[3])) >= (5, 12, 0), "expected >= 5.12.0"
