"""Run worker — drives the agent loop for a single Run (R2).

Architecture
------------
One ``asyncio.Task`` per Run, spawned from POST /v1/runs after commit and
also by the recovery sweep on startup. The task runs ``_drive(run_id)``
which loops:

    while not terminal:
        if status == 'queued': advance(START)
        if status == 'running': call_model(); on text → advance(MODEL_RETURNED_TEXT)
                                              on tool_use → advance(MODEL_RETURNED_TOOL); wait_for_event
        if status == 'requires_tool': wait_for_event (tool_result POST sets it)
        if status == 'terminal': emit terminal event, return

Per-call hard deadline: ``asyncio.wait_for`` wraps every ``acompletion`` so
``httpx.ConnectTimeout`` / ``httpx.ReadTimeout`` fire fail-over IMMEDIATELY
— never block the run for 600s. This is the headline B.7 fix.

Provider failover: reuses the existing ``select_provider`` /
``try_ranked_non_streaming``-style chain. ``claude-oauth`` providers are
excluded from Run routing — Q4: tools never execute in-proxy, so the
OAuth /v1/messages dispatch path is irrelevant here.

Cluster sticky: the worker only runs on the node whose
``settings.cluster_node_id`` matches ``run.owner_node_id``. R5 wires the
handoff; for now non-owner nodes just don't pick up work.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import httpx
import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.db import Run, RunEvent, RunMessage
from app.routing.circuit_breaker import is_billing_error
from app.routing.lmrh import LMRHHint, parse_hint  # parse_hint is unused here but kept for parity
from app.routing.retry import acompletion_with_retry
from app.routing.router import select_provider
from app.runs.state import (
    EventKind, ErrorKind, InvalidTransition, RunCtx, RunStatus,
    advance,
)

logger = logging.getLogger(__name__)


# ── In-process registry: run_id → wakeup Event ──────────────────────────────
# Used so POST /v1/runs/<id>/tool_result can poke the worker out of its
# wait without polling. Only valid on the owner node — R5 introduces a
# cross-node "knock" via /cluster sync.
_WAKEUPS: dict[str, asyncio.Event] = {}
_TASKS: dict[str, asyncio.Task] = {}


def wake(run_id: str) -> None:
    """Public API used by the tool_result and cancel handlers."""
    ev = _WAKEUPS.get(run_id)
    if ev is not None:
        ev.set()


def _now() -> float:
    return time.time()


# ── Per-call timeout helper (the headline B.7 fix) ──────────────────────────


def _per_call_deadline_sec(provider_timeout: Optional[int], deadline_ts: float) -> float:
    """Pick the tighter of (per-provider read timeout, run remaining time).

    Connect-timeout is enforced inside the httpx client litellm uses; we
    add an outer ``asyncio.wait_for`` so any hang anywhere in the call —
    DNS, TLS, write-half — fails fast. 60s is the spec default; provider
    rows can pin it tighter (e.g. 30s for haiku via ``timeout_sec``)."""
    base = float(provider_timeout if provider_timeout and provider_timeout > 0 else 60)
    remaining = max(1.0, deadline_ts - _now())
    return min(base, remaining)


def _is_timeout_err(exc: BaseException) -> bool:
    """ConnectTimeout / ReadTimeout / asyncio.TimeoutError → fail-over."""
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout)):
        return True
    # litellm wraps these into its own classes; check the str form too
    s = str(exc)
    return ("Timeout" in s and "litellm" in s) or "ReadTimeout" in s or "ConnectTimeout" in s


# ── DB helpers (the worker does its own session per step) ───────────────────


async def _emit(db: AsyncSession, run_id: str, kind: str, payload: dict) -> None:
    from sqlalchemy import func
    res = await db.execute(
        select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
    )
    seq = (res.scalar() or 0) + 1
    db.add(RunEvent(run_id=run_id, seq=seq, kind=kind, payload=payload, ts=_now()))


def _ctx_from_row(r: Run) -> RunCtx:
    return RunCtx(
        status=RunStatus(r.status),
        deadline_ts=r.deadline_ts,
        max_turns=r.max_turns,
        turns_used=(r.model_calls or 0),
    )


async def _persist_transition(db: AsyncSession, run: Run, transition) -> None:
    run.status = transition.status.value
    run.updated_at = _now()
    if transition.status.terminal:
        run.completed_at = run.updated_at
    if transition.error_kind:
        run.error_kind = transition.error_kind.value
        run.error_message = transition.error_message
    if transition.turns_used != (run.model_calls or 0):
        run.model_calls = transition.turns_used


async def _load_messages(db: AsyncSession, run_id: str) -> list[dict]:
    res = await db.execute(
        select(RunMessage).where(RunMessage.run_id == run_id)
        .order_by(RunMessage.seq.asc())
    )
    out = []
    for m in res.scalars().all():
        out.append({"role": m.role, "content": m.content})
    return out


async def _append_assistant_message(
    db: AsyncSession, run_id: str, content
) -> int:
    """Append an assistant message row; return its seq."""
    from sqlalchemy import func
    res = await db.execute(
        select(func.max(RunMessage.seq)).where(RunMessage.run_id == run_id)
    )
    seq = (res.scalar() or 0) + 1
    db.add(RunMessage(
        run_id=run_id, seq=seq, role="assistant",
        content=content, tokens=0, created_at=_now(),
    ))
    return seq


# ── Single model call with hard timeout + failover ─────────────────────────


async def _call_model_once(
    db: AsyncSession,
    run: Run,
    messages: list[dict],
    *,
    excluded_provider_ids: set[str],
) -> tuple[object, str, str, float]:
    """Pick a provider, call litellm with a hard outer deadline, return
    ``(response, provider_id, model, latency_sec)``.

    Raises:
        TimeoutFailover  — connect/read timeout; caller should retry next
        BillingHardStop  — billing 4xx; caller should fail the run
        Exception        — anything else — caller decides
    """
    # ``select_provider`` accepts a single ``exclude_provider_id``; for the
    # multi-provider case we walk it manually like fallback.py does.
    seed: Optional[str] = next(iter(excluded_provider_ids), None)
    last_exc: Optional[Exception] = None
    for _attempt in range(8):  # safety cap; real cap is fallback_max_providers
        try:
            route = await select_provider(
                db, hint=None,
                has_tools=bool(run.tools_spec),
                has_images=False,
                key_type="standard",
                pinned_provider_id=None,
                model_override=None,
                exclude_provider_id=seed,
                excluded_provider_types={"claude-oauth"},
            )
        except RuntimeError as e:
            raise ProviderExhausted(str(e)) from e

        if route.provider.id in excluded_provider_ids:
            # select_provider returned a tried provider; expand the seed
            # and re-pick. fallback.py does the same dance.
            excluded_provider_ids.add(route.provider.id)
            seed = route.provider.id
            continue

        # Emit model_call_start
        body_estimate_bytes = len(
            json.dumps({"messages": messages, "model": route.litellm_model})
        )
        await _emit(db, run.id, "model_call_start", {
            "provider_id": route.provider.id,
            "provider_name": route.provider.name,
            "model": route.litellm_model,
            "attempt": len(excluded_provider_ids) + 1,
            "tokens_in_estimate": body_estimate_bytes // 4,  # rough char→token
        })
        await db.commit()  # flush event so SSE consumers see it before the call

        deadline = _per_call_deadline_sec(route.provider.timeout_sec, run.deadline_ts)
        kwargs = dict(route.litellm_kwargs or {})
        if run.tools_spec:
            # Tools pass through to litellm — Anthropic-format here, we'll
            # translate per-provider in R3 alongside compaction.
            kwargs["tools"] = run.tools_spec
        kwargs["api_key"] = route.provider.api_key
        if route.provider.base_url:
            kwargs["api_base"] = route.provider.base_url

        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                acompletion_with_retry(
                    model=route.litellm_model,
                    messages=messages,
                    **kwargs,
                ),
                timeout=deadline,
            )
        except (asyncio.TimeoutError, httpx.ConnectTimeout,
                httpx.ReadTimeout, httpx.PoolTimeout) as e:
            latency = time.monotonic() - t0
            await _emit(db, run.id, "model_call_end", {
                "provider_id": route.provider.id,
                "latency_ms": int(latency * 1000),
                "status": "timeout",
                "error": type(e).__name__,
                "tokens_in": 0, "tokens_out": 0,
                "bytes_in": body_estimate_bytes, "bytes_out": 0,
            })
            await _emit(db, run.id, "provider_failed", {
                "provider_id": route.provider.id,
                "error": f"{type(e).__name__}: {str(e) or 'no message'}",
                "will_retry": True,
            })
            await db.commit()
            excluded_provider_ids.add(route.provider.id)
            seed = route.provider.id
            last_exc = e
            continue
        except Exception as e:
            latency = time.monotonic() - t0
            await _emit(db, run.id, "model_call_end", {
                "provider_id": route.provider.id,
                "latency_ms": int(latency * 1000),
                "status": "error",
                "error": f"{type(e).__name__}: {str(e) or 'no message'}",
                "tokens_in": 0, "tokens_out": 0,
                "bytes_in": body_estimate_bytes, "bytes_out": 0,
            })
            await db.commit()
            if is_billing_error(str(e)):
                raise BillingHardStop(str(e)) from e
            await _emit(db, run.id, "provider_failed", {
                "provider_id": route.provider.id,
                "error": f"{type(e).__name__}: {str(e) or 'no message'}",
                "will_retry": True,
            })
            await db.commit()
            excluded_provider_ids.add(route.provider.id)
            seed = route.provider.id
            last_exc = e
            continue

        latency = time.monotonic() - t0
        # Successful response → emit model_call_end with usage
        try:
            usage = getattr(resp, "usage", None)
            in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
            out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
        except Exception:
            in_tok, out_tok = 0, 0
        try:
            resp_dict = resp.model_dump() if hasattr(resp, "model_dump") else None
            bytes_out = len(json.dumps(resp_dict)) if resp_dict else 0
        except Exception:
            bytes_out = 0
        await _emit(db, run.id, "model_call_end", {
            "provider_id": route.provider.id,
            "latency_ms": int(latency * 1000),
            "status": "ok",
            "tokens_in": in_tok, "tokens_out": out_tok,
            "bytes_in": body_estimate_bytes, "bytes_out": bytes_out,
        })
        run.tokens_in = (run.tokens_in or 0) + in_tok
        run.tokens_out = (run.tokens_out or 0) + out_tok
        run.last_provider_id = route.provider.id
        await db.commit()
        return resp, route.provider.id, route.litellm_model, latency

    if last_exc is not None:
        raise ProviderExhausted("all providers timed out or failed") from last_exc
    raise ProviderExhausted("no provider could be selected")


class ProviderExhausted(Exception):
    """All providers in the chain returned non-retriable failures."""


class BillingHardStop(Exception):
    """Billing-related 4xx — surface immediately as run-level failure."""


# ── Response → next-step interpretation ─────────────────────────────────────


def _extract_assistant_content(resp) -> list[dict]:
    """litellm wraps OpenAI-shape; pull the assistant message and translate
    into Anthropic content blocks (text + tool_use)."""
    try:
        msg = resp.choices[0].message
    except Exception:
        return [{"type": "text", "text": ""}]
    blocks: list[dict] = []
    content_str = getattr(msg, "content", None) or ""
    if content_str:
        blocks.append({"type": "text", "text": content_str})
    tool_calls = getattr(msg, "tool_calls", None) or []
    for tc in tool_calls:
        fn = getattr(tc, "function", None)
        if not fn:
            continue
        try:
            args = json.loads(getattr(fn, "arguments", "{}") or "{}")
        except Exception:
            args = {}
        blocks.append({
            "type": "tool_use",
            "id": getattr(tc, "id", None) or f"toolu_{int(time.time()*1000)}",
            "name": getattr(fn, "name", "") or "",
            "input": args,
        })
    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def _first_tool_use(blocks: list[dict]) -> Optional[dict]:
    for b in blocks:
        if b.get("type") == "tool_use":
            return b
    return None


def _terminal_text(blocks: list[dict]) -> str:
    parts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    return "\n".join(p for p in parts if p)


# ── Worker driver ───────────────────────────────────────────────────────────


async def _drive(run_id: str) -> None:
    """Lifetime of one Run on this node. Returns when the run is terminal.

    The worker re-opens its DB session at every checkpoint so a slow
    upstream call doesn't pin a connection for 60s. The wakeup Event lets
    /tool_result + /cancel poke us out of an idle wait.
    """
    wakeup = _WAKEUPS.setdefault(run_id, asyncio.Event())
    excluded_provider_ids: set[str] = set()

    while True:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Run).where(Run.id == run_id))
            run = res.scalar_one_or_none()
            if run is None:
                logger.info("runs.worker.run_gone", extra={"run_id": run_id})
                _WAKEUPS.pop(run_id, None)
                return

            ctx = _ctx_from_row(run)

            # Deadline check at every iteration boundary — the spec demands
            # mid-run expiry never strands the client.
            if not ctx.status.terminal and _now() >= run.deadline_ts:
                t = advance(ctx, EventKind.DEADLINE_EXCEEDED, _now())
                await _persist_transition(db, run, t)
                await _emit(db, run.id, "expired", {
                    "deadline_ts": run.deadline_ts,
                    "total_ms": int((run.completed_at - run.created_at) * 1000),
                })
                await db.commit()
                _WAKEUPS.pop(run_id, None)
                return

            # State-machine dispatch
            if ctx.status is RunStatus.QUEUED:
                t = advance(ctx, EventKind.START, _now())
                await _persist_transition(db, run, t)
                await _emit(db, run.id, "run_started", {})
                await db.commit()
                # fall through to next iteration which sees RUNNING
                continue

            if ctx.status is RunStatus.RUNNING:
                messages = await _load_messages(db, run_id)
                try:
                    resp, _pid, _model, _lat = await _call_model_once(
                        db, run, messages,
                        excluded_provider_ids=excluded_provider_ids,
                    )
                except ProviderExhausted as e:
                    t = advance(ctx, EventKind.PROVIDER_EXHAUSTED, _now(),
                                {"detail": str(e)})
                    await _persist_transition(db, run, t)
                    await _emit(db, run.id, "failed", {
                        "error": str(e),
                        "kind": ErrorKind.PROVIDER.value,
                        "last_provider_used": run.last_provider_id,
                    })
                    await db.commit()
                    _WAKEUPS.pop(run_id, None)
                    return
                except BillingHardStop as e:
                    t = advance(ctx, EventKind.INTERNAL_ERROR, _now(),
                                {"detail": f"billing: {e}"})
                    await _persist_transition(db, run, t)
                    await _emit(db, run.id, "failed", {
                        "error": str(e),
                        "kind": ErrorKind.INTERNAL.value,
                        "last_provider_used": run.last_provider_id,
                    })
                    await db.commit()
                    _WAKEUPS.pop(run_id, None)
                    return
                except Exception as e:
                    t = advance(ctx, EventKind.INTERNAL_ERROR, _now(),
                                {"detail": f"{type(e).__name__}: {str(e) or 'no message'}"})
                    await _persist_transition(db, run, t)
                    await _emit(db, run.id, "failed", {
                        "error": str(e), "kind": ErrorKind.INTERNAL.value,
                        "last_provider_used": run.last_provider_id,
                    })
                    await db.commit()
                    _WAKEUPS.pop(run_id, None)
                    return

                blocks = _extract_assistant_content(resp)
                await _append_assistant_message(db, run_id, blocks)
                # Reset failover exclusions on a successful call
                excluded_provider_ids = set()

                tool_use = _first_tool_use(blocks)
                if tool_use is not None:
                    # MODEL_RETURNED_TOOL — transition + park
                    t = advance(_ctx_from_row(run), EventKind.MODEL_RETURNED_TOOL, _now())
                    await _persist_transition(db, run, t)
                    if t.status is RunStatus.REQUIRES_TOOL:
                        run.current_tool_use_id = tool_use["id"]
                        run.current_tool_name = tool_use["name"]
                        run.current_tool_input = tool_use["input"]
                        await _emit(db, run.id, "tool_use_requested", {
                            "tool_use_id": tool_use["id"],
                            "name": tool_use["name"],
                            "input": tool_use["input"],
                        })
                    elif t.status is RunStatus.FAILED and t.error_kind is ErrorKind.TOOL_LOOP_EXCEEDED:
                        await _emit(db, run.id, "failed", {
                            "error": t.error_message,
                            "kind": ErrorKind.TOOL_LOOP_EXCEEDED.value,
                            "last_provider_used": run.last_provider_id,
                        })
                        await db.commit()
                        _WAKEUPS.pop(run_id, None)
                        return
                    elif t.status is RunStatus.EXPIRED:
                        await _emit(db, run.id, "expired", {
                            "deadline_ts": run.deadline_ts,
                            "total_ms": int((run.completed_at - run.created_at) * 1000),
                        })
                        await db.commit()
                        _WAKEUPS.pop(run_id, None)
                        return
                    await db.commit()
                    # Fall through; outer loop will pick up REQUIRES_TOOL and wait.
                    continue
                else:
                    # MODEL_RETURNED_TEXT → completed
                    t = advance(_ctx_from_row(run), EventKind.MODEL_RETURNED_TEXT, _now())
                    await _persist_transition(db, run, t)
                    if t.status is RunStatus.COMPLETED:
                        run.result_text = _terminal_text(blocks)
                        await _emit(db, run.id, "completed", {
                            "result_text": run.result_text,
                            "total_ms": int((run.completed_at - run.created_at) * 1000),
                        })
                    elif t.status is RunStatus.EXPIRED:
                        await _emit(db, run.id, "expired", {
                            "deadline_ts": run.deadline_ts,
                            "total_ms": int((run.completed_at - run.created_at) * 1000),
                        })
                    await db.commit()
                    _WAKEUPS.pop(run_id, None)
                    return

            if ctx.status is RunStatus.REQUIRES_TOOL:
                # Idle wait. Wakeup event is set by /tool_result, /cancel, or
                # by a periodic deadline-check tick.
                pass

            if ctx.status.terminal:
                _WAKEUPS.pop(run_id, None)
                return

        # Wait outside of the DB session so we don't pin a connection.
        try:
            # Tick at most every 5s so deadline-pasted runs catch the expire.
            timeout = max(0.1, min(5.0, run.deadline_ts - _now()))
            await asyncio.wait_for(wakeup.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        wakeup.clear()


def spawn(run_id: str) -> asyncio.Task:
    """Public API used by POST /v1/runs and the recovery sweep.

    Idempotent — if a worker already exists for this run, returns it."""
    existing = _TASKS.get(run_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_drive(run_id), name=f"run-worker:{run_id}")
    _TASKS[run_id] = task

    def _cleanup(_t: asyncio.Task) -> None:
        _TASKS.pop(run_id, None)
        _WAKEUPS.pop(run_id, None)

    task.add_done_callback(_cleanup)
    return task


# ── Recovery sweep (called from main.py lifespan) ───────────────────────────


async def recover_orphans() -> int:
    """On startup, restart workers for any runs this node owns that were
    in flight when the proxy died. Emits ``run_recovered`` so the hub
    timeline can render the boundary."""
    node_id = settings.cluster_node_id or "local"
    async with AsyncSessionLocal() as db:
        res = await db.execute(
            select(Run).where(
                Run.owner_node_id == node_id,
                Run.status.in_([RunStatus.RUNNING.value, RunStatus.REQUIRES_TOOL.value]),
            )
        )
        runs = list(res.scalars().all())
        for r in runs:
            await _emit(db, r.id, "run_recovered", {
                "prior_status": r.status,
                "recovered_from_node_id": node_id,
                # We don't track the down-time precisely — best estimate is
                # since-updated_at, since the worker writes updated_at on
                # every transition.
                "downtime_ms": int((_now() - (r.updated_at or _now())) * 1000),
            })
        await db.commit()

    for r in runs:
        spawn(r.id)
    if runs:
        logger.info("runs.worker.recovered", extra={"count": len(runs)})
    return len(runs)
