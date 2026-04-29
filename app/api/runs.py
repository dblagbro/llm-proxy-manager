"""POST /v1/runs and friends — Run runtime endpoints (R1 stubs).

R1 lands the contract: persistence, idempotency replay, FSM-driven state
transitions for cancel/tool_result, and a stub /events stream that emits
``run_started`` then waits. The actual model-call worker arrives in R2.

State machine: app/runs/state.py (pure FSM, fully unit-tested).
Schema: see app/models/db.py (Run, RunMessage, RunEvent, RunIdempotency).
Wire spec: see runs.openapi.json at the repo root.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import verify_api_key
from app.config import settings
from app.models.database import get_db
from app.models.db import Run, RunEvent, RunIdempotency, RunMessage
from app.runs.ids import new_run_id
from app.runs.state import (
    EventKind, InvalidTransition, RunCtx, RunStatus,
    advance, clamp_max_turns,
)
from app.runs import worker as run_worker

logger = logging.getLogger(__name__)
router = APIRouter()

_IDEMPOTENCY_TTL_SEC = 24 * 60 * 60          # Q1 lock-in
_IDEMPOTENCY_KEY_MAX_LEN = 256               # locked with hub team
_DEFAULT_TURNS_CEILING = 50                  # Q6 lock-in
_HARD_TURNS_CEILING = 200                    # Q6 lock-in
_RING_BUFFER_SIZE = 1000                     # SSE Last-Event-ID resume window


# ── helpers ──────────────────────────────────────────────────────────────────


def _runs_max_turns_ceiling() -> int:
    """Admin-tunable default ceiling, clamped to the hard ceiling."""
    val = getattr(settings, "runs_max_turns_ceiling", _DEFAULT_TURNS_CEILING)
    try:
        v = int(val)
    except Exception:
        v = _DEFAULT_TURNS_CEILING
    return max(1, min(v, _HARD_TURNS_CEILING))


def _ctx_from_row(r: Run) -> RunCtx:
    return RunCtx(
        status=RunStatus(r.status),
        deadline_ts=r.deadline_ts,
        max_turns=r.max_turns,
        turns_used=(r.model_calls or 0),
    )


def _serialize_run(r: Run) -> dict:
    """Spec B.3 GET /v1/runs/<id> response shape."""
    out = {
        "run_id": r.id,
        "status": r.status,
        "current_step": r.current_step,
        "model_calls": r.model_calls or 0,
        "tool_calls": r.tool_calls or 0,
        "tokens_in": r.tokens_in or 0,
        "tokens_out": r.tokens_out or 0,
        "context_summarized_at_turn": r.context_summarized_at_turn,
        "last_provider_used": r.last_provider_id,
        "deadline_ts": r.deadline_ts,
        "owner_node_id": r.owner_node_id,
        "result": r.result_text,
        "error": (
            None if not r.error_kind
            else {"kind": r.error_kind, "message": r.error_message}
        ),
        "current_tool_use": None,
    }
    if r.status == RunStatus.REQUIRES_TOOL.value and r.current_tool_use_id:
        out["current_tool_use"] = {
            "tool_use_id": r.current_tool_use_id,
            "name": r.current_tool_name,
            "input": r.current_tool_input or {},
        }
    return out


async def _next_event_seq(db: AsyncSession, run_id: str) -> int:
    """Monotonic event seq per run. Linear scan is fine — tests, low volume."""
    from sqlalchemy import func
    res = await db.execute(
        select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
    )
    cur = res.scalar()
    return (cur or 0) + 1


async def _emit_event(db: AsyncSession, run_id: str, kind: str, payload: dict) -> int:
    seq = await _next_event_seq(db, run_id)
    db.add(RunEvent(
        run_id=run_id, seq=seq, kind=kind,
        payload=payload or {}, ts=time.time(),
    ))
    return seq


async def _persist_transition(db: AsyncSession, run: Run, transition) -> None:
    run.status = transition.status.value
    run.updated_at = time.time()
    if transition.status.terminal:
        run.completed_at = run.updated_at
    if transition.error_kind:
        run.error_kind = transition.error_kind.value
        run.error_message = transition.error_message
    # turns_used → model_calls; the FSM increments on model returns
    if transition.turns_used != (run.model_calls or 0):
        run.model_calls = transition.turns_used


# ── POST /v1/runs ────────────────────────────────────────────────────────────


@router.post("/v1/runs")
async def create_run(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None, alias="authorization"),
):
    """Create a Run.

    Idempotency: ``(api_key_id, idempotency_key)`` returns the existing run
    on duplicate within 24h, regardless of state. Q1 lock-in.
    """
    # Accept either x-api-key or Authorization: Bearer ...
    raw_key = x_api_key
    if not raw_key and authorization:
        if authorization.lower().startswith("bearer "):
            raw_key = authorization[7:].strip()
    if not raw_key:
        raise HTTPException(401, "missing api key")
    key_record = await verify_api_key(db, raw_key)

    body = await request.json()
    idempotency_key = body.get("idempotency_key")
    if idempotency_key is not None:
        if not isinstance(idempotency_key, str):
            raise HTTPException(400, "idempotency_key must be a string")
        if len(idempotency_key) > _IDEMPOTENCY_KEY_MAX_LEN:
            raise HTTPException(400, f"idempotency_key exceeds {_IDEMPOTENCY_KEY_MAX_LEN} chars")

    deadline_ts = body.get("deadline_ts")
    if not isinstance(deadline_ts, (int, float)) or deadline_ts <= 0:
        raise HTTPException(400, "deadline_ts (unix seconds, future) is required")

    requested_turns = int(body.get("max_turns") or _DEFAULT_TURNS_CEILING)
    max_turns, was_clamped = clamp_max_turns(
        requested_turns, _runs_max_turns_ceiling(), _HARD_TURNS_CEILING,
    )

    # Idempotency replay
    if idempotency_key is not None:
        res = await db.execute(
            select(RunIdempotency).where(
                RunIdempotency.api_key_id == key_record.id,
                RunIdempotency.idempotency_key == idempotency_key,
            )
        )
        existing = res.scalar_one_or_none()
        if existing and (time.time() - existing.created_at) < _IDEMPOTENCY_TTL_SEC:
            run_res = await db.execute(select(Run).where(Run.id == existing.run_id))
            run = run_res.scalar_one_or_none()
            if run is not None:
                return JSONResponse({
                    "run_id": run.id,
                    "status": run.status,
                    "idempotent": True,
                })

    # Fresh run
    now = time.time()
    run = Run(
        id=new_run_id(),
        api_key_id=key_record.id,
        owner_node_id=settings.cluster_node_id or "local",
        status=RunStatus.QUEUED.value,
        current_step=None,
        deadline_ts=float(deadline_ts),
        max_turns=max_turns,
        model_preference=body.get("model_preference") or [],
        compaction_model=body.get("compaction_model"),
        system_prompt=body.get("system"),
        tools_spec=body.get("tools") or [],
        metadata_json=body.get("metadata") or {},
        trace_id=body.get("trace_id"),
        model_calls=0, tool_calls=0,
        tokens_in=0, tokens_out=0,
        created_at=now, updated_at=now,
    )
    db.add(run)

    # Conversation seed: system + caller-provided messages
    seq = 0
    if run.system_prompt:
        seq += 1
        db.add(RunMessage(
            run_id=run.id, seq=seq, role="system",
            content=run.system_prompt, tokens=0, created_at=now,
        ))
    for msg in (body.get("messages") or []):
        seq += 1
        db.add(RunMessage(
            run_id=run.id, seq=seq, role=msg.get("role", "user"),
            content=msg.get("content", ""), tokens=0, created_at=now,
        ))

    if idempotency_key is not None:
        db.add(RunIdempotency(
            api_key_id=key_record.id,
            idempotency_key=idempotency_key,
            run_id=run.id,
            created_at=now,
        ))

    if was_clamped:
        await _emit_event(db, run.id, "max_turns_clamped", {
            "requested": requested_turns, "clamped_to": max_turns,
            "ceiling": _runs_max_turns_ceiling(),
            "hard_ceiling": _HARD_TURNS_CEILING,
        })

    await db.commit()
    # R2: spawn worker so the run actually progresses.
    run_worker.spawn(run.id)
    return JSONResponse({"run_id": run.id, "status": run.status})


# ── GET /v1/runs/{run_id} ────────────────────────────────────────────────────


@router.get("/v1/runs/{run_id}")
async def get_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None, alias="authorization"),
):
    raw_key = x_api_key or (
        authorization[7:].strip() if authorization
        and authorization.lower().startswith("bearer ") else None
    )
    if not raw_key:
        raise HTTPException(401, "missing api key")
    key_record = await verify_api_key(db, raw_key)

    res = await db.execute(select(Run).where(Run.id == run_id))
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    if run.api_key_id != key_record.id:
        # Q5 lock-in: same-key required
        raise HTTPException(403, "run is owned by a different api key")
    return JSONResponse(_serialize_run(run))


# ── POST /v1/runs/{run_id}/cancel ────────────────────────────────────────────


@router.post("/v1/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None, alias="authorization"),
):
    raw_key = x_api_key or (
        authorization[7:].strip() if authorization
        and authorization.lower().startswith("bearer ") else None
    )
    if not raw_key:
        raise HTTPException(401, "missing api key")
    key_record = await verify_api_key(db, raw_key)

    res = await db.execute(select(Run).where(Run.id == run_id))
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    if run.api_key_id != key_record.id:
        raise HTTPException(403, "run is owned by a different api key")

    # Hub team flag B: cancel is idempotent at the FSM level (advance returns
    # CANCELLED again on a duplicate call), but we MUST NOT re-emit a
    # `cancelled` event — both sides observed the first one and a duplicate
    # would surface as a noisy double `task_terminate` mirror op. Snapshot
    # the prior status so we know whether this call is the one that actually
    # did the work.
    was_already_cancelled = run.status == RunStatus.CANCELLED.value
    transition = advance(_ctx_from_row(run), EventKind.CANCEL, time.time())
    await _persist_transition(db, run, transition)
    if transition.status is RunStatus.CANCELLED and not was_already_cancelled:
        await _emit_event(db, run.id, "cancelled", {})
    await db.commit()
    # R2: wake the worker so it sees the cancel and exits its idle wait.
    run_worker.wake(run.id)
    return JSONResponse(_serialize_run(run))


# ── POST /v1/runs/{run_id}/tool_result ───────────────────────────────────────


@router.post("/v1/runs/{run_id}/tool_result")
async def post_tool_result(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None, alias="authorization"),
):
    """Spec B.4. R1 accepts and validates the message; R2's worker
    consumes the tool_result and resumes the agent loop.

    Validation done here (per spec failure modes):
      - mismatched tool_use_id → 409
      - run not in requires_tool → 409
      - run already cancelled → 410 (tool_result post-cancel is gone)
      - mismatched bearer → 403
    """
    raw_key = x_api_key or (
        authorization[7:].strip() if authorization
        and authorization.lower().startswith("bearer ") else None
    )
    if not raw_key:
        raise HTTPException(401, "missing api key")
    key_record = await verify_api_key(db, raw_key)

    body = await request.json()
    tool_use_id = body.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        raise HTTPException(400, "tool_use_id is required")

    res = await db.execute(select(Run).where(Run.id == run_id))
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    if run.api_key_id != key_record.id:
        raise HTTPException(403, "run is owned by a different api key")

    if run.status == RunStatus.CANCELLED.value:
        raise HTTPException(410, "run was cancelled; tool result discarded")
    if run.status != RunStatus.REQUIRES_TOOL.value:
        raise HTTPException(
            409,
            f"run is in state {run.status!r}; tool_result only valid in 'requires_tool'",
        )
    if run.current_tool_use_id != tool_use_id:
        raise HTTPException(
            409,
            f"tool_use_id mismatch: run is awaiting {run.current_tool_use_id!r}",
        )

    # Append the tool_result message to the conversation; FSM transitions to RUNNING.
    seq_res = await db.execute(
        select(RunMessage.seq).where(RunMessage.run_id == run.id)
        .order_by(RunMessage.seq.desc()).limit(1)
    )
    last_seq = (seq_res.scalar() or 0) + 1
    db.add(RunMessage(
        run_id=run.id, seq=last_seq, role="user",
        content=[{
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": body.get("output_text") or "",
            "is_error": bool(body.get("is_error", False)),
        }],
        tokens=0, created_at=time.time(),
    ))
    transition = advance(_ctx_from_row(run), EventKind.TOOL_RESULT, time.time())
    await _persist_transition(db, run, transition)
    run.tool_calls = (run.tool_calls or 0) + 1
    run.current_tool_use_id = None
    run.current_tool_name = None
    run.current_tool_input = None
    await _emit_event(db, run.id, "tool_use_received", {
        "tool_use_id": tool_use_id,
        "exec_ms": body.get("exec_ms"),
    })
    await db.commit()
    # R2: worker is parked waiting for tool_result. Poke it via the per-run
    # wakeup Event so it picks up the new conversation tail without polling.
    # If the worker isn't on this node (cluster handoff, R5), spawn one
    # locally — the FSM is the source of truth, the task is just plumbing.
    run_worker.wake(run.id)
    run_worker.spawn(run.id)
    return JSONResponse(_serialize_run(run))


# ── GET /v1/runs/{run_id}/events ─────────────────────────────────────────────


@router.get("/v1/runs/{run_id}/events")
async def get_events(
    run_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    since_ms: int = 0,
    limit: int = 100,
    x_api_key: Optional[str] = Header(None, alias="x-api-key"),
    authorization: Optional[str] = Header(None, alias="authorization"),
    last_event_id: Optional[str] = Header(None, alias="last-event-id"),
    accept: Optional[str] = Header(None, alias="accept"),
):
    """Spec B.6. Two modes:
      - SSE: ``Accept: text/event-stream`` returns a live stream until the
        run terminates. ``Last-Event-ID`` header replays from that seq.
      - Polling: ``?since_ms=`` (ms epoch) returns JSON array of events
        with ts > since_ms.

    R1 emits the persisted ring-buffer contents and (for SSE) a single
    ``: keepalive`` heartbeat every 15s. R2 wires the live event bus so
    new events arrive within ~50ms; for now polling is the canonical
    path until R4 lands the in-memory ring-buffer broker.
    """
    raw_key = x_api_key or (
        authorization[7:].strip() if authorization
        and authorization.lower().startswith("bearer ") else None
    )
    if not raw_key:
        raise HTTPException(401, "missing api key")
    key_record = await verify_api_key(db, raw_key)

    res = await db.execute(select(Run).where(Run.id == run_id))
    run = res.scalar_one_or_none()
    if run is None:
        raise HTTPException(404, "run not found")
    if run.api_key_id != key_record.id:
        raise HTTPException(403, "run is owned by a different api key")

    is_sse = "text/event-stream" in (accept or "").lower()
    resume_seq = 0
    if last_event_id:
        try:
            resume_seq = int(last_event_id)
        except ValueError:
            resume_seq = 0

    if is_sse:
        async def gen():
            # Replay any events past resume_seq
            ev_res = await db.execute(
                select(RunEvent).where(
                    RunEvent.run_id == run.id, RunEvent.seq > resume_seq,
                ).order_by(RunEvent.seq.asc())
            )
            for ev in ev_res.scalars().all():
                yield (
                    f"event: {ev.kind}\n"
                    f"id: {ev.seq}\n"
                    f"data: {json.dumps(ev.payload, ensure_ascii=True)}\n\n"
                ).encode()
            # R1: simple polling loop with 15s keepalive. R4 replaces this
            # with an in-memory broker driven by the worker.
            last_seen = resume_seq
            idle_iters = 0
            while not run.status in (
                RunStatus.COMPLETED.value, RunStatus.FAILED.value,
                RunStatus.EXPIRED.value, RunStatus.CANCELLED.value,
            ):
                if await request.is_disconnected():
                    return
                await asyncio.sleep(1.0)
                idle_iters += 1
                # Re-read run + new events
                fresh = await db.execute(select(Run).where(Run.id == run.id))
                run_fresh = fresh.scalar_one_or_none()
                if run_fresh is None:
                    return
                # Pylance: refresh into outer var so loop exit cond updates
                run.status = run_fresh.status
                run.completed_at = run_fresh.completed_at
                ev_res2 = await db.execute(
                    select(RunEvent).where(
                        RunEvent.run_id == run.id, RunEvent.seq > last_seen,
                    ).order_by(RunEvent.seq.asc())
                )
                for ev in ev_res2.scalars().all():
                    yield (
                        f"event: {ev.kind}\n"
                        f"id: {ev.seq}\n"
                        f"data: {json.dumps(ev.payload, ensure_ascii=True)}\n\n"
                    ).encode()
                    last_seen = ev.seq
                    idle_iters = 0
                if idle_iters >= 15:
                    yield b": keepalive\n\n"
                    idle_iters = 0
        return StreamingResponse(gen(), media_type="text/event-stream")

    # Polling JSON path
    since_ts = (since_ms / 1000.0) if since_ms else 0.0
    ev_res = await db.execute(
        select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.ts > since_ts,
        ).order_by(RunEvent.seq.asc()).limit(max(1, min(limit, _RING_BUFFER_SIZE)))
    )
    events = [
        {"seq": ev.seq, "kind": ev.kind, "payload": ev.payload, "ts_ms": int(ev.ts * 1000)}
        for ev in ev_res.scalars().all()
    ]
    return JSONResponse({"events": events})
