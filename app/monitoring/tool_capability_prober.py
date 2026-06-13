"""v3.8.4 (#264) — periodic tool-call probe + auto-native_tools adjustment.

Fires a standard ``get_weather(city)`` tool-call probe at every
(provider, default_model) on a configurable cadence (default daily).
Records each probe result to ``model_tool_probe``; a rolling window
of the last N probes drives ``ModelCapability.native_tools`` via
hysteresis:

  rolling_success >= ai_tool_prober_native_threshold (default 0.8)
    → native_tools = True  (trust native — emulation layer stays off)

  rolling_success < ai_tool_prober_emulate_threshold (default 0.6)
    → native_tools = False (engage <tool_call>-marker emulation)

  in between → no change (avoids flapping on borderline models)

Default OFF (operator opt-in via ``ai_tool_prober_enabled``). When
enabled the worker uses ``ai_tool_prober_internal_api_key`` to call
its own /v1/messages endpoint, mirroring the v3.7.10 AI rate limiter
pattern. X-Internal-Source header tags the request so its own
activity log row is filterable.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

WARMUP_DELAY_SEC = 120
_STARTUP_JITTER_MAX_SEC = 60.0
_TASK: Optional[asyncio.Task] = None

# Standard probe payload. Tool is small + unambiguous so we can
# verify the model called it correctly via simple JSON inspection.
_PROBE_TOOLS = [{
    "name": "get_weather",
    "description": "Get the current weather for a city.",
    "input_schema": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
        },
        "required": ["city"],
    },
}]

_PROBE_USER_MESSAGE = "What's the weather in San Francisco right now?"
_PROBE_EXPECTED_TOOL = "get_weather"
_PROBE_EXPECTED_ARG_KEY = "city"


def _enabled() -> bool:
    try:
        from app.config import settings
        return bool(getattr(settings, "ai_tool_prober_enabled", False))
    except Exception:
        return False


def _interval_sec() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "ai_tool_prober_interval_sec", 86400))
    except Exception:
        return 86400


def _native_threshold() -> float:
    try:
        from app.config import settings
        return float(getattr(settings, "ai_tool_prober_native_threshold", 0.8))
    except Exception:
        return 0.8


def _emulate_threshold() -> float:
    try:
        from app.config import settings
        return float(getattr(settings, "ai_tool_prober_emulate_threshold", 0.6))
    except Exception:
        return 0.6


def _success_window() -> int:
    try:
        from app.config import settings
        return int(getattr(settings, "ai_tool_prober_success_window", 5))
    except Exception:
        return 5


def evaluate_probe_response(body: dict) -> tuple[bool, bool, bool]:
    """Inspect a /v1/messages response for the expected tool_use block.

    Returns (called, parseable, correct_args).
      - called: response contained at least one tool_use block
      - parseable: the tool's name + input were structurally valid
      - correct_args: ``input`` contained the expected key
    """
    if not isinstance(body, dict):
        return False, False, False
    content = body.get("content")
    if not isinstance(content, list):
        return False, False, False
    tool_blocks = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
    if not tool_blocks:
        return False, False, False
    called = True
    # Look for our specific tool
    matching = [b for b in tool_blocks if b.get("name") == _PROBE_EXPECTED_TOOL]
    if not matching:
        return called, False, False
    b = matching[0]
    args = b.get("input")
    parseable = isinstance(args, dict)
    correct_args = parseable and _PROBE_EXPECTED_ARG_KEY in args and bool(args[_PROBE_EXPECTED_ARG_KEY])
    return called, parseable, correct_args


async def probe_one_model(db, provider, model_id: str) -> dict:
    """Run a single probe + persist the result. Returns a summary dict
    for caller logging. Does NOT update the capability flag — that's
    done in the sweep after computing rolling success."""
    from app.config import settings
    from app.models.db import ModelToolProbe

    api_key = getattr(settings, "ai_tool_prober_internal_api_key", None)
    if not api_key:
        return {"ok": False, "reason": "no_internal_api_key"}

    excerpt: Optional[str] = None
    error_text: Optional[str] = None
    response_format: Optional[str] = None
    called = parseable = correct_args = False

    try:
        async with httpx.AsyncClient(timeout=45.0, verify=False) as client:
            resp = await client.post(
                "http://localhost:3000/v1/messages",
                json={
                    "model": model_id,
                    "max_tokens": 300,
                    "messages": [{"role": "user", "content": _PROBE_USER_MESSAGE}],
                    "tools": _PROBE_TOOLS,
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                    # Tag so the probe's own activity_log row is filterable.
                    "X-Internal-Source": "ai_tool_prober",
                    # Pin routing to this provider so the probe actually
                    # tests THIS provider, not whichever the router picks.
                    "LLM-Hint": f"provider-hint={provider.name};require",
                },
            )
        if resp.status_code != 200:
            error_text = f"http {resp.status_code}: {resp.text[:300]}"
            excerpt = resp.text[:500]
        else:
            body = resp.json()
            excerpt = json.dumps(body, default=str)[:500]
            called, parseable, correct_args = evaluate_probe_response(body)
            # Best-effort tag: if response header indicates emulation,
            # record it. Otherwise default to "native".
            response_format = "emulated" if resp.headers.get("X-Emulation-Level") else "native"
    except httpx.RequestError as exc:
        error_text = f"network: {exc!s}"

    row = ModelToolProbe(
        provider_id=provider.id,
        model_id=model_id,
        called=called,
        parseable=parseable,
        correct_args=correct_args,
        error=error_text,
        raw_excerpt=excerpt,
        response_format=response_format,
    )
    db.add(row)
    await db.commit()
    return {
        "provider_id": provider.id,
        "model_id": model_id,
        "called": called,
        "parseable": parseable,
        "correct_args": correct_args,
        "error": error_text,
    }


async def update_native_tools_from_rolling(db, provider_id: str, model_id: str) -> Optional[bool]:
    """Inspect the last N probes for this (provider, model) and apply
    hysteresis to ModelCapability.native_tools. Returns the new value
    if changed, None if untouched.

    Treats ``correct_args=True`` as the success criterion. ``called=True``
    alone isn't enough — model might have called the WRONG tool or
    passed bad args.
    """
    from sqlalchemy import select, desc, update
    from app.models.db import ModelToolProbe, ModelCapability

    window = _success_window()
    rs = (await db.execute(
        select(ModelToolProbe.correct_args)
        .where(ModelToolProbe.provider_id == provider_id)
        .where(ModelToolProbe.model_id == model_id)
        .order_by(desc(ModelToolProbe.captured_at))
        .limit(window)
    )).all()
    if len(rs) < window:
        # Not enough samples yet for a stable decision
        return None

    success = sum(1 for (ok,) in rs if ok)
    rate = success / len(rs)
    native_thr = _native_threshold()
    emul_thr = _emulate_threshold()

    new_val: Optional[bool] = None
    if rate >= native_thr:
        new_val = True
    elif rate < emul_thr:
        new_val = False

    # v3.8.5 (#265): ALWAYS update tool_call_success_rate so the router
    # can weight candidates regardless of whether native_tools flipped.
    # The bool is hysteresis-gated; the rate is monotonic per probe.
    cap_rs = (await db.execute(
        select(ModelCapability)
        .where(ModelCapability.provider_id == provider_id)
        .where(ModelCapability.model_id == model_id)
    )).scalars().all()
    bool_changed = False
    for cap in cap_rs:
        cap.tool_call_success_rate = float(rate)
        if new_val is not None and cap.native_tools is not new_val:
            cap.native_tools = new_val
            bool_changed = True
    if cap_rs:
        await db.commit()
    if bool_changed and new_val is not None:
        logger.info(
            "ai_tool_prober.native_tools_changed provider_id=%s model=%s rate=%.2f new=%s",
            provider_id, model_id, rate, new_val,
        )
        return new_val
    return None


async def _probe_loop_once() -> dict:
    """One sweep: probe every enabled provider's default_model."""
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider

    out = {"probed": 0, "skipped": 0, "updates": 0}
    async with AsyncSessionLocal() as db:
        rs = (await db.execute(
            select(Provider)
            .where(Provider.enabled == True)  # noqa: E712
            .where(Provider.deleted_at.is_(None))
        )).scalars().all()
        for p in rs:
            model = p.default_model
            if not model:
                out["skipped"] += 1
                continue
            try:
                r = await probe_one_model(db, p, model)
                if r.get("ok") is False:
                    out["skipped"] += 1
                    continue
                out["probed"] += 1
                changed = await update_native_tools_from_rolling(db, p.id, model)
                if changed is not None:
                    out["updates"] += 1
            except Exception as e:
                logger.warning(
                    "ai_tool_prober.probe_crashed provider=%s model=%s err=%s",
                    p.name, model, e,
                )
                out["skipped"] += 1
    return out


