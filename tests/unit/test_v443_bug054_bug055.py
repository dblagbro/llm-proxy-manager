"""v4.4.3 BUG-054 + BUG-055 regression tests.

BUG-054: production frontend/index.html had Vite scaffold title
"frontend". Trivial UX leak — browser tab shows "frontend" instead of
something meaningful. Fix is a single-token title change.

BUG-055: activity_log accumulates orphan FK refs because provider /
api_key tombstones get hard-deleted after
``provider_tombstone_retention_days`` (default 7), but the activity
log rows that referenced them stay. Audit on 2026-05-20 found 438
orphan provider_ids + 7,937 orphan api_key_ids on www1. Fix adds
``_prune_activity_log_orphans()`` to the daily sweep, run after the
tombstone prune.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


# ── BUG-054: frontend title source-level check ──────────────────


def test_bug054_frontend_html_title_is_not_vite_scaffold():
    """The Vite default is ``<title>frontend</title>``. Any value
    other than that — anything operator-meaningful — is acceptable.
    Source-level so the test stays green regardless of which copy
    we ship in the v4.4.3 fix."""
    src = Path("frontend/index.html").read_text()
    # Extract title
    import re
    m = re.search(r"<title>([^<]*)</title>", src)
    assert m is not None, "frontend/index.html must have a <title> element"
    title = m.group(1).strip()
    assert title.lower() != "frontend", \
        f"frontend/index.html still has Vite scaffold title (got {title!r})"
    assert len(title) > 0, "<title> must not be empty"


# ── BUG-055: activity_log orphan prune ──────────────────────────


def test_bug055_orphan_prune_helper_exists():
    """``_prune_activity_log_orphans()`` is defined in prune.py and
    referenced from ``_sweep_once()``."""
    from app.monitoring import prune
    assert hasattr(prune, "_prune_activity_log_orphans"), \
        "BUG-055 fix must expose _prune_activity_log_orphans()"


def test_bug055_orphan_prune_wired_into_sweep():
    """Source-level: the sweep dispatches the orphan prune AFTER the
    tombstone prunes (the orphan-creation step). Without that
    ordering, the first sweep pass after a tombstone hard-delete
    would leave orphans for an extra day."""
    src = Path("app/monitoring/prune.py").read_text()
    body = src[src.index("async def _sweep_once"):src.index("async def _prune_loop")]
    assert "_prune_activity_log_orphans" in body, \
        "_sweep_once must call _prune_activity_log_orphans"
    # Ordering check: orphan call comes after tombstone calls
    tombstone_idx = body.index("_prune_provider_tombstones")
    orphan_idx = body.index("_prune_activity_log_orphans")
    assert orphan_idx > tombstone_idx, \
        "orphan prune must run AFTER tombstone prune (tombstones " \
        "create new orphans)"


def test_bug055_sweep_output_dict_has_orphan_counter():
    """The ``out`` dict in _sweep_once includes the new counter so
    operators see it in the prune.swept log line + the get_last_sweep
    API."""
    src = Path("app/monitoring/prune.py").read_text()
    sweep_body = src[src.index("async def _sweep_once"):]
    assert '"activity_log_orphans"' in sweep_body, \
        "_sweep_once must initialize activity_log_orphans key"


def test_bug055_sweep_log_line_includes_orphan_count():
    """The post-sweep INFO log line must include the orphan count so
    operators tailing logs can see how many dangling refs were
    cleaned up each sweep."""
    src = Path("app/monitoring/prune.py").read_text()
    # Find the actual logger.info call (not a docstring mention).
    idx = src.index('"prune.swept activity_log=%d')
    body = src[idx:idx + 1500]
    assert "activity_log_orphans" in body


# ── BUG-055: behavioral test against a real SQLite DB ───────────


@pytest_asyncio.fixture
async def fresh_db():
    """Reuse the module-level engine; ensure schema; clean ApiKey + Provider + ActivityLog rows."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ApiKey, Provider, ActivityLog

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ActivityLog))
        await cleanup.execute(delete(ApiKey))
        await cleanup.execute(delete(Provider))
        await cleanup.commit()
    yield AsyncSessionLocal
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(delete(ActivityLog))
        await cleanup.execute(delete(ApiKey))
        await cleanup.execute(delete(Provider))
        await cleanup.commit()


