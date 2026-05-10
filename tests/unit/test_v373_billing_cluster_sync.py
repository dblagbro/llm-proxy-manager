"""v3.7.3 — cluster-sync replication of billing/auto-rotation fields tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.cluster.sync import _parse_iso_or_none


# ── _parse_iso_or_none ────────────────────────────────────────────


def test_parse_iso_handles_none():
    assert _parse_iso_or_none(None) is None
    assert _parse_iso_or_none("") is None


def test_parse_iso_handles_z_suffix():
    dt = _parse_iso_or_none("2026-05-10T20:00:00Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.tzinfo is not None


def test_parse_iso_handles_offset():
    dt = _parse_iso_or_none("2026-05-10T20:00:00+00:00")
    assert dt is not None
    assert dt.month == 5


def test_parse_iso_handles_naive():
    dt = _parse_iso_or_none("2026-05-10T20:00:00")
    assert dt is not None
    # No tz in input → naive datetime ok
    assert dt.year == 2026


def test_parse_iso_returns_none_on_garbage():
    assert _parse_iso_or_none("not-a-date") is None


def test_parse_iso_passes_through_datetime():
    """If a datetime is already passed (shouldn't happen, but be defensive)."""
    now = datetime.now(timezone.utc)
    assert _parse_iso_or_none(now) == now


# ── cluster-sync wiring regression ────────────────────────────────


def test_build_payload_includes_billing_fields():
    """v3.6.x/v3.7.x Provider fields must be in the cluster sync
    payload so peer nodes see the auto-rotation skip decisions."""
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    # Identifier + capture timestamp + auto-rotation outcome (NOT the
    # cookies themselves — those stay on capture node)
    assert "anthropic_org_uuid" in src
    assert "anthropic_session_captured_at" in src
    assert "auto_skip_until" in src
    assert "auto_skip_reason" in src


def test_build_payload_does_not_include_cookies():
    """Cookies are auth material — must NOT be in the cluster sync
    payload. They stay on the node where the operator pasted them.

    Check the JSON-key shape ``"anthropic_session_cookies":`` rather
    than the bare field name, so an explanatory comment mentioning
    the field doesn't false-positive.
    """
    from pathlib import Path
    src = Path("app/cluster/manager.py").read_text()
    start = src.index("providers_result = await db.execute")
    end = src.index("# Only push settings")
    providers_block = src[start:end]
    assert '"anthropic_session_cookies":' not in providers_block, (
        "anthropic_session_cookies should NOT be in the cluster sync "
        "payload — auth material stays on capture node"
    )


def test_apply_pass_updates_billing_fields_on_existing_rows():
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    # The update path (else branch — existing row found)
    assert 'existing.anthropic_org_uuid = p_data.get("anthropic_org_uuid")' in src
    assert 'existing.auto_skip_reason = p_data.get("auto_skip_reason")' in src
    # auto_skip_until needs the ISO parse — check both branches present
    assert "datetime.fromisoformat" in src


def test_apply_pass_creates_new_rows_with_billing_fields():
    """New peer-imported rows must include the new fields so a fresh
    node doesn't lose the auto-skip state mid-replication."""
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    # Provider(...) constructor block at the row-insert path
    insert_marker = "owned_by_key_id=p_data.get(\"owned_by_key_id\")"
    insert_idx = src.index(insert_marker)
    # Look ~30 lines forward for the new fields
    snippet = src[insert_idx:insert_idx + 1500]
    assert "anthropic_org_uuid=p_data.get" in snippet
    assert "anthropic_session_captured_at=p_data.get" in snippet
    assert "auto_skip_until=_parse_iso_or_none" in snippet
    assert "auto_skip_reason=p_data.get" in snippet


def test_apply_pass_does_not_overwrite_local_cookies():
    """If a peer's payload doesn't carry cookies (it shouldn't), the
    local node's cookies must NOT be cleared.

    Invariant: the apply pass never WRITES to
    ``Provider.anthropic_session_cookies``. We check for the
    write-pattern ``= p_data.get("anthropic_session_cookies")`` and
    ``existing.anthropic_session_cookies =``, both of which must be
    absent. The bare field name MAY appear in comments (we
    explicitly document the intentional non-replication).
    """
    from pathlib import Path
    src = Path("app/cluster/sync.py").read_text()
    forbidden_patterns = [
        'anthropic_session_cookies=p_data',
        'anthropic_session_cookies = p_data',
        'existing.anthropic_session_cookies =',
        'existing.anthropic_session_cookies=',
    ]
    for pat in forbidden_patterns:
        assert pat not in src, f"cookies must not be written by sync apply (found pattern: {pat!r})"
