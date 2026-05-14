"""v3.8.9 (#267) Phase 4 — request-time memory injection middleware.

When the operator enables ``caller_memory_enabled`` AND a request arrives
with an ``X-Conversation-Id`` header, this middleware reads the matching
memory entry from the king-store and prepends it as a system-prompt
prefix on the outgoing request body.

Cross-vendor strategy: we always inject as a system-prompt prefix.
- Anthropic ``/v1/messages``: body["system"] is a string OR a list of
  blocks. We prepend a text block / string segment in either shape.
- OpenAI ``/v1/chat/completions``: body["messages"][0] is a system
  role message OR we synthesize one at index 0.

Phase 4 is read-only (no write-back). Memory writes still go through
the admin endpoints and are populated manually until Phase 5 wires up
Anthropic memory-tool extraction.

Behavior rules (sensible defaults — operator can revisit later):
- ``caller_memory_enabled=False`` → no-op (forward request unchanged).
- No ``X-Conversation-Id`` header → no-op (we only inject in scoped flows).
- No memory entry → no-op.
- Store errors → silent degrade (log, forward unchanged) so a Redis
  outage never breaks live traffic.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

MEMORY_HEADER_PREFIX = (
    "[Persistent caller memory — applies across providers]\n"
)


async def maybe_inject_memory(
    db,
    *,
    body: dict,
    api_key_id: str,
    conversation_id: Optional[str],
    memory_tag: Optional[str],
    endpoint: str,
) -> tuple[dict, bool]:
    """Return (possibly-modified body, injected_bool).

    ``endpoint`` is ``"messages"`` (Anthropic shape) or ``"completions"``
    (OpenAI shape). Anything else: no-op.
    """
    try:
        from app.config import settings
        if not getattr(settings, "caller_memory_enabled", False):
            return body, False
        if not conversation_id:
            return body, False
        tag = memory_tag or "default"

        from app.memory.store import get
        entry = await get(db, api_key_id, conversation_id, tag)
        if entry is None or not entry.content:
            # v3.9.4 (#267) Phase 7 — back-pressure recovery. If the
            # marker exists but content is missing (DB restore that
            # lost content rows), try to reconstruct from the original
            # upstream provider. Silent degrade on any failure; the
            # noop case is cheap (no handler registered → early return).
            from app.memory.recover import maybe_recover_memory
            recovered = await maybe_recover_memory(
                db, api_key_id=api_key_id,
                conversation_id=conversation_id, memory_tag=tag,
            )
            if recovered:
                entry = await get(db, api_key_id, conversation_id, tag)
            if entry is None or not entry.content:
                return body, False

        prefix = MEMORY_HEADER_PREFIX + entry.content.strip() + "\n"
        if endpoint == "messages":
            return _inject_anthropic(body, prefix), True
        if endpoint == "completions":
            return _inject_openai(body, prefix), True
        return body, False
    except Exception as e:
        logger.warning(
            f"caller_memory.inject: silent degrade ({e!r}) — forwarding unchanged"
        )
        return body, False


def _inject_anthropic(body: dict, prefix: str) -> dict:
    """Prepend ``prefix`` to the existing system prompt.

    Anthropic accepts ``system`` as either a string or a list of content
    blocks. We preserve whichever shape the caller used.
    """
    new_body = dict(body)
    existing = new_body.get("system")
    if existing is None:
        new_body["system"] = prefix.rstrip()
        return new_body
    if isinstance(existing, str):
        new_body["system"] = prefix + existing
        return new_body
    if isinstance(existing, list):
        # Prepend a text block, then the caller's blocks.
        new_body["system"] = [{"type": "text", "text": prefix.rstrip()}] + existing
        return new_body
    # Unknown shape — leave as-is (silent degrade).
    return body


def _inject_openai(body: dict, prefix: str) -> dict:
    """Prepend ``prefix`` to the system message (or synthesize one)."""
    new_body = dict(body)
    msgs = list(new_body.get("messages") or [])
    if msgs and msgs[0].get("role") == "system":
        first = dict(msgs[0])
        content = first.get("content", "")
        if isinstance(content, str):
            first["content"] = prefix + content
        elif isinstance(content, list):
            # OpenAI also supports a list-of-parts shape for system content.
            first["content"] = [{"type": "text", "text": prefix.rstrip()}] + content
        else:
            # Unknown — leave alone.
            return body
        msgs[0] = first
    else:
        msgs.insert(0, {"role": "system", "content": prefix.rstrip()})
    new_body["messages"] = msgs
    return new_body
