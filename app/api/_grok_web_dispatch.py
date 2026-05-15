"""
Shared grok-web dispatch — used by both /v1/messages (Anthropic shape)
and /v1/chat/completions (OpenAI shape).

v3.2.x parked near-identical 50-line dispatch blocks in both
``messages.py`` and ``completions.py``. v3.2.9 extracted them into
``dispatch_grok_web_openai`` / ``dispatch_grok_web_anthropic``.

v3.2.10 hardens both helpers to call ``record_outcome()`` on every
terminal state (success or failure, stream or non-stream). Pre-fix,
grok-web traffic was completely invisible to ProviderMetric, the
activity log, the circuit breaker, and per-key budget tracking — every
other provider type goes through this path; grok-web was the only
exception. Surfaced when the operator noticed
``grok-web 24h: reqs=0 ok=0 fail=0`` despite verified end-to-end calls.
"""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Any, AsyncIterator, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

logger = logging.getLogger(__name__)


# v3.9.16 (P5) — Grok-Web rate-limit response carries "cool-off N
# seconds remaining" when the bridge has cached a recent 429. Extracting
# N + setting Provider.auto_skip_until = now + N tells the router to
# avoid this provider until the cool-off expires, which is more honest
# than letting it serve the cached 429 to every queued caller.
_GROKWEB_COOLOFF_PATTERN = re.compile(r"cool-off\s+(\d+)s\s+remaining", re.IGNORECASE)


async def _apply_grokweb_429_cooloff(
    db: AsyncSession, provider_id: str, err_msg: str,
) -> Optional[int]:
    """Parse a GrokWebError message for cool-off-N-seconds and set
    Provider.auto_skip_until accordingly. Returns the parsed seconds
    if applied, None otherwise. Silent on any DB or parse error —
    rate-limit handling must never escalate into a request failure."""
    try:
        match = _GROKWEB_COOLOFF_PATTERN.search(err_msg or "")
        if not match:
            return None
        secs = int(match.group(1))
        if secs <= 0 or secs > 3600:  # sanity bounds — 1h max cool-off
            return None
        from sqlalchemy import select
        from app.models.db import Provider
        p = (await db.execute(
            select(Provider).where(Provider.id == provider_id)
        )).scalar_one_or_none()
        if p is None:
            return None
        skip_until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        # Don't shorten an existing longer skip; do extend a shorter one.
        existing = p.auto_skip_until
        if existing is None or existing.replace(tzinfo=timezone.utc) < skip_until:
            p.auto_skip_until = skip_until.replace(tzinfo=None)
            p.auto_skip_reason = f"grok-web 429 cool-off {secs}s"
            await db.commit()
            logger.info(
                "grok_web.auto_skip_set provider=%s cooloff_sec=%s",
                provider_id, secs,
            )
        return secs
    except Exception as e:
        logger.warning(f"grok_web.cooloff_set_failed err={e!r}")
        return None


def _user_call_timeout() -> float:
    """v3.3.3: outer ceiling on user-traffic grok-web calls. Lets the
    router fall through to OpenRouter rather than block a user for 60s
    on a tail-latency outlier (15s observed 2026-05-09; p95 ~7s).
    Probes still use the function's own _PROBE_TIMEOUT_SEC (15s)."""
    try:
        return float(getattr(settings, "grok_web_user_timeout_sec", 30) or 30)
    except Exception:
        return 30.0


def _flatten_anthropic_system(system: Any) -> Optional[str]:
    """Anthropic's ``system`` can be a string OR a list of content
    blocks; flatten to a single string for the grok-web dispatcher."""
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict)
        )
    return str(system)


async def _record_grok_outcome(
    *,
    db: AsyncSession,
    provider_id: str,
    provider_name: Optional[str],
    model: str,
    requested_model: Optional[str],
    success: bool,
    in_tok: int = 0,
    out_tok: int = 0,
    t0: float,
    key_record_id: str,
    error_str: str = "",
    endpoint: str = "messages",
    llm_hint: Optional[str] = None,
) -> None:
    """Single-call wrapper around record_outcome with the args every
    grok-web call needs. Lazy-imports record_outcome to avoid a circular
    import (this module is imported by messages.py / completions.py
    which transitively import monitoring.helpers)."""
    from app.monitoring.helpers import record_outcome

    try:
        await record_outcome(
            db, provider_id, model,
            success=success, in_tok=in_tok, out_tok=out_tok, t0=t0,
            key_record_id=key_record_id, error_str=error_str,
            endpoint=endpoint, provider_name=provider_name,
            requested_model=requested_model,
            had_lmrh_hint=bool(llm_hint),
            lmrh_hint_raw=llm_hint or None,
        )
    except Exception as exc:
        # Observability must never break the response. Log + continue.
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "grok-web record_outcome failed: %s", exc,
        )


