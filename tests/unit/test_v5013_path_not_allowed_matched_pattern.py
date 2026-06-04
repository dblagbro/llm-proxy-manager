"""v5.0.13 — ``path_not_allowed`` audit rows carry the rejected path.

Pre-v5.0.13 the ``ComplianceEvent.matched_pattern`` column was NULL
for every ``path_not_allowed`` row, which meant an operator had to
grep nginx access logs to figure out which path was rejected. This
came up acutely on 2026-06-04 diagnosing the hub canary's
``/v1/v1/messages`` double-prefix bug — three round-trips of grep
that the audit row alone could have answered.

v5.0.13 threads the normalized rejected path through
``_emit_path_block_event`` → ``emit_event(matched_pattern=path)``. The
JSON 403 body already carried ``requested_path``; this just
mirrors that value into the audit table so the row is self-contained.

Static-pin tests catch any future change that drops the wiring.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.db import ApiKey, Base, ComplianceEvent
from app.middleware.allowed_paths import _emit_path_block_event


# ── Source guard ────────────────────────────────────────────────────


def test_middleware_threads_path_into_matched_pattern():
    """The emission site must pass ``matched_pattern=path``. If a
    future edit drops the kwarg, the column goes NULL again and we
    lose the audit-row self-containment."""
    src = Path("app/middleware/allowed_paths.py").read_text()
    assert "matched_pattern=path" in src, (
        "_emit_path_block_event no longer threads the rejected path "
        "into emit_event(matched_pattern=…). Restore it — otherwise "
        "every path_not_allowed audit row reverts to NULL and "
        "operators have to grep access logs to identify what was blocked."
    )


def test_audit_event_signature_still_accepts_matched_pattern():
    """``emit_event`` must keep the matched_pattern kwarg in its
    signature. Pinning this prevents an unrelated refactor from
    silently dropping the column write."""
    import inspect
    from app.compliance.audit import emit_event
    sig = inspect.signature(emit_event)
    assert "matched_pattern" in sig.parameters


# ── Behavioral ──────────────────────────────────────────────────────


async def _fresh_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest.mark.asyncio
async def test_path_not_allowed_row_carries_rejected_path():
    """End-to-end: calling the middleware's audit helper writes a row
    whose ``matched_pattern`` equals the rejected path string."""
    Session = await _fresh_db()
    # Seed the API key the audit row will reference (FK)
    async with Session() as db:
        db.add(ApiKey(
            id="key-v5013", name="t",
            key_hash=hashlib.sha256(b"sk-v5013").hexdigest(),
            key_prefix="sk-v501",
        ))
        await db.commit()

    # Patch AsyncSessionLocal so the helper's own session writes
    # against the in-memory DB.
    with patch("app.models.database.AsyncSessionLocal", Session):
        audit_id = await _emit_path_block_event(
            api_key_id="key-v5013",
            path="/v1/v1/messages",   # the hub canary's double-prefix bug
            ua="claude-cli/2.1.118 (external, sdk-cli)",
        )

    assert audit_id and audit_id.startswith("comp_")

    # Confirm the row landed with the rejected path in matched_pattern
    async with Session() as db:
        from sqlalchemy import select
        row = (await db.execute(
            select(ComplianceEvent).where(ComplianceEvent.audit_id == audit_id)
        )).scalar_one()

    assert row.event_type == "path_not_allowed"
    assert row.reason_code == "path-not-in-allowed_paths"
    assert row.http_status == 403
    assert row.matched_pattern == "/v1/v1/messages", (
        f"v5.0.13 regression: matched_pattern={row.matched_pattern!r}, "
        "expected the rejected request path"
    )
    assert row.client_user_agent == "claude-cli/2.1.118 (external, sdk-cli)"
