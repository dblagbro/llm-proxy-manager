"""v5.0.0 — policy-change quorum fan-out (spec §6.1).

Exercises ``app.cluster.manager.push_policy_change_with_quorum`` which
fans a policy-change payload out to all active peers and returns as
soon as ``required_acks`` peers ACK or the timeout elapses.

Test matrix:
  - happy path: 3 peers, required_acks=2, all 3 ack →
    ``cluster_sync_status="fully-acked"``.
  - N-1 quorum: 3 peers, required_acks=2, 2 ack within timeout, 1 lags →
    ``cluster_sync_status="quorum-reached-1-pending"``.
  - insufficient acks: required_acks=2, only 1 acks within timeout →
    raises ``ClusterSyncQuorumNotReached``.

We stub ``_push_to_peer`` so the test stays synchronous-ish and
deterministic. The real HTTP path is exercised in integration tests.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from app.cluster import manager
from app.cluster.manager import (
    ClusterSyncQuorumNotReached,
    PeerNode,
    push_policy_change_with_quorum,
)


@pytest.fixture
def reset_peers(monkeypatch):
    """Install 3 healthy peers and restore on teardown."""
    saved = dict(manager._peers)
    manager._peers.clear()
    for name in ("alpha", "bravo", "charlie"):
        manager._peers[name] = PeerNode(
            id=name, name=name, url=f"http://{name}/llm", status="healthy",
        )
    yield
    manager._peers.clear()
    manager._peers.update(saved)


def _ack_after(delay: float):
    """Build a ``_push_to_peer`` replacement that delays ``delay`` seconds
    then returns (peer, now). Used to stage staggered ack arrivals."""

    async def _impl(peer, payload, timeout_sec):
        await asyncio.sleep(delay)
        return peer, datetime.now(timezone.utc)

    return _impl


def _fail_after(delay: float, exc: Exception):
    async def _impl(peer, payload, timeout_sec):
        await asyncio.sleep(delay)
        raise exc

    return _impl


@pytest.mark.asyncio
async def test_quorum_happy_path_all_three_ack(monkeypatch, reset_peers):
    """3 peers, required_acks=2, all 3 ack quickly → fully-acked."""

    monkeypatch.setattr(manager, "_push_to_peer", _ack_after(0.0))

    result = await push_policy_change_with_quorum(
        payload={"compliance_policy_changes": [{"policy_change_id": "ppc_test"}]},
        required_acks=2,
        timeout_sec=2.0,
    )

    assert result["cluster_sync_status"] == "fully-acked"
    assert len(result["applied_to_peers"]) == 3
    assert result["pending_peers"] == []


@pytest.mark.asyncio
async def test_quorum_n_minus_1_one_pending(monkeypatch, reset_peers):
    """3 peers, required_acks=2: 2 ack immediately, 1 lags past quorum.
    Expected: cluster_sync_status='quorum-reached-1-pending'."""

    call_count = {"n": 0}

    async def staged(peer, payload, timeout_sec):
        call_count["n"] += 1
        # First two peers ack immediately; the third sleeps past the
        # synthetic quorum check window.
        if peer.id == "charlie":
            await asyncio.sleep(1.0)
        return peer, datetime.now(timezone.utc)

    monkeypatch.setattr(manager, "_push_to_peer", staged)

    result = await push_policy_change_with_quorum(
        payload={"compliance_policy_changes": [{"policy_change_id": "ppc_lag"}]},
        required_acks=2,
        timeout_sec=0.3,
    )

    assert result["cluster_sync_status"] == "quorum-reached-1-pending"
    assert len(result["applied_to_peers"]) == 2
    assert len(result["pending_peers"]) == 1
    assert result["pending_peers"][0]["peer"] == "charlie"


@pytest.mark.asyncio
async def test_quorum_insufficient_raises(monkeypatch, reset_peers):
    """required_acks=2, only 1 succeeds within timeout — must raise."""

    async def mostly_fail(peer, payload, timeout_sec):
        if peer.id == "alpha":
            return peer, datetime.now(timezone.utc)
        # bravo + charlie both fail
        raise RuntimeError("HTTP 500: synthetic")

    monkeypatch.setattr(manager, "_push_to_peer", mostly_fail)

    with pytest.raises(ClusterSyncQuorumNotReached) as exc_info:
        await push_policy_change_with_quorum(
            payload={"compliance_policy_changes": [{"policy_change_id": "ppc_fail"}]},
            required_acks=2,
            timeout_sec=0.5,
        )

    # The exception carries acks + pending so the caller can report
    # which peers are stuck.
    assert len(exc_info.value.acks) == 1
    assert len(exc_info.value.pending) == 2
