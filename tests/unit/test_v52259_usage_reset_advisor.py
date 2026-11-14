"""v5.22.59 — usage-reset advisor: pure decision + fail-safe path (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.monitoring import usage_reset_advisor as ura
from app.monitoring.usage_reset_advisor import (
    ADVISE_FABLE_RESET,
    CANNOT_DECIDE,
    NO_ACTION,
    Thresholds,
    decide,
)
from app.providers.anthropic_billing import ScrapeResult, parse_fable_usage

T = Thresholds()
DAY = 86400


def d(w=96, f=35, r=None, s=DAY, adv=False):
    return decide(w, f, r, s, T, already_advised=adv).action


def test_advise_happy_path():
    assert d() == ADVISE_FABLE_RESET


@pytest.mark.parametrize("w,exp", [(94.9, NO_ACTION), (95, ADVISE_FABLE_RESET)])
def test_weekly_boundary(w, exp):
    assert d(w=w) == exp


@pytest.mark.parametrize("f,exp", [(29.9, NO_ACTION), (30, ADVISE_FABLE_RESET)])
def test_fable_boundary(f, exp):
    assert d(f=f) == exp


def test_missing_fable_cannot_decide_only_when_weekly_high():
    assert d(f=None) == CANNOT_DECIDE
    assert d(w=50, f=None) == NO_ACTION


def test_missing_weekly_or_reset_time():
    assert d(w=None) == CANNOT_DECIDE
    assert d(s=None) == CANNOT_DECIDE


@pytest.mark.parametrize("s,exp", [(7200, NO_ACTION), (7201, ADVISE_FABLE_RESET), (-5, NO_ACTION)])
def test_near_window_reset(s, exp):
    assert d(s=s) == exp


def test_once_per_window():
    assert d(adv=True) == NO_ACTION


def test_zero_resets_available():
    assert d(r=0) == NO_ACTION
    assert d(r=1) == ADVISE_FABLE_RESET


def test_window_key_stable_under_jitter():
    a = datetime(2026, 11, 15, 19, 59, 59, 52934, tzinfo=timezone.utc)  # noqa: UP017
    b = datetime(2026, 11, 15, 20, 0, 0, 53062, tzinfo=timezone.utc)  # noqa: UP017
    assert ura.window_key(a) == ura.window_key(b) == "2026-11-15T20"


def _body(weekly=96, fable=35):
    return {
        "seven_day": {"utilization": weekly, "resets_at": "2026-11-15T20:00:00+00:00"},
        "limits": [
            {"kind": "weekly_all", "scope": None},
            {"kind": "weekly_scoped", "scope": {"model": {"display_name": "Fable"}}},
        ],
        "weekly_scoped_shares": [{"limit_index": 1, "used_percent_of_weekly": fable}],
    }


def test_parse_fable_usage():
    out = parse_fable_usage(_body())
    assert out["weekly_pct"] == 96 and out["fable_pp"] == 35
    assert out["resets_available"] is None
    assert parse_fable_usage({"seven_day": {"utilization": 10}})["fable_pp"] is None
    assert parse_fable_usage("junk")["weekly_pct"] is None


def test_redeem_stub_raises_and_is_gated():
    with pytest.raises(NotImplementedError):
        ura.redeem_fable_reset()
    with (
        patch("app.config.settings.usage_reset_advisor_redeem_enabled", False),
        patch.object(ura, "redeem_fable_reset") as r,
    ):
        ura._maybe_redeem("p")
        r.assert_not_called()


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.commits = 0

    async def get(self, _cls, pk):
        return self.rows.get(pk)

    def add(self, obj):
        self.rows[obj.provider_id] = obj

    async def commit(self):
        self.commits += 1


PROV = SimpleNamespace(
    id="p1", name="acct", anthropic_org_uuid="org", anthropic_session_cookies='{"sessionKey": "x"}'
)
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)  # noqa: UP017


def _fetch(ok=True, state="ok", body=None, err=None):
    return AsyncMock(
        return_value=ScrapeResult(
            ok=ok,
            http_status=200 if ok else 403,
            auth_state=state,
            raw_body=None,
            parsed=body,
            error=err,
        )
    )


@pytest.mark.asyncio
async def test_fail_safe_alerts_once_no_loop():
    db = FakeDB()
    send = AsyncMock(return_value=True)
    with (
        patch(
            "app.providers.anthropic_billing.fetch_usage",
            _fetch(False, "session_expired", None, "403 rejected"),
        ),
        patch.object(ura, "_send", send),
    ):
        r1 = await ura.check_provider(db, PROV, NOW)
        r2 = await ura.check_provider(db, PROV, NOW)
    assert r1["action"] == r2["action"] == CANNOT_DECIDE and not r1["ok"]
    assert send.await_count == 1
    assert "check manually" in send.await_args.args[2]
    assert "sessionKey" not in str(send.await_args)


@pytest.mark.asyncio
async def test_unparseable_response_alerts_once():
    db = FakeDB()
    send = AsyncMock(return_value=True)
    with (
        patch(
            "app.providers.anthropic_billing.fetch_usage",
            _fetch(False, "parse_error", None, "json parse failed"),
        ),
        patch.object(ura, "_send", send),
    ):
        await ura.check_provider(db, PROV, NOW)
        await ura.check_provider(db, PROV, NOW)
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_advise_once_per_window_then_recover_realerts_failure():
    db = FakeDB()
    send = AsyncMock(return_value=True)
    with (
        patch("app.providers.anthropic_billing.fetch_usage", _fetch(body=_body())),
        patch.object(ura, "_send", send),
    ):
        r1 = await ura.check_provider(db, PROV, NOW)
        r2 = await ura.check_provider(db, PROV, NOW + timedelta(minutes=15))
    assert r1["action"] == ADVISE_FABLE_RESET and r2["action"] == NO_ACTION
    assert send.await_count == 1
    assert db.rows["p1"].advised_window == "2026-11-15T20"


@pytest.mark.asyncio
async def test_smtp_disabled_leaves_marker_unset():
    db = FakeDB()
    with (
        patch("app.providers.anthropic_billing.fetch_usage", _fetch(body=_body())),
        patch.object(ura, "_send", AsyncMock(return_value=False)),
    ):
        await ura.check_provider(db, PROV, NOW)
    assert db.rows["p1"].advised_window is None
