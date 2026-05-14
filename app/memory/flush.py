"""v3.9.3 (#267) Phase 6 — provider-side memory flush handlers.

When a conversation routes to a different provider than the one that
wrote the last memory entry, this module emits a best-effort flush
request to the OLD provider so the proxy's king-store remains the
authoritative source. Per RFC decision #3: active flush is best-effort,
default ON, log+continue on failure (worst case: duplicate memory on
provider side, never data loss).

Scope today
-----------
Of the currently-deployed provider types, none expose a clean
"clear conversation memory" API that the proxy can call without
side effects:

- **anthropic**: the memory tool's clear semantics require the model
  to emit a tool_use ``delete`` — there's no out-of-band reset.
- **claude-oauth**: Console-side memory is not exposed via API.
- **openai**: chat-completions is stateless; no upstream memory to flush.
- **ChatGPT-oauth-plan**: conversation history exists but the OAuth
  delete-conversation endpoint isn't wired into our session capture
  (codex_session_cookies don't include the CSRF token shape needed).
- **cohere**, **google** (Vertex / Gemini), **openrouter**,
  **grok-web**, **grok-bridge**: all stateless from our request path.

So today every handler is a NO-OP that records the provider transition
to the marker (so back-pressure recovery in Phase 7 has the trail) and
returns silently. The scaffolding is in place to land real handlers
incrementally without re-wiring the call site.

What this module DOES do today
------------------------------
- Detect provider transitions (compare current ``route.provider.id``
  with ``CallerMemoryMarker.last_known_provider_id``).
- Update the marker on every transition so the trail is current.
- Log transitions at INFO so operators can see the routing handoff.
- Provide a clean registration point for future per-vendor handlers.

Future handlers (queued for Phase 6.1+)
---------------------------------------
- ChatGPT-oauth-plan: DELETE /backend-api/conversation/{id} once we
  capture the bearer token + CSRF cookie shape.
- OpenAI Assistants API: DELETE /v1/threads/{thread_id} (when we
  start using Assistants — currently unused).
- Custom provider integrations: arbitrary cleanup callbacks.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Provider-type → async handler. Handlers receive ``ctx`` (a dict of
# everything they could need) and return True if the flush was
# successfully emitted, False if it was skipped/failed (best-effort).
FlushHandler = Callable[[dict], Awaitable[bool]]
_HANDLERS: dict[str, FlushHandler] = {}


def register_handler(provider_type: str, handler: FlushHandler) -> None:
    """Register a per-vendor flush handler. Idempotent — re-registering
    the same provider_type replaces the previous handler."""
    _HANDLERS[provider_type] = handler


async def _flush_noop(ctx: dict) -> bool:
    """Default handler: log the transition + succeed.

    Used for provider types where there's nothing to flush server-side
    (stateless providers) OR where the upstream cleanup API isn't
    accessible from the proxy's current auth shape.
    """
    logger.info(
        "caller_memory.flush: noop "
        f"provider_type={ctx.get('provider_type')} "
        f"old_provider_id={ctx.get('old_provider_id')} "
        f"new_provider_id={ctx.get('new_provider_id')} "
        f"api_key_id={ctx.get('api_key_id')} "
        f"conversation_id={ctx.get('conversation_id')!r}"
    )
    return True


async def maybe_flush_provider_memory(
    db,
    *,
    api_key_id: str,
    conversation_id: Optional[str],
    memory_tag: Optional[str],
    new_provider_id: str,
) -> bool:
    """Detect provider transition and flush the old provider's memory.

    Returns True if a transition was detected (and handler invoked),
    False if no transition (same provider as last write, no marker yet,
    feature disabled, etc).

    Never raises — silent degrade per RFC decision #3. The worst case
    is duplicate memory on provider side, which Phase 7 reconciliation
    can resolve.
    """
    try:
        from app.config import settings
        if not getattr(settings, "caller_memory_enabled", False):
            return False
        if not getattr(settings, "caller_memory_active_flush_enabled", True):
            return False
        if not conversation_id:
            return False

        tag = memory_tag or "default"

        from sqlalchemy import select
        from app.models.db import CallerMemoryMarker, Provider

        # Look up the marker — it carries the last_known_provider_id.
        mq = (
            select(CallerMemoryMarker)
            .where(CallerMemoryMarker.api_key_id == api_key_id)
            .where(CallerMemoryMarker.memory_tag == tag)
            .where(CallerMemoryMarker.conversation_id == conversation_id)
        )
        marker = (await db.execute(mq)).scalar_one_or_none()
        if marker is None:
            return False
        old_provider_id = marker.last_known_provider_id
        if not old_provider_id or old_provider_id == new_provider_id:
            return False

        # Provider transition detected. Look up the old provider's type
        # to pick the right handler.
        pq = select(Provider).where(Provider.id == old_provider_id)
        old_provider = (await db.execute(pq)).scalar_one_or_none()
        provider_type = old_provider.provider_type if old_provider else "unknown"
        last_known_external_ref = marker.last_known_external_ref

        ctx = {
            "db": db,
            "api_key_id": api_key_id,
            "conversation_id": conversation_id,
            "memory_tag": tag,
            "old_provider_id": old_provider_id,
            "new_provider_id": new_provider_id,
            "provider_type": provider_type,
            "old_provider": old_provider,
            "last_known_external_ref": last_known_external_ref,
        }
        handler = _HANDLERS.get(provider_type, _flush_noop)
        ok = await handler(ctx)
        if not ok:
            logger.warning(
                f"caller_memory.flush: handler for {provider_type!r} "
                f"reported failure (best-effort; continuing)"
            )

        # Always update the marker — even on handler failure. The proxy's
        # king-store is authoritative; the upstream-side state is what
        # we're trying to converge, not the marker. Phase 7 reconciles.
        marker.last_known_provider_id = new_provider_id
        marker.last_known_external_ref = None  # the old ref is stale now
        await db.commit()

        return True
    except Exception as e:
        logger.warning(
            f"caller_memory.flush: silent degrade ({e!r}) — continuing"
        )
        return False