def _wrap_openai_stream_with_recording(
    inner: AsyncIterator[bytes],
    *,
    db: AsyncSession,
    provider_id: str,
    provider_name: Optional[str],
    model: str,
    requested_model: Optional[str],
    in_tok_estimate: int,
    t0: float,
    key_record_id: str,
    llm_hint: Optional[str],
) -> AsyncIterator[bytes]:
    """Wrap a stream generator so we can count output chars across the
    full SSE stream and call record_outcome on completion. Tokens
    estimated by 4-char heuristic — grok.com's web API doesn't return
    per-chunk usage, so this is the best proxy."""
    output_chars = 0
    saw_done = False
    err_str = ""

    async def _gen():
        nonlocal output_chars, saw_done, err_str
        try:
            async for chunk in inner:
                # Each chunk is ``data: {...}\n\n``; parse JSON to extract
                # ``choices[0].delta.content`` and accumulate.
                try:
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: "):].strip()
                        if payload == "[DONE]":
                            saw_done = True
                            continue
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        delta = (obj.get("choices") or [{}])[0].get("delta") or {}
                        content = delta.get("content") or ""
                        if isinstance(content, str):
                            output_chars += len(content)
                except Exception:
                    pass  # never break the proxy stream over a count error
                yield chunk
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"
            raise
        finally:
            await _record_grok_outcome(
                db=db, provider_id=provider_id, provider_name=provider_name,
                model=model, requested_model=requested_model,
                success=(err_str == "" and saw_done),
                in_tok=in_tok_estimate,
                out_tok=max(1, output_chars // 4) if output_chars else 0,
                t0=t0, key_record_id=key_record_id, error_str=err_str,
                endpoint="completions", llm_hint=llm_hint,
            )

    return _gen()


def _wrap_anthropic_stream_with_recording(
    inner: AsyncIterator[bytes],
    *,
    db: AsyncSession,
    provider_id: str,
    provider_name: Optional[str],
    model: str,
    requested_model: Optional[str],
    in_tok_estimate: int,
    t0: float,
    key_record_id: str,
    llm_hint: Optional[str],
) -> AsyncIterator[bytes]:
    """Same as OpenAI wrapper, but for Anthropic SSE (``event: ...
    data: {...}``). Counts characters from ``content_block_delta`` events."""
    output_chars = 0
    saw_stop = False
    err_str = ""

    async def _gen():
        nonlocal output_chars, saw_stop, err_str
        try:
            async for chunk in inner:
                try:
                    text = chunk.decode("utf-8", errors="replace")
                    for line in text.splitlines():
                        if not line.startswith("data: "):
                            continue
                        payload = line[len("data: "):].strip()
                        try:
                            obj = json.loads(payload)
                        except json.JSONDecodeError:
                            continue
                        if obj.get("type") == "content_block_delta":
                            delta = obj.get("delta") or {}
                            t = delta.get("text") or ""
                            if isinstance(t, str):
                                output_chars += len(t)
                        elif obj.get("type") == "message_stop":
                            saw_stop = True
                except Exception:
                    pass
                yield chunk
        except Exception as e:
            err_str = f"{type(e).__name__}: {str(e) or 'no message'}"
            raise
        finally:
            await _record_grok_outcome(
                db=db, provider_id=provider_id, provider_name=provider_name,
                model=model, requested_model=requested_model,
                success=(err_str == "" and saw_stop),
                in_tok=in_tok_estimate,
                out_tok=max(1, output_chars // 4) if output_chars else 0,
                t0=t0, key_record_id=key_record_id, error_str=err_str,
                endpoint="messages", llm_hint=llm_hint,
            )

    return _gen()


async def dispatch_grok_web_openai(
    *,
    route: Any,
    body: dict,
    stream: bool,
    resp_headers: dict[str, str],
    db: Optional[AsyncSession] = None,
    key_record_id: Optional[str] = None,
    t0: Optional[float] = None,
    llm_hint: Optional[str] = None,
) -> Any:
    """Run a grok-web request and return an OpenAI-shape FastAPI response.

    Used by ``/v1/chat/completions``. ``db`` / ``key_record_id`` / ``t0``
    are required for record_outcome to fire; pass ``None`` only in tests
    that explicitly assert non-recording behavior.
    """
    from app.providers.grok_web import (
        complete_grok_web,
        stream_grok_web,
        GrokWebError,
        GrokWebAuthError,
    )

    msgs = list(body.get("messages") or [])
    requested_model = body.get("model") or route.profile.model_id
    served_model = requested_model
    can_record = (db is not None and key_record_id is not None and t0 is not None)
    in_tok_estimate = max(1, sum(len(str(m.get("content", ""))) for m in msgs) // 4)

    if stream:
        try:
            stream_gen = stream_grok_web(
                route.provider.extra_config or {},
                messages=msgs,
                model=requested_model,
                timeout=_user_call_timeout(),
            )
            first_chunk = await stream_gen.__anext__()
        except GrokWebAuthError as e:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str=f"GrokWebAuthError: {e}", endpoint="completions",
                    llm_hint=llm_hint,
                )
            raise HTTPException(401, str(e))
        except GrokWebError as e:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str=f"GrokWebError: {e}", endpoint="completions",
                    llm_hint=llm_hint,
                )
            # v3.9.16 (P5) — auto-skip on 429 cool-off
            if e.status_code == 429:
                await _apply_grokweb_429_cooloff(db, route.provider.id, str(e))
            raise HTTPException(e.status_code, str(e))
        except StopAsyncIteration:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str="empty stream", endpoint="completions",
                    llm_hint=llm_hint,
                )
            raise HTTPException(502, "grok-web upstream: empty stream")

        async def _replay():
            yield first_chunk
            async for c in stream_gen:
                yield c

        replay = (
            _wrap_openai_stream_with_recording(
                _replay(),
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                in_tok_estimate=in_tok_estimate, t0=t0,
                key_record_id=key_record_id, llm_hint=llm_hint,
            )
            if can_record else _replay()
        )

        resp_headers["X-Cache-Status"] = "bypass"
        return StreamingResponse(
            replay,
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # Non-streaming
    try:
        result = await complete_grok_web(
            route.provider.extra_config or {},
            messages=msgs,
            model=requested_model,
            timeout=_user_call_timeout(),
        )
    except GrokWebAuthError as e:
        if can_record:
            await _record_grok_outcome(
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                success=False, t0=t0, key_record_id=key_record_id,
                error_str=f"GrokWebAuthError: {e}", endpoint="completions",
                llm_hint=llm_hint,
            )
        raise HTTPException(401, str(e))
    except GrokWebError as e:
        if can_record:
            await _record_grok_outcome(
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                success=False, t0=t0, key_record_id=key_record_id,
                error_str=f"GrokWebError: {e}", endpoint="completions",
                llm_hint=llm_hint,
            )
        # v3.9.16 (P5) — auto-skip on 429 cool-off
        if e.status_code == 429:
            await _apply_grokweb_429_cooloff(db, route.provider.id, str(e))
        raise HTTPException(e.status_code, str(e))

    # Success — record with the actual token counts from upstream usage.
    if can_record:
        usage = result.get("usage") or {}
        served_actual = result.get("model") or served_model
        await _record_grok_outcome(
            db=db, provider_id=route.provider.id,
            provider_name=route.provider.name,
            model=served_actual, requested_model=requested_model,
            success=True,
            in_tok=int(usage.get("prompt_tokens") or in_tok_estimate),
            out_tok=int(usage.get("completion_tokens") or 0),
            t0=t0, key_record_id=key_record_id,
            endpoint="completions", llm_hint=llm_hint,
        )
    resp_headers["X-Cache-Status"] = "bypass"
    return JSONResponse(content=result, headers=resp_headers)


async def dispatch_grok_web_anthropic(
    *,
    route: Any,
    body: dict,
    stream: bool,
    resp_headers: dict[str, str],
    db: Optional[AsyncSession] = None,
    key_record_id: Optional[str] = None,
    t0: Optional[float] = None,
    llm_hint: Optional[str] = None,
) -> Any:
    """Run a grok-web request and return an Anthropic-shape FastAPI response.

    Used by ``/v1/messages``. Same observability contract as
    ``dispatch_grok_web_openai`` — pass db/key_record_id/t0 for recording.
    """
    from app.providers.grok_web import (
        complete_grok_web,
        stream_grok_web_anthropic,
        anthropic_response_from_openai,
        GrokWebError,
        GrokWebAuthError,
    )

    msgs_for_grok = list(body.get("messages") or [])
    sys_for_grok = _flatten_anthropic_system(body.get("system"))
    requested_model = body.get("model") or route.profile.model_id
    served_model = requested_model
    can_record = (db is not None and key_record_id is not None and t0 is not None)
    in_tok_estimate = max(1, sum(len(str(m.get("content", ""))) for m in msgs_for_grok) // 4)

    if stream:
        try:
            stream_gen = stream_grok_web_anthropic(
                route.provider.extra_config or {},
                messages=msgs_for_grok,
                system=sys_for_grok,
                model=requested_model,
                timeout=_user_call_timeout(),
            )
            first_chunk = await stream_gen.__anext__()
        except GrokWebAuthError as e:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str=f"GrokWebAuthError: {e}", endpoint="messages",
                    llm_hint=llm_hint,
                )
            raise HTTPException(401, str(e))
        except GrokWebError as e:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str=f"GrokWebError: {e}", endpoint="messages",
                    llm_hint=llm_hint,
                )
            # v3.9.16 (P5) — auto-skip on 429 cool-off
            if e.status_code == 429:
                await _apply_grokweb_429_cooloff(db, route.provider.id, str(e))
            raise HTTPException(e.status_code, str(e))
        except StopAsyncIteration:
            if can_record:
                await _record_grok_outcome(
                    db=db, provider_id=route.provider.id,
                    provider_name=route.provider.name,
                    model=served_model, requested_model=requested_model,
                    success=False, t0=t0, key_record_id=key_record_id,
                    error_str="empty stream", endpoint="messages",
                    llm_hint=llm_hint,
                )
            raise HTTPException(502, "grok-web upstream: empty stream")

        async def _replay():
            yield first_chunk
            async for c in stream_gen:
                yield c

        replay = (
            _wrap_anthropic_stream_with_recording(
                _replay(),
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                in_tok_estimate=in_tok_estimate, t0=t0,
                key_record_id=key_record_id, llm_hint=llm_hint,
            )
            if can_record else _replay()
        )

        resp_headers["X-Cache-Status"] = "bypass"
        return StreamingResponse(
            replay,
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # Non-streaming: complete_grok_web returns OpenAI shape; we
    # translate to Anthropic /v1/messages shape here.
    msgs_with_system = (
        [{"role": "system", "content": sys_for_grok}] + msgs_for_grok
        if sys_for_grok else msgs_for_grok
    )
    try:
        openai_result = await complete_grok_web(
            route.provider.extra_config or {},
            messages=msgs_with_system,
            model=requested_model,
            timeout=_user_call_timeout(),
        )
    except GrokWebAuthError as e:
        if can_record:
            await _record_grok_outcome(
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                success=False, t0=t0, key_record_id=key_record_id,
                error_str=f"GrokWebAuthError: {e}", endpoint="messages",
                llm_hint=llm_hint,
            )
        raise HTTPException(401, str(e))
    except GrokWebError as e:
        if can_record:
            await _record_grok_outcome(
                db=db, provider_id=route.provider.id,
                provider_name=route.provider.name,
                model=served_model, requested_model=requested_model,
                success=False, t0=t0, key_record_id=key_record_id,
                error_str=f"GrokWebError: {e}", endpoint="messages",
                llm_hint=llm_hint,
            )
        # v3.9.16 (P5) — auto-skip on 429 cool-off
        if e.status_code == 429:
            await _apply_grokweb_429_cooloff(db, route.provider.id, str(e))
        raise HTTPException(e.status_code, str(e))

    if can_record:
        usage = openai_result.get("usage") or {}
        served_actual = openai_result.get("model") or served_model
        await _record_grok_outcome(
            db=db, provider_id=route.provider.id,
            provider_name=route.provider.name,
            model=served_actual, requested_model=requested_model,
            success=True,
            in_tok=int(usage.get("prompt_tokens") or in_tok_estimate),
            out_tok=int(usage.get("completion_tokens") or 0),
            t0=t0, key_record_id=key_record_id,
            endpoint="messages", llm_hint=llm_hint,
        )
    anth_result = anthropic_response_from_openai(openai_result)
    resp_headers["X-Cache-Status"] = "bypass"
    return JSONResponse(content=anth_result, headers=resp_headers)
