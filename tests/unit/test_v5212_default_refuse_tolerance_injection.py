"""v5.21.2 — Per-key default LMRH refuse-tolerance injection.

Static-grep pins for the injection wire between the api_keys column,
the messages.py handler, the admin PATCH surface, and the list-response.
"""
from __future__ import annotations
from pathlib import Path


def test_schema_migration_column_present():
    src = Path("app/models/database.py").read_text()
    assert "ADD COLUMN default_refuse_tolerance TEXT" in src


def test_orm_column_declared():
    src = Path("app/models/db_apikey.py").read_text()
    assert "default_refuse_tolerance = Column" in src


def test_admin_patch_accepts_field():
    src = Path("app/api/apikeys.py").read_text()
    assert "default_refuse_tolerance: Optional[str] = None" in src


def test_admin_patch_vocab_validated():
    """Unknown values must NOT persist — the taxonomy is fixed at
    strict/default/lenient. Stale-frontend garbage should drop to NULL."""
    src = Path("app/api/apikeys.py").read_text()
    assert 'if val in ("strict", "default", "lenient")' in src


def test_list_response_exposes_field():
    src = Path("app/api/apikeys.py").read_text()
    assert '"default_refuse_tolerance"' in src


def test_messages_handler_injects_when_missing():
    """The messages handler must inject the per-key default ONLY when
    the caller didn't already specify a refuse-tolerance dim in their
    LMRH-Hint header."""
    src = Path("app/api/messages.py").read_text()
    assert "_key_rt_default" in src
    # Injection is gated on absence of caller's dim
    assert 'refuse-tolerance=' in src
    assert 'X-LMRH-Injected-Dim' in src


def test_caller_hint_wins_over_default():
    """If the caller passed their own refuse-tolerance dim, the per-key
    default MUST NOT overwrite it. Check the gate looks for the
    caller's dim before injecting."""
    src = Path("app/api/messages.py").read_text()
    handler_start = src.find("apply_privacy_filters(messages_list")
    handler_body = src[handler_start:handler_start + 3000]
    # The check pattern that ensures we skip injection when caller
    # already sent their own value:
    assert '.find("refuse-tolerance=") < 0' in handler_body or \
           '"refuse-tolerance=" not in' in handler_body


def test_frontend_type_declared():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "default_refuse_tolerance" in src


def test_frontend_editor_wired():
    src = Path("frontend/src/components/keys/ComplianceFieldsEditor.tsx").read_text()
    # Prop declared + select rendered
    assert "defaultRefuseTolerance" in src
    assert "Default refuse-tolerance" in src
    for v in ('"strict"', '"default"', '"lenient"'):
        assert v in src
