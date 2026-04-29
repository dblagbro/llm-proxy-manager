"""Run-state replication across cluster peers (R5).

Per the locked design (Q2: sticky-with-handoff), the owner node executes
the run; peers hold a checkpoint copy so a takeover after owner failure
doesn't lose history.

Push policy:
  - Non-terminal transitions: debounced 250ms — multiple state changes
    inside a window collapse into one push. Massive volume reduction.
  - Terminal transitions (completed/failed/expired/cancelled): synchronous
    fire-and-forget with a 2s peer-ack timeout, then immediate background
    retry on failure. The originating node still returns to the client
    after the 2s peer ack OR timeout; clients reading the run on a peer
    after completion see the terminal state in the worst case ~2s late.

Push body shape (extension to existing /cluster/sync payload):
  {"runs": [{run_row_dict}, ...]}

Last-write-wins by ``updated_at`` on the receiving peer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from app.cluster.auth import sign_payload
from app.config import settings
from app.models.db import Run

logger = logging.getLogger(__name__)


DEBOUNCE_SEC = 0.25                # 250ms
TERMINAL_PEER_ACK_TIMEOUT_SEC = 2.0
TERMINAL_KINDS = {"completed", "failed", "expired", "cancelled"}


def _serialise_run(r: Run) -> dict:
    """Subset of Run we ship between peers — enough to reconstruct the
    state for adopt() and to answer GET on a peer."""
    return {
        "id": r.id,
        "api_key_id": r.api_key_id,
        "owner_node_id": r.owner_node_id,
        "status": r.status,
        "current_step": r.current_step,
        "deadline_ts": r.deadline_ts,
        "max_turns": r.max_turns,
        "model_preference": r.model_preference,
        "compaction_model": r.compaction_model,
        "system_prompt": r.system_prompt,
        "tools_spec": r.tools_spec,
        "metadata_json": r.metadata_json,
        "trace_id": r.trace_id,
        "model_calls": r.model_calls,
        "tool_calls": r.tool_calls,
        "tokens_in": r.tokens_in,
        "tokens_out": r.tokens_out,
        "last_provider_id": r.last_provider_id,
        "context_summarized_at_turn": r.context_summarized_at_turn,
        "current_tool_use_id": r.current_tool_use_id,
        "current_tool_name": r.current_tool_name,
        "current_tool_input": r.current_tool_input,
        "result_text": r.result_text,
        "error_kind": r.error_kind,
        "error_message": r.error_message,
        "created_at": r.created_at,
        "updated_at": r.updated_at,
        "completed_at": r.completed_at,
    }


# ── Debounced push coalescer ────────────────────────────────────────────────


@dataclass
class _PendingPush:
    run_snapshot: dict
    fire_at: float
    task: Optional[asyncio.Task] = None


_pending: dict[str, _PendingPush] = {}      # run_id → pending push
_lock = asyncio.Lock()


async def replicate(run: Run, *, terminal: bool = False) -> None:
    """Public API: schedule a state push for this run.

    For non-terminal transitions, debounces 250ms — overlapping calls
    coalesce into one push at the end of the window. For terminal
    transitions, fires immediately with a 2s peer-ack window. Background
    retry on failure (kept minimal — 3 attempts at 5/15/45s).
    """
    if not settings.cluster_enabled:
        return
    snapshot = _serialise_run(run)

    if terminal:
        await _push_terminal(snapshot)
        return

    # Non-terminal: debounce
    async with _lock:
        existing = _pending.get(run.id)
        fire_at = time.monotonic() + DEBOUNCE_SEC
        if existing is not None:
            # Replace snapshot + extend the timer
            existing.run_snapshot = snapshot
            existing.fire_at = fire_at
            return
        pending = _PendingPush(run_snapshot=snapshot, fire_at=fire_at)
        _pending[run.id] = pending
        pending.task = asyncio.create_task(_debounce_runner(run.id))


async def _debounce_runner(run_id: str) -> None:
    """Sleep until ``fire_at``; if the timer was extended while we were
    sleeping, sleep again. Push when settled."""
    while True:
        async with _lock:
            pending = _pending.get(run_id)
            if pending is None:
                return
            sleep_for = max(0.0, pending.fire_at - time.monotonic())
        if sleep_for > 0:
            await asyncio.sleep(sleep_for)
            continue
        async with _lock:
            pending = _pending.pop(run_id, None)
        if pending is None:
            return
        await _push_to_peers([pending.run_snapshot], wait_for_ack=False)
        return


# ── Peer push ───────────────────────────────────────────────────────────────


async def _push_to_peers(
    snapshots: list[dict],
    *,
    wait_for_ack: bool,
) -> dict[str, bool]:
    """Push a runs payload to every peer. Returns ``{peer_id: ok_bool}``."""
    try:
        from app.cluster.manager import peers as _peer_registry
        peers = list(_peer_registry.values())
    except Exception:
        return {}
    if not peers:
        return {}

    payload = {
        "source_node": settings.cluster_node_id,
        "timestamp": time.time(),
        "runs": snapshots,
    }
    body = json.dumps(payload, sort_keys=True).encode()
    sig = sign_payload(body)
    headers = {
        "X-Cluster-Node": settings.cluster_node_id or "",
        "X-Cluster-Sig": sig,
        "Content-Type": "application/json",
    }
    timeout = TERMINAL_PEER_ACK_TIMEOUT_SEC if wait_for_ack else 5.0

    results: dict[str, bool] = {}

    async def push_one(peer):
        url = f"{peer.url.rstrip('/')}/cluster/sync"
        try:
            async with httpx.AsyncClient(timeout=timeout, verify=False) as client:
                r = await client.post(url, content=body, headers=headers)
                results[peer.id] = (200 <= r.status_code < 300)
        except Exception as e:
            results[peer.id] = False
            logger.info("runs.replication.push_fail peer=%s err=%s", peer.id, e)

    await asyncio.gather(*(push_one(p) for p in peers), return_exceptions=True)
    return results


async def _push_terminal(snapshot: dict) -> None:
    """Sync-ack with 2s timeout per peer; on any failure schedule a
    background retry chain (5s, 15s, 45s)."""
    results = await _push_to_peers([snapshot], wait_for_ack=True)
    failed = [pid for pid, ok in results.items() if not ok]
    if failed:
        logger.info("runs.replication.terminal_peer_ack_miss run=%s peers=%s",
                    snapshot.get("id"), failed)
        asyncio.create_task(_retry_terminal_push(snapshot, failed))


async def _retry_terminal_push(
    snapshot: dict, target_peer_ids: list[str],
) -> None:
    """3-attempt retry chain for peers that missed a terminal-state push."""
    for delay in (5.0, 15.0, 45.0):
        await asyncio.sleep(delay)
        try:
            from app.cluster.manager import peers as _peer_registry
            peers = [_peer_registry.get(pid) for pid in target_peer_ids]
            peers = [p for p in peers if p is not None]
        except Exception:
            return
        if not peers:
            return
        # Targeted retry: build a one-shot peer list
        body = json.dumps({
            "source_node": settings.cluster_node_id,
            "timestamp": time.time(),
            "runs": [snapshot],
        }, sort_keys=True).encode()
        sig = sign_payload(body)
        headers = {
            "X-Cluster-Node": settings.cluster_node_id or "",
            "X-Cluster-Sig": sig,
            "Content-Type": "application/json",
        }
        still_failing: list[str] = []
        for peer in peers:
            try:
                async with httpx.AsyncClient(timeout=5.0, verify=False) as client:
                    r = await client.post(
                        f"{peer.url.rstrip('/')}/cluster/sync",
                        content=body, headers=headers,
                    )
                    if not (200 <= r.status_code < 300):
                        still_failing.append(peer.id)
            except Exception:
                still_failing.append(peer.id)
        target_peer_ids = still_failing
        if not target_peer_ids:
            return  # All peers caught up
    # All 3 attempts exhausted — log loud, give up. Next periodic
    # /cluster/sync push (driven by app/cluster/manager.py) will reconcile
    # eventually; we just don't have the same-second guarantee.
    logger.warning("runs.replication.terminal_push_exhausted run=%s peers=%s",
                   snapshot.get("id"), target_peer_ids)
