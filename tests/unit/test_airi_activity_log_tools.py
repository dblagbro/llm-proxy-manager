"""AIRI activity-log tools (v4.0.2) — search_activity_log + get_error_summary.

These close a real gap: before this, AIRI's only log tool returned just
(time, provider, severity) and could not answer "any 429s lately?".
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.airi import tools
from app.models.db import ActivityLog, Provider


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest_asyncio.fixture
async def log_env():
    """A provider plus a spread of activity-log rows: real 429s, timeouts,
    a background keepalive probe, healthy requests, and one stale error."""
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(ActivityLog))
        await c.execute(delete(Provider).where(Provider.name.like("altool-%")))
        await c.commit()
    async with AsyncSessionLocal() as c:
        p = Provider(name="altool-grok", provider_type="grok-web", priority=5, enabled=True)
        c.add(p)
        await c.commit()
        await c.refresh(p)
        pid = p.id
        ts = _now()
        rows = []
        for _ in range(3):  # real client-traffic 429s
            rows.append(ActivityLog(
                event_type="llm_request", severity="error", message="upstream error",
                provider_id=pid, created_at=ts,
                event_meta={"provider_name": "altool-grok", "error_class": "rate_limit",
                            "error": "GrokWebError: bridge 429: Too many requests"}))
        for _ in range(2):  # timeouts
            rows.append(ActivityLog(
                event_type="llm_request", severity="warning", message="timed out",
                provider_id=pid, created_at=ts,
                event_meta={"provider_name": "altool-grok", "error_class": "timeout",
                            "error": "ReadTimeout"}))
        rows.append(ActivityLog(  # background probe — not real traffic
            event_type="keepalive_probe", severity="warning",
            message="[probe] altool-grok — error", provider_id=pid, created_at=ts,
            event_meta={"provider_name": "altool-grok", "error_class": "rate_limit",
                        "error": "429", "probe": True}))
        for _ in range(5):  # healthy requests
            rows.append(ActivityLog(
                event_type="llm_request", severity="info", message="ok",
                provider_id=pid, created_at=ts, event_meta={}))
        rows.append(ActivityLog(  # stale error, well outside any short window
            event_type="llm_request", severity="error", message="old",
            provider_id=pid, created_at=ts - timedelta(hours=48),
            event_meta={"error_class": "auth", "error": "401 Unauthorized"}))
        for r in rows:
            c.add(r)
        await c.commit()
    yield


# ── get_error_summary ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_error_summary_counts_by_class(log_env):
    out = await tools.run_tool("get_error_summary", {"window_minutes": 120})
    assert out["total_errors"] == 6              # 3 + 2 + 1; the stale row excluded
    assert out["by_error_class"]["rate_limit"] == 4   # 3 real + 1 probe
    assert out["by_error_class"]["timeout"] == 2
    assert "auth" not in out["by_error_class"]   # 48h old — outside the window
    assert out["by_event_type"]["keepalive_probe"] == 1
    assert out["by_provider"]["altool-grok"] == 6


@pytest.mark.asyncio
async def test_error_summary_window_widening_catches_stale(log_env):
    out = await tools.run_tool("get_error_summary", {"window_minutes": 72 * 60})
    assert "auth" in out["by_error_class"]


@pytest.mark.asyncio
async def test_error_summary_ignores_healthy_rows(log_env):
    out = await tools.run_tool("get_error_summary", {})
    # only error-severity rows are counted — never the 5 info requests
    assert out["total_errors"] == 6


# ── search_activity_log ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_finds_429_by_free_text(log_env):
    out = await tools.run_tool("search_activity_log", {"query": "429"})
    assert out["match_count"] == 4               # 3 real + 1 probe carry "429"
    assert all(e["error_class"] == "rate_limit" for e in out["events"])


@pytest.mark.asyncio
async def test_search_errors_only(log_env):
    out = await tools.run_tool("search_activity_log", {"errors_only": True})
    assert out["match_count"] == 6
    assert all(e["severity"] in ("warning", "error", "critical") for e in out["events"])


@pytest.mark.asyncio
async def test_search_event_type_filter(log_env):
    out = await tools.run_tool("search_activity_log", {"event_type": "keepalive_probe"})
    assert out["match_count"] == 1
    assert out["events"][0]["event_type"] == "keepalive_probe"


@pytest.mark.asyncio
async def test_search_provider_filter(log_env):
    hit = await tools.run_tool("search_activity_log",
                               {"provider": "altool-grok", "errors_only": True})
    assert hit["match_count"] == 6
    miss = await tools.run_tool("search_activity_log", {"provider": "no-such-provider"})
    assert miss["match_count"] == 0


@pytest.mark.asyncio
async def test_search_surfaces_error_detail(log_env):
    out = await tools.run_tool("search_activity_log", {"query": "Too many requests"})
    assert out["match_count"] == 3
    assert "429" in out["events"][0]["error"]


# ── get_recent_routing enrichment ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_recent_routing_surfaces_error_class(log_env):
    out = await tools.run_tool("get_recent_routing", {"limit": 100})
    assert any(r.get("error_class") == "rate_limit" for r in out["recent"])
    # healthy rows stay clean — no error_class noise
    assert all("error_class" not in r for r in out["recent"] if r["severity"] == "info")


# ── the tools are registered + read-only ─────────────────────────────────────

def test_log_tools_registered_read_only():
    names = {t["name"] for t in tools.TOOL_SCHEMAS}
    assert {"search_activity_log", "get_error_summary"} <= names
    assert {"search_activity_log", "get_error_summary"} <= tools.READ_ONLY_TOOLS
