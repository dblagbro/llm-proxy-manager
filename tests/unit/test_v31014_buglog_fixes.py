"""Tests for the v3.10.14 bug-log fixes — BUG-026 and BUG-033."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete


# ── BUG-033: tool_result image is visible, not silently dropped ──────────────

def test_bug033_tool_result_image_marker_is_descriptive():
    from app.api._oauth_chat_translate import _tool_result_content_to_str
    content = [{
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }]
    out = _tool_result_content_to_str(content)
    assert out != "[image]", "the dropped image must not be a silent bare marker"
    assert "image/png" in out, "marker should name the media type"
    assert "omitted" in out.lower()


def test_bug033_tool_result_text_still_passes_through():
    from app.api._oauth_chat_translate import _tool_result_content_to_str
    content = [{"type": "text", "text": "the tool said hello"}]
    assert _tool_result_content_to_str(content) == "the tool said hello"


# ── BUG-026: supervisor stats exclude internal-source traffic ────────────────

@pytest_asyncio.fixture
async def db_ready():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base, ActivityLog

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as cleanup:
        await cleanup.execute(
            delete(ActivityLog).where(ActivityLog.provider_id == "bug026-prov")
        )
        await cleanup.commit()
    yield AsyncSessionLocal


@pytest.mark.asyncio
async def test_bug026_compute_provider_stats_excludes_internal_source(db_ready):
    from app.monitoring.ai_provider_supervisor_stats import compute_provider_stats
    from app.models.db import ActivityLog

    pid = "bug026-prov"
    async with db_ready() as db:
        # two real user calls + one internal AI-supervisor classifier call
        db.add(ActivityLog(event_type="llm_request", severity="info",
                            provider_id=pid, event_meta={"out_tok": 10}))
        db.add(ActivityLog(event_type="llm_request", severity="info",
                            provider_id=pid, event_meta={"out_tok": 10}))
        db.add(ActivityLog(event_type="llm_request", severity="error",
                            provider_id=pid,
                            event_meta={"internal_source": "ai_provider_supervisor"}))
        await db.commit()

    async with db_ready() as db:
        stats = await compute_provider_stats(
            db, pid, short_window_min=60, long_window_days=1,
        )
    # the internal-source row (and its error severity) must be excluded
    assert stats["short_window"]["requests"] == 2, (
        "internal-source classifier calls must not count toward provider stats"
    )
