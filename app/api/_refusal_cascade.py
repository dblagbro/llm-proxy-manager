"""v5.20.1 — proxy-side on-refusal cascade.

Called from messages.py after the initial dispatch but BEFORE the
response-tail. If ``refusal_retry_enabled`` is on for the key AND
detection fires on the initial response, walks a chain of alternate
providers (excluding the ones already tried) and returns whichever
first produces a clean response — or reports chain exhaustion if none
succeed.

Design decisions locked from DevinGPT team's 2026-07-05 memo:
- NO silent substitution: every attempt writes an activity_log row +
  X-Refusal-Retry-* headers. Operator can audit the full chain.
- Reuses ``refusal_detection.detect_refusal`` (same regex signature
  the v5.20.0 detection tail uses).
- Optional per-key ``refusal_retry_priority_chain`` (JSON list of
  provider IDs) — cascade tries these first, then falls back to
  the proxy's ranked-provider list. If NULL, only ranked-provider
  fallback.
- Non-streaming only in v5.20.1. Streaming cascade is v5.20.2+
  (needs buffering or SSE frame emission).
- Cost cap: ``refusal_retry_max_attempts`` (default 3). Hard stop
  regardless of chain length so a caller can't accidentally burn
  20 providers.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_MAX_ATTEMPTS = 3


@dataclass
class CascadeResult:
    """Outcome of the cascade attempt."""
    swapped: bool               # True if a cascade attempt succeeded
    final_route: Any            # the route that produced the returned response
    final_result: Any           # raw litellm response
    final_anthropic: dict       # anthropic-shape response
    attempts_made: int          # 0 = no refusal detected on initial (cascade not run)
    chain_exhausted: bool       # True if all attempts refused
    attempted_provider_ids: list[str]  # audit trail


async def maybe_cascade_on_refusal(
    *,
    db,
    key_record: Any,
    initial_route: Any,
    initial_result: Any,
    initial_anthropic: dict,
    hint: Any,
    has_images: bool,
    messages_list: list,
    max_tokens: int,
    system: Any,
    extra: dict,
    dispatch: Callable[..., Awaitable[Any]],
    to_anthropic_response: Callable[[Any], dict],
    resp_headers: dict,
    body: dict,
) -> CascadeResult:
    """Detect refusal on the initial response and cascade if warranted.

    Returns a CascadeResult with the final route + response the caller
    should return. If no cascade ran, ``swapped=False`` and the caller
    keeps its own initial_route/result/anthropic. Response headers are
    mutated in place with X-Refusal-Retry-* attribution.
    """
    from app.refusal_detection import detect_refusal, extract_text_from_anthropic_response
    from app.routing.router import select_provider

    _no_op = CascadeResult(
        swapped=False,
        final_route=initial_route,
        final_result=initial_result,
        final_anthropic=initial_anthropic,
        attempts_made=0,
        chain_exhausted=False,
        attempted_provider_ids=[getattr(initial_route.provider, "id", "?")],
    )

    if not getattr(key_record, "refusal_retry_enabled", False):
        return _no_op

    # v5.20.1 — detection reuses the v5.20.0 regex module for
    # per-attempt consistency. No secondary LLM call.
    _initial_text = extract_text_from_anthropic_response(initial_anthropic)
    _match = detect_refusal(_initial_text)
    if _match is None:
        return _no_op

    # Detection fired. Enter the cascade loop.
    max_attempts = int(
        getattr(key_record, "refusal_retry_max_attempts", None)
        or DEFAULT_MAX_ATTEMPTS
    )
    attempted_ids: set[str] = {getattr(initial_route.provider, "id", "?")}
    audit_trail: list[str] = list(attempted_ids)

    _initial_pattern = _match.pattern_name
    # v5.20.1 note: per-key priority chain (refusal_retry_priority_chain)
    # is documented in the DevinGPT memo as a future column. It needs a
    # ``prefer_provider_id`` kwarg on select_provider which doesn't
    # exist yet; that's a v5.20.2 candidate. Today's cascade uses the
    # LMRH ranking with the current provider(s) excluded — same order
    # a fresh /v1/messages call would land on.
    priority_chain: list[str] = []

    # v5.20.1 — audit the initial refusal separately so the operator
    # sees "primary refused; cascade tried X, Y" as ordered rows.
    _emit_activity(
        db, "refusal_retry_start",
        api_key_id=getattr(key_record, "id", None),
        provider_id=getattr(initial_route.provider, "id", None),
        message=(
            f"Refusal cascade started: initial_pattern={_initial_pattern} "
            f"max_attempts={max_attempts}"
        ),
        event_meta={
            "initial_pattern": _initial_pattern,
            "initial_category": _match.category,
            "initial_snippet": _match.matched_snippet,
            "initial_provider_id": getattr(initial_route.provider, "id", None),
            "priority_chain": priority_chain,
            "max_attempts": max_attempts,
        },
    )

    current_route = initial_route
    current_result = initial_result
    current_anthropic = initial_anthropic
    swapped = False

    for attempt_idx in range(1, max_attempts + 1):
        # Pick next candidate via LMRH-ranked selection, excluding
        # every provider we've already tried this cascade.
        alt_route = None
        try:
            alt_route = await select_provider(
                db, hint,
                has_tools=False,
                has_images=has_images,
                key_type=key_record.key_type,
                api_key_id=key_record.id,
                exclude_provider_ids=attempted_ids,
            )
        except Exception as exc:
            logger.warning(
                "refusal_cascade.rank_pick_failed attempt=%d err=%s",
                attempt_idx, exc,
            )
            alt_route = None
        if alt_route is None:
            # No more providers to try.
            break

        _pid = getattr(alt_route.provider, "id", "?")
        attempted_ids.add(_pid)
        audit_trail.append(_pid)

        # Dispatch.
        _attempt_t0 = time.time()
        try:
            alt_result = await dispatch(alt_route)
        except Exception as exc:
            logger.warning(
                "refusal_cascade.dispatch_failed attempt=%d provider=%s err=%s",
                attempt_idx, _pid, exc,
            )
            _emit_activity(
                db, "refusal_retry_attempt_failed",
                api_key_id=getattr(key_record, "id", None),
                provider_id=_pid,
                message=f"Cascade attempt {attempt_idx} dispatch failed: {exc}",
                event_meta={
                    "attempt_idx": attempt_idx,
                    "err": str(exc)[:200],
                    "latency_ms": int((time.time() - _attempt_t0) * 1000),
                },
            )
            continue

        # Detect refusal on the retry response.
        try:
            alt_anthropic = to_anthropic_response(alt_result)
        except Exception as exc:
            logger.warning(
                "refusal_cascade.anthropic_convert_failed attempt=%d err=%s",
                attempt_idx, exc,
            )
            continue
        _alt_text = extract_text_from_anthropic_response(alt_anthropic)
        _alt_match = detect_refusal(_alt_text)
        _attempt_latency_ms = int((time.time() - _attempt_t0) * 1000)

        if _alt_match is None:
            # Accepted! Swap and exit.
            current_route = alt_route
            current_result = alt_result
            current_anthropic = alt_anthropic
            swapped = True
            _emit_activity(
                db, "refusal_retry_success",
                api_key_id=getattr(key_record, "id", None),
                provider_id=_pid,
                message=(
                    f"Cascade succeeded on attempt {attempt_idx} via {_pid} "
                    f"(initial_pattern={_initial_pattern})"
                ),
                event_meta={
                    "attempt_idx": attempt_idx,
                    "provider_id": _pid,
                    "latency_ms": _attempt_latency_ms,
                    "initial_pattern": _initial_pattern,
                    "audit_trail": audit_trail,
                },
            )
            break
        else:
            _emit_activity(
                db, "refusal_retry_attempt_refused",
                api_key_id=getattr(key_record, "id", None),
                provider_id=_pid,
                message=(
                    f"Cascade attempt {attempt_idx} also refused via {_pid} "
                    f"(pattern={_alt_match.pattern_name})"
                ),
                event_meta={
                    "attempt_idx": attempt_idx,
                    "provider_id": _pid,
                    "pattern": _alt_match.pattern_name,
                    "category": _alt_match.category,
                    "snippet": _alt_match.matched_snippet,
                    "latency_ms": _attempt_latency_ms,
                },
            )
            continue

    if not swapped:
        _emit_activity(
            db, "refusal_retry_exhausted",
            api_key_id=getattr(key_record, "id", None),
            provider_id=getattr(current_route.provider, "id", None),
            message=(
                f"Refusal cascade exhausted after {len(audit_trail) - 1} "
                f"retry attempt(s); returning initial refusal"
            ),
            event_meta={
                "initial_pattern": _initial_pattern,
                "audit_trail": audit_trail,
                "attempts_made": len(audit_trail) - 1,
            },
        )

    # Emit response headers regardless — the caller wants attribution
    # even when cascade didn't swap.
    resp_headers["X-Refusal-Retry-Attempted"] = str(len(audit_trail) - 1)
    resp_headers["X-Refusal-Retry-Provider"] = getattr(
        current_route.provider, "id", "?"
    )
    resp_headers["X-Refusal-Chain-Exhausted"] = "true" if not swapped else "false"

    try:
        await db.commit()
    except Exception:
        pass

    return CascadeResult(
        swapped=swapped,
        final_route=current_route,
        final_result=current_result,
        final_anthropic=current_anthropic,
        attempts_made=len(audit_trail) - 1,
        chain_exhausted=not swapped,
        attempted_provider_ids=audit_trail,
    )


def _emit_activity(
    db, event_type: str, *,
    api_key_id: Optional[str],
    provider_id: Optional[str],
    message: str,
    event_meta: dict,
    severity: str = "info",
) -> None:
    """Fire-and-forget activity_log write. Failures are swallowed so a
    logging problem doesn't break the cascade path."""
    try:
        from app.models.db import ActivityLog
        db.add(ActivityLog(
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            severity=severity,
            event_type=event_type,
            api_key_id=api_key_id,
            provider_id=provider_id,
            message=message,
            event_meta=event_meta,
        ))
    except Exception:
        pass
