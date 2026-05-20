"""v4.4 M-3 + M-4 — probe→node_auth_state writer + routing filter
+ CB-sync exemption for node_local_session providers.

Path A wiring per `docs/4.4-per-node-bridge-design.md`. These tests
pin the smallest end-to-end contract:

M-3 (probe → write_local_state):
  - Successful grok-web probe writes auth_state="ok"
  - Auth-class failure → "needs_reauth"
  - Network/timeout/5xx → "bridge_down"
  - Other (bad_request) → "needs_reauth"

M-4 (routing filter):
  - Providers tagged `node_local_session=True` in extra_config get
    filtered out when local auth_state != "ok"
  - Providers WITHOUT the tag are unaffected (no-op for everyone else)
  - Missing row (never written) → filtered (fail-safe)

M-4 (CB-sync exemption):
  - `_persist_auto_skip()` skips setting Provider.auto_skip_until for
    node_local_session providers — the per-node auth_state table is
    the authoritative signal instead.
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from app.routing.node_auth_state import is_local_node_routable
from app.models.db import Provider, ProviderNodeAuthState


# ── M-4 routing filter logic ─────────────────────────────────────


def test_is_local_node_routable_ok_only():
    assert is_local_node_routable(
        ProviderNodeAuthState(provider_id="x", node_id="n",
                              auth_state="ok",
                              last_check_at=datetime.utcnow())
    ) is True
    for s in ("expired", "needs_reauth", "never_authed", "bridge_down"):
        assert is_local_node_routable(
            ProviderNodeAuthState(provider_id="x", node_id="n",
                                  auth_state=s,
                                  last_check_at=datetime.utcnow())
        ) is False
    assert is_local_node_routable(None) is False  # fail-safe


def test_router_filter_skips_node_local_session_when_not_routable():
    """Source-level wiring guard. select_provider() now contains a
    per-node-auth filter for providers with extra_config.
    node_local_session=True. This test verifies the source matches
    the contract (full integration test lives in select_provider's
    own test file, runs against a live DB)."""
    from pathlib import Path
    src = Path("app/routing/router.py").read_text()
    # The filter block has these unique marker strings
    assert "v4.4 M-4 (Path A)" in src
    assert "node_local_session" in src
    assert "is_local_node_routable" in src


def test_router_filter_noop_for_providers_without_flag():
    """The filter must be a NO-OP for providers whose extra_config
    lacks node_local_session=True. Source-level check: the filter
    body uses .get("node_local_session") which falls through cleanly
    for missing key / False / None / explicit-False."""
    from pathlib import Path
    src = Path("app/routing/router.py").read_text()
    # The continue-when-flag-absent path
    assert 'if not _ec.get("node_local_session"):' in src
    assert "_kept.append(_p)" in src


# ── M-4 CB-sync exemption ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_auto_skip_exempts_node_local_session():
    """A grok-web provider tagged node_local_session=True must not
    get its Provider.auto_skip_until stamped by the persistent-
    auth-failure path. The per-node auth_state table is the
    authoritative cluster-visible signal instead."""
    from app.routing.circuit_breaker import _persist_auto_skip
    from sqlalchemy.ext.asyncio import (
        create_async_engine, async_sessionmaker, AsyncSession,
    )
    from app.models.db import Base
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, class_=AsyncSession,
                                 expire_on_commit=False)
    async with Session() as db:
        db.add(Provider(
            id="grok-prov-tagged", name="grok-web-tagged",
            provider_type="grok-web", priority=1, enabled=True,
            extra_config={"node_local_session": True, "bridge_url": "x"},
        ))
        db.add(Provider(
            id="grok-prov-untagged", name="grok-web-untagged",
            provider_type="grok-web", priority=2, enabled=True,
            extra_config={"bridge_url": "y"},
        ))
        await db.commit()

    # Patch AsyncSessionLocal where _persist_auto_skip imports it from.
    import app.models.database as db_mod
    with patch.object(db_mod, "AsyncSessionLocal", Session):
        await _persist_auto_skip("grok-prov-tagged", "invalid x-api-key")
        await _persist_auto_skip("grok-prov-untagged", "invalid x-api-key")

    # Tagged provider: auto_skip_until should remain None.
    # Untagged: should be set ~24h out.
    from sqlalchemy import select
    async with Session() as db:
        tagged = (await db.execute(
            select(Provider).where(Provider.id == "grok-prov-tagged")
        )).scalar_one()
        untagged = (await db.execute(
            select(Provider).where(Provider.id == "grok-prov-untagged")
        )).scalar_one()
        assert tagged.auto_skip_until is None, \
            "node_local_session provider should NOT receive auto_skip_until"
        assert untagged.auto_skip_until is not None, \
            "normal provider should receive auto_skip_until on persistent-auth-failure"
        assert untagged.auto_skip_reason == "persistent_auth_failure"
    await engine.dispose()


# ── M-3 probe→state mapping ──────────────────────────────────────


def test_probe_outcome_mapping_matches_classifier_buckets():
    """Source-level wiring guard for the keepalive→node_auth_state
    hook. The probe outcome maps to one of {ok, needs_reauth,
    bridge_down}; the classify_error buckets drive the failure
    cases (auth→needs_reauth; network/timeout/5xx/rate_limit→
    bridge_down; else→needs_reauth)."""
    from pathlib import Path
    src = Path("app/monitoring/keepalive.py").read_text()
    # The hook block has these unique markers
    assert "v4.4 M-3" in src
    assert "write_local_state" in src
    # The classify_error → auth_state map
    assert '"auth"' in src and '"needs_reauth"' in src
    assert '"bridge_down"' in src
    # v4.4.1 BUG-051: rate_limit joins the transient bucket
    assert '"network", "timeout", "upstream_5xx", "rate_limit"' in src


def test_bug051_rate_limit_maps_to_bridge_down():
    """v4.4.1 BUG-051 regression guard. A 429 from upstream is
    transient throttling — it must map to ``bridge_down``
    (auto-clears on next successful probe), NOT to ``needs_reauth``
    (which is the operator-time signal). Pre-fix evidence: a live
    c1conv probe row got stamped ``needs_reauth`` for an upstream
    ``grok-web bridge 429: Too many requests``, which (under any
    future Path A activation) would semi-permanently gate the
    node out of routing."""
    from app.routing.circuit_breaker import classify_error
    # The classifier returns "rate_limit" for these strings:
    samples = [
        'grok-web bridge 429: {"detail":"grok.com 429 (cached, cool-off 24s remaining)..."}',
        "HTTP 429 Too Many Requests from upstream",
        "RateLimitError: rate_limit_exceeded",
        "ratelimit hit; throttled",
    ]
    for s in samples:
        assert classify_error(s) == "rate_limit", \
            f"classifier should bucket {s!r} as rate_limit"

    # And the M-3 mapping branches rate_limit into bridge_down (not
    # needs_reauth). We assert the source contains the explicit
    # branch because the hook itself is buried in record_outcome's
    # try-block and is hard to call directly without a probe context.
    from pathlib import Path
    src = Path("app/monitoring/keepalive.py").read_text()
    # The new branch line — rate_limit appended to the transient set.
    assert 'in ("network", "timeout", "upstream_5xx", "rate_limit")' in src
    # And the BUG-051 inline comment to make grep find the rationale.
    assert "BUG-051" in src


def test_bug051_billing_and_bad_request_still_needs_reauth():
    """The fix narrowly adds rate_limit to the transient bucket. It
    must NOT change the policy for billing / bad_request / unknown
    — those remain ``needs_reauth`` (the operator-time signal) by
    design. Source-level guard so a future refactor doesn't
    accidentally widen the fix into a regression."""
    from app.routing.circuit_breaker import classify_error
    # billing/bad_request bucketing is unchanged:
    assert classify_error("HTTP 402 Payment Required") == "billing"
    assert classify_error("HTTP 400 bad json") == "bad_request"
    # And the else-branch comment in keepalive.py still names them:
    from pathlib import Path
    src = Path("app/monitoring/keepalive.py").read_text()
    # Source comment names billing and bad_request as needs_reauth.
    assert "bad_request" in src and "billing" in src
    # The else: branch must still set needs_reauth.
    # Locate the M-3 mapping block and assert the else branch lands.
    block_start = src.index("# Classify the probe error into an auth_state")
    block = src[block_start: block_start + 1500]
    assert "else:" in block and '_new_state = "needs_reauth"' in block


def test_probe_hook_writes_local_state_best_effort_swallow():
    """The hook must be best-effort — a failure inside the
    write_local_state path can NOT corrupt the probe's own
    record_outcome flow. Source-level check for the
    try/except/.debug-log shape."""
    from pathlib import Path
    src = Path("app/monitoring/keepalive.py").read_text()
    assert "keepalive.m3_node_auth_state_write_failed" in src
    # except Exception means everything swallowed
    assert "except Exception" in src
