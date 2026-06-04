"""v5.0.4 — cursor-oauth JWT expiry monitor.

Pins:

- ``_decode_jwt_exp`` extracts the ``exp`` claim from a real
  Cursor-shape token (synthesized as ``user_<id>::<JWT>``) and from a
  bare JWT.
- ``_run_one_sweep`` backfills ``Provider.oauth_expires_at`` when NULL
  and produces a per-provider snapshot with days_left.
- The warn flag fires when days_left <= threshold.
- Non-cursor-oauth providers are ignored.
- Malformed/missing tokens don't break the sweep.
"""
import base64
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.models.database import AsyncSessionLocal
from app.models.db import Provider
from app.monitoring.cursor_oauth_expiry_monitor import (
    _decode_jwt_exp,
    _days_until,
    _run_one_sweep,
    get_last_sweep,
)


pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_test_providers():
    """Each test in this module manipulates the providers table; restore
    it to the pre-test state on yield so neighboring tests (lmrh_v2,
    etc.) see the fixtures they expect rather than a polluted table."""
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(Provider))).scalars().all()
        before_ids = {p.id for p in existing}
    yield
    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(Provider))
        # Drop only the providers our tests added — never touch
        # pre-existing rows from other fixtures.
        for p in rs.scalars().all():
            if p.id not in before_ids:
                await db.delete(p)
        await db.commit()


def _make_jwt(payload: dict) -> str:
    """Minimal JWT — header.payload.signature with the signature
    unverified (we don't check it; only the payload's exp claim)."""
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.unverified-signature"


# ── _decode_jwt_exp ───────────────────────────────────────────────────


def test_decode_jwt_exp_from_synthesized_cursor_token():
    """Cursor synthesizes the api_key as ``user_<id>::<JWT>`` — the
    decoder must split on ``::`` first."""
    exp_ts = 9999999999
    jwt = _make_jwt({"exp": exp_ts, "scope": "openid offline_access"})
    cursor_token = f"user_01ABC::{jwt}"
    assert _decode_jwt_exp(cursor_token) == float(exp_ts)


def test_decode_jwt_exp_from_bare_jwt():
    jwt = _make_jwt({"exp": 12345})
    assert _decode_jwt_exp(jwt) == 12345.0


def test_decode_jwt_exp_handles_missing_exp():
    jwt = _make_jwt({"scope": "openid"})
    assert _decode_jwt_exp(jwt) is None


def test_decode_jwt_exp_handles_garbage_token():
    assert _decode_jwt_exp("not a jwt") is None
    assert _decode_jwt_exp("") is None
    assert _decode_jwt_exp(None) is None
    assert _decode_jwt_exp("xx.yy") is None  # too few segments to be JWT
    assert _decode_jwt_exp("header.NOTBASE64.sig") is None


# ── _days_until ──────────────────────────────────────────────────────


def test_days_until_handles_none():
    assert _days_until(None) is None


def test_days_until_positive_for_future():
    future = time.time() + 86400 * 30
    assert _days_until(future) == pytest.approx(30, abs=0.01)


def test_days_until_negative_for_past():
    past = time.time() - 86400 * 5
    assert _days_until(past) < 0


# ── _run_one_sweep ──────────────────────────────────────────────────


async def _reset_providers():
    """Drop only providers our tests own (ids starting with 'cur' or 'oai')
    so we don't blow away the seed rows other tests' fixtures rely on."""
    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(Provider))
        for p in rs.scalars().all():
            if p.id.startswith(("cur", "oai")):
                await db.delete(p)
        await db.commit()


async def test_run_one_sweep_backfills_oauth_expires_at_when_null():
    await _reset_providers()
    exp_ts = time.time() + 86400 * 30  # 30 days out
    jwt = _make_jwt({"exp": int(exp_ts), "scope": "offline_access"})
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="cur1", name="Cursor", provider_type="cursor-oauth",
            enabled=True, priority=10, api_key=f"user_1::{jwt}",
        ))
        await db.commit()

    snapshots = await _run_one_sweep()
    cur1 = next((s for s in snapshots if s["provider_id"] == "cur1"), None)
    assert cur1 is not None
    assert cur1["days_left"] == pytest.approx(30, abs=0.5)
    assert cur1["warn"] is False
    assert cur1["jwt_expires_at"] is not None

    # Persisted to the row
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Provider).where(Provider.id == "cur1"))
        p = r.scalar_one()
        assert p.oauth_expires_at is not None


async def test_run_one_sweep_warn_when_under_threshold():
    await _reset_providers()
    exp_ts = time.time() + 86400 * 7  # 7 days out
    jwt = _make_jwt({"exp": int(exp_ts)})
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="cur1", name="Cursor", provider_type="cursor-oauth",
            enabled=True, priority=10, api_key=f"user_1::{jwt}",
        ))
        await db.commit()

    snapshots = await _run_one_sweep(warn_threshold_days=14)
    cur1 = next((s for s in snapshots if s["provider_id"] == "cur1"), None)
    assert cur1 is not None
    assert cur1["warn"] is True
    assert cur1["days_left"] == pytest.approx(7, abs=0.5)


async def test_run_one_sweep_ignores_non_cursor_oauth_providers():
    await _reset_providers()
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="oai1", name="OpenAI", provider_type="openai",
            enabled=True, priority=10, api_key="sk-not-a-jwt",
        ))
        await db.commit()
    snapshots = await _run_one_sweep()
    # The sweep returns only cursor-oauth providers — our added 'oai1'
    # must NOT be in the snapshot (its provider_type is 'openai').
    assert not any(s["provider_id"] == "oai1" for s in snapshots)


async def test_run_one_sweep_handles_malformed_token():
    await _reset_providers()
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="cur1", name="Cursor-broken", provider_type="cursor-oauth",
            enabled=True, priority=10, api_key="this-is-not-a-jwt-at-all",
        ))
        await db.commit()
    snapshots = await _run_one_sweep()
    cur1 = next((s for s in snapshots if s["provider_id"] == "cur1"), None)
    assert cur1 is not None
    # No exp could be decoded → days_left None, no warn
    assert cur1["days_left"] is None
    assert cur1["warn"] is False


async def test_get_last_sweep_returns_snapshot():
    """Admin endpoint consumes this — make sure it round-trips through
    the module-level state."""
    await _reset_providers()
    exp_ts = time.time() + 86400 * 30
    jwt = _make_jwt({"exp": int(exp_ts)})
    async with AsyncSessionLocal() as db:
        db.add(Provider(
            id="cur1", name="Cursor", provider_type="cursor-oauth",
            enabled=True, priority=10, api_key=f"user_1::{jwt}",
        ))
        await db.commit()
    await _run_one_sweep()
    snap = get_last_sweep()
    assert snap["last_sweep_ts"] is not None
    assert any(p["provider_id"] == "cur1" for p in snap["providers"])
    assert snap["warn_threshold_days"] == 14
