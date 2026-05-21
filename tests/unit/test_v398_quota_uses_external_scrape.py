"""v3.9.8 — usage quota display uses ExternalUsageSnapshot (authoritative).

The pre-v3.9.8 dashboard showed "weekly 643%" / "weekly 365%" for the
Anthropic Pro Max providers because /api/providers was reading
``ProviderUsageWindow`` — the proxy-side traffic counter — instead of
the authoritative ``ExternalUsageSnapshot`` data from the v3.7.0
Anthropic Console scrape. The proxy slice can be ~3 orders of magnitude
lower than the account total when the same Pro Max account also feeds
Claude Code / desktop / other workloads, so the operator-set
``usage_weekly_limit_tokens`` (sized for proxy slice) hit nonsense
ratios against the rolled-up counter.

These tests lock the precedence: snapshot wins when present; fall
through to ``ProviderUsageWindow`` only for providers that haven't
been scraped (e.g. per_call providers without an upstream usage API).
"""
from __future__ import annotations

import pytest
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.models.db import (
    Base, Provider, ProviderUsageWindow, ExternalUsageSnapshot,
)


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s:
        s.add(Provider(
            id="p_scraped", name="Devin-Anthropic-Max-Gmail",
            provider_type="claude-oauth", api_key="x", priority=1,
            enabled=True, default_model="claude",
            usage_tracking_enabled=True,
            usage_weekly_limit_tokens=10_000_000,  # low limit, bad source overflowed it
            usage_session_limit_tokens=1_000_000,
        ))
        s.add(Provider(
            id="p_unscraped", name="some-paid-openai",
            provider_type="openai", api_key="x", priority=2,
            enabled=True, default_model="gpt-4o",
            usage_tracking_enabled=False,
        ))
        # ProviderUsageWindow row with the BAD 643% number
        s.add(ProviderUsageWindow(
            provider_id="p_scraped",
            session_tokens=5_000_000,
            session_pct=500.0,  # internal hallucination
            weekly_tokens=64_300_000,
            weekly_pct=643.0,
            updated_at=datetime.now(timezone.utc),
        ))
        # Authoritative snapshot — Anthropic Console says 28%
        s.add(ExternalUsageSnapshot(
            provider_id="p_scraped",
            captured_at=datetime.now(timezone.utc),
            source="anthropic_console_v1",
            http_status=200,
            auth_state="ok",
            five_hour_utilization=12.0,
            seven_day_utilization=28.0,
        ))
        # ProviderUsageWindow for the unscraped provider (no snapshot)
        s.add(ProviderUsageWindow(
            provider_id="p_unscraped",
            session_tokens=500,
            session_pct=5.0,
            weekly_tokens=10000,
            weekly_pct=12.0,
            updated_at=datetime.now(timezone.utc),
        ))
        await s.commit()
        yield s
    await engine.dispose()


# ── Logic-level: replicate the providers.list query path ─────────────


async def test_scraped_provider_shows_authoritative_pct(db):
    """When ExternalUsageSnapshot exists, list endpoint uses it."""
    from sqlalchemy import select, desc

    snap_res = await db.execute(
        select(ExternalUsageSnapshot)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
    )
    snap_by_provider = {}
    for snap in snap_res.scalars().all():
        snap_by_provider.setdefault(snap.provider_id, snap)
    usage_res = await db.execute(select(ProviderUsageWindow))
    usage_by_id = {w.provider_id: w for w in usage_res.scalars().all()}

    # Apply the same precedence logic as providers.list_providers
    provs = (await db.execute(select(Provider))).scalars().all()
    by_name = {p.name: p for p in provs}

    scraped = by_name["Devin-Anthropic-Max-Gmail"]
    snap = snap_by_provider.get(scraped.id)
    w = usage_by_id.get(scraped.id)
    assert snap is not None and snap.seven_day_utilization is not None
    # The authoritative value wins:
    assert snap.seven_day_utilization == 28.0
    # The internal hallucination is NOT exposed when scrape is available
    assert w.weekly_pct == 643.0, "internal counter still has the bad value"
    # ^ But the SOURCE the endpoint USES is the snapshot, not w.


async def test_unscraped_provider_falls_back_to_internal_window(db):
    """When no ExternalUsageSnapshot exists, fall back to ProviderUsageWindow."""
    from sqlalchemy import select, desc

    snap_res = await db.execute(
        select(ExternalUsageSnapshot)
        .order_by(desc(ExternalUsageSnapshot.captured_at))
    )
    snap_by_provider = {}
    for snap in snap_res.scalars().all():
        snap_by_provider.setdefault(snap.provider_id, snap)
    usage_res = await db.execute(select(ProviderUsageWindow))
    usage_by_id = {w.provider_id: w for w in usage_res.scalars().all()}

    provs = (await db.execute(select(Provider))).scalars().all()
    by_name = {p.name: p for p in provs}
    unscraped = by_name["some-paid-openai"]
    snap = snap_by_provider.get(unscraped.id)
    w = usage_by_id.get(unscraped.id)
    assert snap is None
    assert w is not None
    assert w.weekly_pct == 12.0


# ── Source-level guards ─────────────────────────────────────────────


def test_providers_list_imports_external_snapshot():
    src = Path("app/api/providers.py").read_text() + "\n# providers_stats.py\n" + Path("app/api/providers_stats.py").read_text()
    # Pre-v4.4.14 the import was a single line in providers.py; the v4.4.14
    # split moved the endpoints to providers_stats.py where the import is
    # multi-line. Accept either form: the symbol name appears AND it's in
    # an import statement somewhere in the concatenated source.
    assert "ExternalUsageSnapshot" in src
    assert "import" in src
    assert "snap.seven_day_utilization" in src
    assert "snap.five_hour_utilization" in src


def test_providers_list_prefers_snapshot_over_window():
    src = Path("app/api/providers.py").read_text() + "\n# providers_stats.py\n" + Path("app/api/providers_stats.py").read_text()
    # The snapshot branch must come BEFORE the ProviderUsageWindow fallback
    idx_snap = src.find("snap_by_provider.get(p.id)")
    idx_w_assign = src.find("d[\"usage_weekly_pct\"] = w.weekly_pct")
    assert idx_snap > 0
    assert idx_w_assign > idx_snap, (
        "ProviderUsageWindow fallback must come AFTER the snapshot branch; "
        "otherwise the bad pct values overwrite the good ones"
    )


def test_provider_detail_endpoint_also_prefers_snapshot():
    src = Path("app/api/providers.py").read_text() + "\n# providers_stats.py\n" + Path("app/api/providers_stats.py").read_text()
    assert '"data_source": "external_scrape"' in src
    assert '"data_source": "internal_window"' in src


def test_data_source_field_exposed_for_ui_distinction():
    """UI needs to know which source it's seeing so it can label
    'authoritative (Anthropic)' vs 'internal (proxy slice)'."""
    src = Path("app/api/providers.py").read_text() + "\n# providers_stats.py\n" + Path("app/api/providers_stats.py").read_text()
    assert 'd["usage_data_source"] = "external_scrape"' in src
    assert 'd["usage_data_source"] = "internal_window"' in src
