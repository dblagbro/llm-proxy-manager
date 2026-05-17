"""AIRI per-user notification preferences (v4.0.3)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.airi import notify_prefs, notify
from app.models.db import AiriNotificationPref


@pytest_asyncio.fixture
async def prefs_env():
    from app.models.database import engine, AsyncSessionLocal
    from app.models.db import Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with AsyncSessionLocal() as c:
        await c.execute(delete(AiriNotificationPref))
        await c.commit()
    yield AsyncSessionLocal


# ── get / set ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pref_default_for_unconfigured_user(prefs_env):
    async with prefs_env() as db:
        p = await notify_prefs.get_pref(db, "nobody")
    assert p["configured"] is False
    assert p["email"] is None
    assert p["categories"] == {"monitor": True, "automation": True}
    assert p["min_severity"] == "warning"


@pytest.mark.asyncio
async def test_set_pref_creates_then_upserts(prefs_env):
    async with prefs_env() as db:
        out = await notify_prefs.set_pref(
            db, "alice", email="alice@example.com", enabled=True,
            categories={"monitor": True, "automation": False}, min_severity="critical")
    assert out["configured"] is True
    assert out["email"] == "alice@example.com"
    assert out["categories"] == {"monitor": True, "automation": False}
    assert out["min_severity"] == "critical"
    async with prefs_env() as db:
        out2 = await notify_prefs.set_pref(
            db, "alice", email="alice2@example.com", enabled=False,
            categories={"monitor": True, "automation": True}, min_severity="info")
    assert out2["email"] == "alice2@example.com" and out2["enabled"] is False
    # exactly one row — it was updated, not duplicated
    async with prefs_env() as db:
        rows = (await db.execute(
            delete(AiriNotificationPref).returning(AiriNotificationPref.id))).all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_set_pref_rejects_bad_input(prefs_env):
    async with prefs_env() as db:
        assert "error" in await notify_prefs.set_pref(
            db, "x", email="not-an-email", enabled=True,
            categories=None, min_severity="warning")
        assert "error" in await notify_prefs.set_pref(
            db, "x", email="ok@e.com", enabled=True,
            categories=None, min_severity="bogus")


@pytest.mark.asyncio
async def test_set_pref_empty_email_allowed(prefs_env):
    async with prefs_env() as db:
        out = await notify_prefs.set_pref(
            db, "x", email="", enabled=True, categories=None, min_severity="warning")
    assert out["email"] is None and out["configured"] is True


# ── resolve_recipients ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_filters_by_category_severity_enabled(prefs_env):
    async with prefs_env() as db:
        await notify_prefs.set_pref(db, "alice", email="alice@e.com", enabled=True,
            categories={"monitor": True, "automation": True}, min_severity="warning")
        await notify_prefs.set_pref(db, "bob", email="bob@e.com", enabled=True,
            categories={"monitor": True, "automation": False}, min_severity="critical")
        await notify_prefs.set_pref(db, "carol", email="", enabled=True,
            categories=None, min_severity="info")          # no email
        await notify_prefs.set_pref(db, "dave", email="dave@e.com", enabled=False,
            categories=None, min_severity="info")           # disabled
    async with prefs_env() as db:
        # monitor @ warning -> alice only (bob needs critical; carol no email; dave off)
        assert await notify_prefs.resolve_recipients(
            db, category="monitor", severity="warning") == {"alice@e.com"}
        # monitor @ critical -> alice + bob
        assert await notify_prefs.resolve_recipients(
            db, category="monitor", severity="critical") == {"alice@e.com", "bob@e.com"}
        # automation @ warning -> alice only (bob opted out of automation)
        assert await notify_prefs.resolve_recipients(
            db, category="automation", severity="warning") == {"alice@e.com"}


# ── airi_notify end-to-end recipient fan-out ─────────────────────────────────

@pytest.mark.asyncio
async def test_airi_notify_fans_out_to_global_plus_subscribers(prefs_env, monkeypatch):
    async with prefs_env() as db:
        await notify_prefs.set_pref(db, "alice", email="alice@e.com", enabled=True,
            categories={"monitor": True, "automation": True}, min_severity="info")
    sent: list = []

    async def fake_send(severity, subject, message, provider_id=None,
                        throttle_key=None, to=None):
        sent.append(to)

    import app.monitoring.notifications as notif
    monkeypatch.setattr(notif, "send_alert", fake_send)
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_to", "ops@e.com")

    await notify.airi_notify("test subject", "the message", "warning", "monitor")
    # the shared mailbox AND the opted-in operator each get one
    assert set(sent) == {"ops@e.com", "alice@e.com"}


@pytest.mark.asyncio
async def test_airi_notify_respects_category_optout(prefs_env, monkeypatch):
    async with prefs_env() as db:
        await notify_prefs.set_pref(db, "alice", email="alice@e.com", enabled=True,
            categories={"monitor": True, "automation": False}, min_severity="info")
    sent: list = []

    async def fake_send(severity, subject, message, provider_id=None,
                        throttle_key=None, to=None):
        sent.append(to)

    import app.monitoring.notifications as notif
    monkeypatch.setattr(notif, "send_alert", fake_send)
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_to", "ops@e.com")

    # automation notification — alice opted OUT of automation -> only the mailbox
    await notify.airi_notify("rule acted", "msg", "warning", "automation")
    assert set(sent) == {"ops@e.com"}
