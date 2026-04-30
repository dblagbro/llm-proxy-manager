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


def _rl_reset(run_id: str) -> None:
    """R6 helper: drop the per-run rate-limit bucket on terminal cleanup.

    Bound to all _WAKEUPS.pop() sites in _drive() so the bucket dict
    stays the same size as the active-run set. Defensive against the
    rate_limit module being importable but mid-reload."""
    try:
        from app.runs.rate_limit import reset as _rl_reset_inner
        _rl_reset_inner(run_id)
    except Exception:
        pass


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
    """R4: emit through the in-memory broker (sub-100ms to live SSE
    consumers) AND persist to RunEvent (durability + Last-Event-ID
    replay across worker restart). The DB row's seq is the canonical
    one — we read it back so the broker stays consistent."""
    from sqlalchemy import func
    from app.runs import event_bus
    res = await db.execute(
        select(func.max(RunEvent.seq)).where(RunEvent.run_id == run_id)
    )
    seq = (res.scalar() or 0) + 1
    ts = _now()
    db.add(RunEvent(run_id=run_id, seq=seq, kind=kind, payload=payload, ts=ts))
    # Publish to live subscribers immediately. The DB commit happens at
    # the worker's natural commit point; live consumers don't wait for it.
    event_bus.publish(run_id, seq=seq, kind=kind, payload=payload or {}, ts=ts)


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
    # R5: schedule cluster replication. Non-terminal pushes debounce 250ms;
    # terminal pushes sync-ack with 2s timeout + background retry. Fire after
    # the row is fully mutated so the snapshot reflects the post-transition
    # state. The replicate call is non-blocking (returns immediately for
    # debounced; awaits ack window for terminal — caller already committed).
    try:
        from app.runs.replication import replicate
        await replicate(run, terminal=transition.status.terminal)
    except Exception as e:
        # Replication failure must not break the run loop — peers reconcile
        # via the periodic /cluster/sync push as a safety net.
        logger.info("runs.replication.skip run=%s err=%s", run.id, e)


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
        # R3: translate Anthropic-format tools to the provider's native shape
        # (or fall back to PBTC emulation if native_tools=false on this
        # provider's capability profile). The worker keeps the canonical
        # Anthropic-format tools_spec on the Run row; per-call we adapt.
        emulation_prompt: Optional[str] = None
        if run.tools_spec:
            from app.runs.tools import adapt_tools_for_route
            native_tools = bool(getattr(route.profile, "native_tools", True)) if hasattr(route, "profile") else True
            tools_arg, emulation_prompt = adapt_tools_for_route(
                run.tools_spec,
                litellm_model=route.litellm_model,
                native_tools=native_tools,
            )
            if tools_arg is not None:
                kwargs["tools"] = tools_arg
        kwargs["api_key"] = route.provider.api_key
        if route.provider.base_url:
            kwargs["api_base"] = route.provider.base_url

        # If emulating tool-use, prepend the PBTC system prompt to the
        # message stream for THIS call only. The Run's stored conversation
        # stays canonical Anthropic shape.
        call_messages = messages
        if emulation_prompt:
            call_messages = [{"role": "system", "content": emulation_prompt}] + messages

        t0 = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                acompletion_with_retry(
                    model=route.litellm_model,
                    messages=call_messages,
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
#
# v3.0.8 (item 6): the original ``_drive`` was a 250-line nested-branch
# loop. Split into per-state handlers, each doing one transition's worth
# of work and returning a small ``_StepResult`` that tells the outer loop
# what to do next. Behavior is unchanged; readability is much improved
# and the per-state logic is now individually testable.
#
# Sentinel values returned from a handler:
#   _STEP_CONTINUE — keep looping (re-read run, re-dispatch)
#   _STEP_RETURN   — terminal; outer loop should clean up and return
#   _STEP_WAIT     — idle wait on the wakeup event with deadline-bounded timeout


_STEP_CONTINUE = "continue"
_STEP_RETURN = "return"
_STEP_WAIT = "wait"


async def _step_check_deadline(db, run, ctx) -> Optional[str]:
    """If the deadline has passed, transition to EXPIRED + emit + return.
    Otherwise None (caller proceeds)."""
    if ctx.status.terminal or _now() < run.deadline_ts:
        return None
    t = advance(ctx, EventKind.DEADLINE_EXCEEDED, _now())
    await _persist_transition(db, run, t)
    await _emit(db, run.id, "expired", {
        "deadline_ts": run.deadline_ts,
        "total_ms": int((run.completed_at - run.created_at) * 1000),
    })
    await db.commit()
    return _STEP_RETURN


async def _step_queued(db, run, ctx) -> str:
    """QUEUED → RUNNING; emit run_started; loop."""
    t = advance(ctx, EventKind.START, _now())
    await _persist_transition(db, run, t)
    await _emit(db, run.id, "run_started", {})
    await db.commit()
    return _STEP_CONTINUE


async def _peek_next_model(db, run) -> str:
    """Best-effort model id for compaction trigger. The actual call goes
    through _call_model_once which may re-route; this is just for the
    80%-context threshold check."""
    try:
        peek_route = await select_provider(
            db, hint=None,
            has_tools=bool(run.tools_spec),
            has_images=False,
            key_type="standard",
            excluded_provider_types={"claude-oauth"},
        )
        return peek_route.litellm_model
    except Exception:
        return "gpt-4o"


async def _maybe_compact_run(db, run, run_id, messages):
    """Run compaction if the conversation is at ≥80% of next-call model
    context. Returns (possibly-rewritten-messages, did_compact)."""
    next_model = await _peek_next_model(db, run)
    try:
        from app.runs.compaction import maybe_compact, apply_compaction_to_db
        compaction = await maybe_compact(
            db, run_id=run.id,
            run_compaction_model=run.compaction_model,
            next_call_model=next_model,
            messages=messages,
        )
    except Exception as ce:
        logger.warning("runs.compaction.failed run=%s err=%s", run.id, ce)
        compaction = None

    if compaction is None:
        return messages, False
    await apply_compaction_to_db(
        db, run_id=run.id, new_messages=compaction["new_messages"],
    )
    run.context_summarized_at_turn = run.model_calls or 0
    await _emit(db, run.id, "context_compacted", compaction["event"])
    await db.commit()
    # Reload after compaction so next call sees compacted message list
    return await _load_messages(db, run_id), True


async def _wait_for_rate_limit_slot(run):
    """Block until acquire() returns a slot; emit ``rate_limited`` event
    on first throttled attempt (R6 lock-in)."""
    try:
        from app.runs import rate_limit as _rl
        async def _emit_rl(payload):
            async with AsyncSessionLocal() as _rl_db:
                await _emit(_rl_db, run.id, "rate_limited", payload)
                await _rl_db.commit()
        await _rl.acquire(run.id, emit_callback=_emit_rl)
    except Exception as e:
        logger.info("runs.rate_limit.skip run=%s err=%s", run.id, e)


async def _fail_run(db, run, ctx, exc, kind):
    """Common failure path used by all 3 model-call exception classes.
    Advances FSM to FAILED with the right error_kind, emits ``failed``."""
    t = advance(ctx, EventKind.PROVIDER_EXHAUSTED if kind is ErrorKind.PROVIDER
                else EventKind.INTERNAL_ERROR, _now(),
                {"detail": str(exc) or repr(exc)})
    await _persist_transition(db, run, t)
    await _emit(db, run.id, "failed", {
        "error": str(exc),
        "kind": kind.value,
        "last_provider_used": run.last_provider_id,
    })
    await db.commit()


async def _step_running(db, run, run_id, ctx, excluded_provider_ids) -> str:
    """RUNNING: load conversation → maybe compact → rate-limit gate →
    call model → handle response (text completes; tool_use parks)."""
    messages = await _load_messages(db, run_id)
    messages, _did_compact = await _maybe_compact_run(db, run, run_id, messages)
    await _wait_for_rate_limit_slot(run)

    try:
        resp, _pid, _model, _lat = await _call_model_once(
            db, run, messages,
            excluded_provider_ids=excluded_provider_ids,
        )
    except ProviderExhausted as e:
        await _fail_run(db, run, ctx, e, ErrorKind.PROVIDER)
        return _STEP_RETURN
    except BillingHardStop as e:
        await _fail_run(db, run, ctx, f"billing: {e}", ErrorKind.INTERNAL)
        return _STEP_RETURN
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e) or 'no message'}"
        await _fail_run(db, run, ctx, detail, ErrorKind.INTERNAL)
        return _STEP_RETURN

    blocks = _extract_assistant_content(resp)
    await _append_assistant_message(db, run_id, blocks)
    # Reset failover exclusions on a successful call
    excluded_provider_ids.clear()

    tool_use = _first_tool_use(blocks)
    if tool_use is not None:
        return await _handle_tool_use(db, run, tool_use)
    return await _handle_terminal_text(db, run, blocks)


