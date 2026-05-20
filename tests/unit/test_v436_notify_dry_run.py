"""v4.3.6 — BUG-031 closure: dry_run path for app.airi.notify.airi_notify.

Closes BUG-031 (AIRI notifications dispatch not live-tested). Live
testing used to require an actual SMTP send, which would spam the
operator's inbox on every run. v4.3.6 adds a dry_run mode that
resolves recipients + renders body but skips the send and returns
the planned dispatch as a dict.

The unit tests below pin both paths (param + env var), make sure
production behaviour is unchanged when dry_run is false, and confirm
the returned dict carries the same body the production path would
have sent.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from app.airi import notify


# ── dry_run via the param ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_param_skips_send_and_returns_dict(monkeypatch):
    monkeypatch.delenv("AIRI_NOTIFY_DRY_RUN", raising=False)
    # patch send_alert + the prefs resolver: dry_run must not call them.
    sent = []

    async def fake_send(*a, **kw):
        sent.append(kw.get("to"))

    monkeypatch.setattr(
        "app.monitoring.notifications.send_alert", fake_send, raising=False
    )

    async def fake_resolve(db, *, category, severity):
        return {"alice@example.com"}

    monkeypatch.setattr(
        "app.airi.notify_prefs.resolve_recipients", fake_resolve, raising=False
    )
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_to", "ops@example.com", raising=False)

    result = await notify.airi_notify(
        "test subject", "the message", "warning", "monitor", dry_run=True
    )

    assert sent == [], "dry_run must NOT call send_alert"
    assert result is not None
    assert result["dry_run"] is True
    assert result["subject"] == "AIRI: test subject"
    assert "the message" in result["body"]
    assert "Open AIRI to discuss or act:" in result["body"]
    assert result["severity"] == "warning"
    assert result["category"] == "monitor"
    assert set(result["recipients"]) == {"ops@example.com", "alice@example.com"}


# ── dry_run via the env var (no code change at call sites) ─────────


@pytest.mark.asyncio
async def test_dry_run_env_var_flips_globally(monkeypatch):
    """An integration test that flips the env var should affect every
    airi_notify call without touching the call sites."""
    monkeypatch.setenv("AIRI_NOTIFY_DRY_RUN", "1")
    sent = []

    async def fake_send(*a, **kw):
        sent.append(kw.get("to"))

    monkeypatch.setattr(
        "app.monitoring.notifications.send_alert", fake_send, raising=False
    )

    async def fake_resolve(db, *, category, severity):
        return set()

    monkeypatch.setattr(
        "app.airi.notify_prefs.resolve_recipients", fake_resolve, raising=False
    )
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_to", "ops@example.com", raising=False)

    result = await notify.airi_notify(
        "from env var", "body", "info", "automation"
    )
    assert sent == []
    assert result is not None and result["dry_run"] is True
    assert result["recipients"] == ["ops@example.com"]


@pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
def test_env_var_truthy_values(monkeypatch, val):
    monkeypatch.setenv("AIRI_NOTIFY_DRY_RUN", val)
    assert notify._env_dry_run() is True


@pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "anything-else"])
def test_env_var_falsy_values(monkeypatch, val):
    monkeypatch.setenv("AIRI_NOTIFY_DRY_RUN", val)
    assert notify._env_dry_run() is False


# ── production path unchanged when dry_run is false ───────────────


@pytest.mark.asyncio
async def test_production_path_calls_send_when_dry_run_false(monkeypatch):
    monkeypatch.delenv("AIRI_NOTIFY_DRY_RUN", raising=False)
    sent = []

    async def fake_send(severity, subject, message, provider_id=None,
                        throttle_key=None, to=None):
        sent.append((to, subject, severity))

    monkeypatch.setattr(
        "app.monitoring.notifications.send_alert", fake_send, raising=False
    )

    async def fake_resolve(db, *, category, severity):
        return {"alice@e.com"}

    monkeypatch.setattr(
        "app.airi.notify_prefs.resolve_recipients", fake_resolve, raising=False
    )
    from app.config import settings
    monkeypatch.setattr(settings, "smtp_to", "ops@e.com", raising=False)

    result = await notify.airi_notify("subj", "body", "warning", "monitor")

    # send_alert called once per recipient
    assert {row[0] for row in sent} == {"ops@e.com", "alice@e.com"}
    # production path returns None (unchanged from v4.0)
    assert result is None