@pytest.mark.asyncio
async def test_bug055_orphan_prune_deletes_dangling_refs(fresh_db):
    """End-to-end: seed activity_log with rows whose provider_id /
    api_key_id reference non-existent rows; call the prune helper;
    assert orphans are deleted and rows pointing at live FKs survive."""
    from app.monitoring.prune import _prune_activity_log_orphans
    from app.models.db import ActivityLog, ApiKey, Provider

    async with fresh_db() as db:
        # 1 live provider, 1 live api_key
        db.add(Provider(
            id="prov-live", name="live", provider_type="openai",
            priority=10, enabled=True, extra_config={},
        ))
        db.add(ApiKey(
            id="key-live", name="live", key_hash="x", key_prefix="llmp-z",
            enabled=True,
        ))
        # Seed activity_log:
        #   3 rows pointing at live FKs (should survive)
        #   2 rows pointing at non-existent provider_id (orphan)
        #   2 rows pointing at non-existent api_key_id (orphan)
        #   1 row pointing at BOTH non-existent (orphan, must not be
        #     double-counted as 2 deletes)
        now = datetime.now(timezone.utc)
        db.add_all([
            ActivityLog(event_type="x", severity="info", message="live1",
                        provider_id="prov-live", api_key_id="key-live", created_at=now),
            ActivityLog(event_type="x", severity="info", message="live2",
                        provider_id="prov-live", created_at=now),
            ActivityLog(event_type="x", severity="info", message="live3",
                        api_key_id="key-live", created_at=now),
            ActivityLog(event_type="x", severity="info", message="orphan-prov-1",
                        provider_id="prov-gone-1", created_at=now),
            ActivityLog(event_type="x", severity="info", message="orphan-prov-2",
                        provider_id="prov-gone-2", api_key_id="key-live", created_at=now),
            ActivityLog(event_type="x", severity="info", message="orphan-key-1",
                        api_key_id="key-gone-1", created_at=now),
            ActivityLog(event_type="x", severity="info", message="orphan-key-2",
                        provider_id="prov-live", api_key_id="key-gone-2", created_at=now),
            ActivityLog(event_type="x", severity="info", message="orphan-both",
                        provider_id="prov-gone-3", api_key_id="key-gone-3", created_at=now),
        ])
        await db.commit()

    deleted = await _prune_activity_log_orphans()

    # 5 orphans total: prov-gone-1, prov-gone-2, key-gone-1, key-gone-2, both
    # The "both" row is deleted by the first (provider) pass, so the
    # second (api_key) pass doesn't see it — total deletions = 5
    # NOT 6 (no double-count).
    assert deleted == 5, f"expected 5 orphans deleted, got {deleted}"

    async with fresh_db() as db:
        surviving = (await db.execute(
            select(ActivityLog.message).order_by(ActivityLog.message)
        )).scalars().all()
        assert sorted(surviving) == ["live1", "live2", "live3"], \
            f"only live-FK rows must survive, got {surviving!r}"


@pytest.mark.asyncio
async def test_bug055_orphan_prune_no_op_when_clean(fresh_db):
    """If there are no orphans, the prune is a no-op and returns 0
    (doesn't hammer the DB needlessly)."""
    from app.monitoring.prune import _prune_activity_log_orphans
    from app.models.db import ActivityLog, ApiKey, Provider

    async with fresh_db() as db:
        db.add(Provider(
            id="prov-clean", name="c", provider_type="openai",
            priority=10, enabled=True, extra_config={},
        ))
        db.add(ApiKey(
            id="key-clean", name="c", key_hash="x", key_prefix="llmp-c",
            enabled=True,
        ))
        db.add(ActivityLog(
            event_type="x", severity="info", message="ok",
            provider_id="prov-clean", api_key_id="key-clean",
            created_at=datetime.now(timezone.utc),
        ))
        await db.commit()

    deleted = await _prune_activity_log_orphans()
    assert deleted == 0