async def _scan_loop() -> None:
    from app.monitoring.worker_heartbeat import WorkerHeartbeat, register_expected_interval
    hb = WorkerHeartbeat(name="tool_capability_prober")
    jitter = random.uniform(0.0, _STARTUP_JITTER_MAX_SEC)
    await asyncio.sleep(WARMUP_DELAY_SEC + jitter)
    while True:
        register_expected_interval("tool_capability_prober", _interval_sec())
        if not _enabled():
            await hb.tick(status="disabled", note="ai_tool_prober disabled")
            await asyncio.sleep(300)
            continue
        try:
            counts = await _probe_loop_once()
            if any(counts.values()):
                logger.info(
                    "ai_tool_prober.swept probed=%d skipped=%d native_tools_updates=%d",
                    counts["probed"], counts["skipped"], counts["updates"],
                )
            await hb.tick(
                status="ok",
                note=f"probed={counts.get('probed',0)} skipped={counts.get('skipped',0)}",
            )
        except Exception as e:
            logger.warning("ai_tool_prober.sweep_failed err=%s", e)
            await hb.tick(status="error", note=str(e)[:200])
        await asyncio.sleep(_interval_sec())


def start() -> None:
    """Spawn the prober loop. Idempotent. No-op behavior when feature
    flag is off (worker still runs, just sleeps without doing anything).
    """
    global _TASK
    if _TASK is not None and not _TASK.done():
        return
    _TASK = asyncio.create_task(_scan_loop(), name="ai-tool-prober-loop")
    logger.info(
        "ai_tool_prober.started — opt-in via AI_TOOL_PROBER_ENABLED=true",
    )