async def _handle_tool_use(db, run, tool_use) -> str:
    """Model returned a tool_use block → REQUIRES_TOOL or terminal-fail
    if max_turns hit / deadline already past."""
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
        await db.commit()
        return _STEP_CONTINUE  # outer loop sees REQUIRES_TOOL → waits

    if t.status is RunStatus.FAILED and t.error_kind is ErrorKind.TOOL_LOOP_EXCEEDED:
        await _emit(db, run.id, "failed", {
            "error": t.error_message,
            "kind": ErrorKind.TOOL_LOOP_EXCEEDED.value,
            "last_provider_used": run.last_provider_id,
        })
        await db.commit()
        return _STEP_RETURN

    if t.status is RunStatus.EXPIRED:
        await _emit(db, run.id, "expired", {
            "deadline_ts": run.deadline_ts,
            "total_ms": int((run.completed_at - run.created_at) * 1000),
        })
        await db.commit()
        return _STEP_RETURN

    # Defensive: any other terminal kind, just commit + exit
    await db.commit()
    return _STEP_RETURN


async def _handle_terminal_text(db, run, blocks) -> str:
    """Model returned text-only → COMPLETED (or EXPIRED if deadline hit)."""
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
    return _STEP_RETURN


async def _drive(run_id: str) -> None:
    """Lifetime of one Run on this node. Returns when the run is terminal.

    The worker re-opens its DB session at every checkpoint so a slow
    upstream call doesn't pin a connection for 60s. The wakeup Event lets
    /tool_result + /cancel poke us out of an idle wait.

    Loop: read run + ctx → check deadline → dispatch by status → either
    continue (re-read), return (terminal), or fall through to wait().
    Each per-state handler is its own function — see _step_* helpers
    above.
    """
    wakeup = _WAKEUPS.setdefault(run_id, asyncio.Event())
    excluded_provider_ids: set[str] = set()
    run = None  # for the wait() block at the bottom

    def _cleanup():
        _WAKEUPS.pop(run_id, None)
        _rl_reset(run_id)

    while True:
        async with AsyncSessionLocal() as db:
            res = await db.execute(select(Run).where(Run.id == run_id))
            run = res.scalar_one_or_none()
            if run is None:
                logger.info("runs.worker.run_gone", extra={"run_id": run_id})
                _cleanup()
                return

            ctx = _ctx_from_row(run)

            # Deadline check at every iteration boundary
            if (await _step_check_deadline(db, run, ctx)) == _STEP_RETURN:
                _cleanup()
                return

            # State-machine dispatch
            if ctx.status is RunStatus.QUEUED:
                if (await _step_queued(db, run, ctx)) == _STEP_CONTINUE:
                    continue

            elif ctx.status is RunStatus.RUNNING:
                step = await _step_running(db, run, run_id, ctx, excluded_provider_ids)
                if step == _STEP_RETURN:
                    _cleanup()
                    return
                if step == _STEP_CONTINUE:
                    continue
                # else fall through to wait

            elif ctx.status is RunStatus.REQUIRES_TOOL:
                # Idle wait. Wakeup event is set by /tool_result, /cancel,
                # or by the periodic deadline-check tick below.
                pass

            if ctx.status.terminal:
                _cleanup()
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
        _WAKEUPS.pop(run_id, None); _rl_reset(run_id)

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
