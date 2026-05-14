"""v3.9.4 (#267) Phase 7 — back-pressure memory recovery.

When the marker says we used to have memory for this (api_key,
conversation, tag) but the content row is gone, this module tries
to reconstruct the content by reading it back from the original
upstream provider. Worst case is a DB restore that lost the
``caller_memory`` rows while the marker table survived (markers are
small + frozen — they back up cleanly even when content rows are
mid-mutation).

Scope today
-----------
Same caveat as Phase 6: of the deployed provider types, none expose
a clean conversation-state read-back API that the proxy can call:

- **anthropic**: memory tool ``view`` would work, but only when the
  caller's request includes the memory tool definition — the proxy
  can't initiate the view call on its own.
- **claude-oauth**: Console-side memory not API-accessible.
- **openai** chat-completions: stateless, nothing to read back.
- **ChatGPT-oauth-plan**: ``GET /backend-api/conversation/{id}``
  exists but needs CSRF token shape we don't capture.
- **OpenAI Assistants**: ``GET /v1/threads/{id}/messages`` works but
  we don't use Assistants yet.
- **cohere**, **google** (Vertex / Gemini), **openrouter**,
  **grok-*** : stateless from our request path.

Every handler ships as a noop. The dispatcher itself is the value:
when real handlers land, the wiring + retry semantics are in place
already.

Retry semantics
---------------
A recovery attempt is cheap when `_HANDLERS` is empty for the
provider_type (early-return, no upstream call). When a real handler
lands and fails, the dispatcher does NOT set ``recovered_at`` — so
the next request retries. To bound retry rate, future versions can
add an in-memory backoff (mirroring the circuit breaker pattern).
On success, ``recovered_at`` is set and subsequent requests skip
recovery for this (api_key, conversation, tag).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

# Provider-type → async handler. Each handler receives ``ctx``
# (everything the recovery might need) and returns the reconstructed
# content as a string, or None when reconstruction isn't possible.
RecoverHandler = Callable[[dict], Awaitable[Optional[str]]]
_HANDLERS: dict[str, RecoverHandler] = {}


def register_handler(provider_type: str, handler: RecoverHandler) -> None:
    """Register a per-vendor recovery handler. Idempotent — re-registering
    replaces the prior handler."""
    _HANDLERS[provider_type] = handler


async def maybe_recover_memory(
    db,
    *,
    api_key_id: str,
    conversation_id: Optional[str],
    memory_tag: Optional[str],
) -> Optional[str]:
    """Try to reconstruct missing memory content from the original
    upstream provider. Returns the recovered content on success, None
    otherwise.

    On success: writes the content via ``store.put()`` (cluster-syncs
    to peers) and sets ``marker.recovered_at = now``. The caller can
    then re-read via ``store.get()`` to fetch the freshly populated row.

    Never raises — silent degrade. Recovery failure is recoverable
    (operator can re-import from snapshot); blowing up the request
    is not.
    """
    try:
        from app.config import settings
        if not getattr(settings, "caller_memory_enabled", False):
            return None
        if not getattr(settings, "caller_memory_recovery_enabled", True):
            return None
        if not conversation_id:
            return None

        tag = memory_tag or "default"

        from sqlalchemy import select
        from app.models.db import CallerMemoryMarker, Provider

        mq = (
            select(CallerMemoryMarker)
            .where(CallerMemoryMarker.api_key_id == api_key_id)
            .where(CallerMemoryMarker.memory_tag == tag)
            .where(CallerMemoryMarker.conversation_id == conversation_id)
        )
        marker = (await db.execute(mq)).scalar_one_or_none()
        if marker is None:
            return None
        if marker.recovered_at is not None:
            # Already recovered (or skipped). Don't loop.
            return None
        old_provider_id = marker.last_known_provider_id
        if not old_provider_id:
            return None

        pq = select(Provider).where(Provider.id == old_provider_id)
        old_provider = (await db.execute(pq)).scalar_one_or_none()
        provider_type = old_provider.provider_type if old_provider else "unknown"

        handler = _HANDLERS.get(provider_type)
        if handler is None:
            # No handler registered for this provider type — nothing to
            # try. Don't mark recovered_at; if a handler lands later,
            # the next request will pick it up. Logged at debug, not
            # warning, because this is the steady-state shape today.
            logger.debug(
                "caller_memory.recover: no handler for "
                f"provider_type={provider_type!r} "
                f"(api_key={api_key_id} conv={conversation_id!r})"
            )
            return None

        ctx = {
            "db": db,
            "api_key_id": api_key_id,
            "conversation_id": conversation_id,
            "memory_tag": tag,
            "old_provider_id": old_provider_id,
            "provider_type": provider_type,
            "old_provider": old_provider,
            "last_known_external_ref": marker.last_known_external_ref,
        }
        content = await handler(ctx)
        if not content:
            try:
                from app.observability.prometheus import observe_memory_operation
                observe_memory_operation("recover", "skipped")
            except Exception:
                pass
            logger.info(
                "caller_memory.recover: handler returned no content "
                f"provider_type={provider_type!r} "
                f"api_key={api_key_id} conv={conversation_id!r}"
            )
            return None

        # Persist + mark recovered.
        from app.memory.store import put
        await put(
            db,
            api_key_id=api_key_id,
            content=content,
            conversation_id=conversation_id,
            memory_tag=tag,
            source_provider_id=old_provider_id,
        )
        marker.recovered_at = time.time()
        await db.commit()
        try:
            from app.observability.prometheus import observe_memory_operation
            observe_memory_operation("recover", "applied")
        except Exception:
            pass
        logger.info(
            "caller_memory.recover: recovered "
            f"{len(content)} chars provider_type={provider_type!r} "
            f"api_key={api_key_id} conv={conversation_id!r}"
        )
        return content
    except Exception as e:
        try:
            from app.observability.prometheus import observe_memory_operation
            observe_memory_operation("recover", "degraded")
        except Exception:
            pass
        logger.warning(
            f"caller_memory.recover: silent degrade ({e!r}) — continuing"
        )
        return None
