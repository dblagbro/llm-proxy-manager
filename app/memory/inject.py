"""v3.8.9 (#267) Phase 4 — request-time memory injection middleware.

When the operator enables ``caller_memory_enabled`` AND a request arrives
with an ``X-Conversation-Id`` header, this middleware reads the matching
memory entry from the king-store and prepends it as a system-prompt
prefix on the outgoing request body.

Design decisions — RESOLVED 2026-05-14 (pre-Phase-10 flip)
----------------------------------------------------------
Three open questions from the original Phase 4 design were locked in
ahead of the Phase 10 operator opt-in:

**Q1: Injection scope** — fire only when caller supplies
``X-Conversation-Id`` (NOT for every request).
*Why*: without a conversation ID, there's no per-conv memory entry to
inject. One-shot requests stay clean. Avoids accidental cross-pollination
between unrelated callers. Operators who want global per-key memory can
still get it by passing a fixed conversation_id.

**Q2: Anthropic injection point** — system prompt prefix (NOT
``memory_blocks`` field, NOT first user message).
*Why*: ``memory_blocks`` is a feature of the Anthropic memory tool and
only fires when the caller includes that tool definition — too narrow
a contract for the proxy to commit to. First-user-message would conflict
with caller-authored content and break role boundaries. System prompt
is invisible to other roles and stable across providers — the same
behavior translates cleanly to OpenAI-shape via #269's Fix B translator.

**Q3: OpenAI injection point** — system prompt prefix on existing
message-0, or synthesized at index 0 if no system message is present.
*Why*: same as Q2 — keeps the cross-provider behavior identical, makes
the OpenAI Fix B path round-trip cleanly, and doesn't pollute the
caller's chat history with proxy-side annotations.

Cross-vendor strategy summary:
- Anthropic ``/v1/messages``: body["system"] is a string OR a list of
  blocks. We prepend a text block / string segment in either shape.
- OpenAI ``/v1/chat/completions``: body["messages"][0] is a system
  role message OR we synthesize one at index 0.

Phase 4 is read-only (no write-back). Phase 5 (v3.9.0) added Anthropic
memory-tool write-back; admin endpoints (Phase 9, v3.9.6) cover the
operator-driven write path.

Behavior rules (locked):
- ``caller_memory_enabled=False`` → no-op (forward request unchanged).
- No ``X-Conversation-Id`` header → no-op (Q1 locks scope to scoped flows).
- No memory entry → no-op.
- Selected provider has ``memory_disabled=True`` → no-op (Phase 8 gate
  enforced in messages.py / completions.py — this module is unaware).
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
        from app.observability.prometheus import observe_memory_operation
        if not getattr(settings, "caller_memory_enabled", False):
            return body, False
        if not conversation_id:
            return body, False
        tag = memory_tag or "default"

        # v5.0.0 compliance — resolve the per-key blocklist before reading
        # so a memory row tagged with a banned source_company never gets
        # injected into the prompt (decision 7: NULL is also banned when
        # the blocklist is non-empty).
        blocked_companies = None
        try:
            from app.compliance import get_effective_blocklist
            blocked_companies = await get_effective_blocklist(db, api_key_id)
        except Exception:
            # Resolution failure leaves blocked_companies=None — store.get()
            # treats that as "no filter", which is the legacy behavior.
            blocked_companies = None

        from app.memory.store import get
        entry = await get(
            db, api_key_id, conversation_id, tag,
            blocked_companies=blocked_companies or None,
        )
        # Audit the filter-out case: a row exists but was dropped by the
        # blocklist. Re-probe without the filter to distinguish "no row"
        # from "row banned" so we only emit the event when policy actually
        # blocked something.
        if entry is None and blocked_companies:
            try:
                raw = await get(db, api_key_id, conversation_id, tag)
                if raw is not None and raw.content:
                    from app.compliance import emit_event, generate_audit_id
                    await emit_event(
                        db,
                        audit_id=generate_audit_id(),
                        api_key_id=api_key_id,
                        event_type="memory_filtered",
                        reason_code="source-company-banned",
                        http_status=0,  # internal filter
                        blocked_company=raw.source_company or next(iter(blocked_companies), None),
                        commit=False,
                    )
            except Exception:
                # Audit failures must not block the request — silent degrade.
                pass
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
                entry = await get(
                    db, api_key_id, conversation_id, tag,
                    blocked_companies=blocked_companies or None,
                )
            if entry is None or not entry.content:
                observe_memory_operation("inject", "skipped")
                return body, False

        prefix = MEMORY_HEADER_PREFIX + entry.content.strip() + "\n"
        if endpoint == "messages":
            observe_memory_operation("inject", "applied")
            return _inject_anthropic(body, prefix), True
        if endpoint == "completions":
            observe_memory_operation("inject", "applied")
            return _inject_openai(body, prefix), True
        observe_memory_operation("inject", "skipped")
        return body, False
    except Exception as e:
        try:
            from app.observability.prometheus import observe_memory_operation as _obs
            _obs("inject", "degraded")
        except Exception:
            pass
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
