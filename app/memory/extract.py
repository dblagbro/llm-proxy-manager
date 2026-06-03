"""v3.9.0 (#267) Phase 5 — Anthropic memory-tool write-back.

When an upstream Anthropic response contains ``tool_use`` blocks for the
``memory`` tool (beta tool name ``memory_20250818``), we extract the
write operations and persist them to our king-store.

Closes the read/write loop with Phase 4 (read-side injection): now the
store is populated from real traffic instead of admin endpoints only.

Anthropic memory tool commands handled in this phase:
- ``create``   → store.put(content=input.content, memory_tag=path)
- ``str_replace`` → read-modify-write: existing content, swap ``old_str``
  for ``new_str``, store.put() the result
- ``insert``    → read-modify-write: insert content at ``insert_line``
- ``delete``    → store.delete()
- ``rename``    → put(new_path) + delete(old_path)

``view`` is a read op and does NOT mutate the store.

Phase 5 ships non-streaming only. Streaming write-back lands in Phase 5.5
(needs assembled-block sniffing in app/api/_messages_streaming.py).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Anthropic ships the memory tool under a versioned name; we accept both
# the shorthand "memory" (used in some doc examples) and the beta
# version string the API actually emits.
_MEMORY_TOOL_NAMES = {"memory", "memory_20250818"}


async def maybe_extract_memory_writes(
    db,
    *,
    response_dict: dict,
    api_key_id: str,
    conversation_id: Optional[str],
    memory_tag_default: Optional[str] = None,
    source_provider_id: Optional[str] = None,
) -> int:
    """Scan an Anthropic /v1/messages response for memory-tool writes
    and persist them. Returns the number of store mutations applied.

    No-op (returns 0) when:
    - ``caller_memory_enabled`` is False
    - ``conversation_id`` is None (un-scoped traffic stays untouched)
    - response has no tool_use blocks for the memory tool
    - any store error (silent degrade — never breaks live traffic)
    """
    try:
        from app.config import settings
        if not getattr(settings, "caller_memory_enabled", False):
            return 0
        if not conversation_id:
            return 0

        # v3.9.5 (#267 Phase 8) — per-provider opt-out. Skip extract
        # entirely if the provider that served this response has
        # memory_disabled=True.
        #
        # v5.0.0 — same lookup also resolves source_company (owner_company
        # of the serving provider, falling back to provider_type → company
        # derivation). source_company persists onto every memory row so
        # later compliance filtering can drop it for keys that have banned
        # the originating company.
        source_company: Optional[str] = None
        if source_provider_id:
            from sqlalchemy import select
            from app.models.db import Provider
            from app.compliance import provider_type_to_company
            pq = select(Provider).where(Provider.id == source_provider_id)
            p = (await db.execute(pq)).scalar_one_or_none()
            if p is not None and getattr(p, "memory_disabled", False):
                return 0
            if p is not None:
                source_company = (
                    getattr(p, "owner_company", None)
                    or provider_type_to_company(getattr(p, "provider_type", None))
                )

        content = response_dict.get("content") or []
        if not isinstance(content, list):
            return 0

        writes = 0
        for blk in content:
            if not isinstance(blk, dict):
                continue
            if blk.get("type") != "tool_use":
                continue
            if blk.get("name") not in _MEMORY_TOOL_NAMES:
                continue
            inp = blk.get("input") or {}
            if not isinstance(inp, dict):
                continue
            cmd = inp.get("command")
            applied = await _apply_command(
                db,
                cmd=cmd,
                inp=inp,
                api_key_id=api_key_id,
                conversation_id=conversation_id,
                memory_tag_default=memory_tag_default,
                source_provider_id=source_provider_id,
                source_company=source_company,
            )
            if applied:
                writes += 1
        try:
            from app.observability.prometheus import observe_memory_operation
            observe_memory_operation("extract", "applied" if writes > 0 else "skipped")
        except Exception:
            pass
        return writes
    except Exception as e:
        try:
            from app.observability.prometheus import observe_memory_operation
            observe_memory_operation("extract", "degraded")
        except Exception:
            pass
        logger.warning(
            f"caller_memory.extract: silent degrade ({e!r}) — response forwarded unchanged"
        )
        return 0


async def _apply_command(
    db,
    *,
    cmd: Optional[str],
    inp: dict,
    api_key_id: str,
    conversation_id: str,
    memory_tag_default: Optional[str],
    source_provider_id: Optional[str],
    source_company: Optional[str] = None,
) -> bool:
    """Apply one memory-tool command to the store. Returns True if the
    store was mutated.

    ``source_company`` is the owner_company of the provider that produced
    this write — persisted to every CallerMemory + CallerMemoryMarker row
    so compliance filtering at read time can drop banned origins.
    """
    from app.memory.store import get, put, delete

    # Path → memory_tag mapping. Anthropic memory tool addresses entries
    # by filesystem-style path (``/memories/user_facts.md``); we strip
    # the leading "/memories/" prefix and use the rest as memory_tag.
    # Callers without a path get memory_tag_default (or "default").
    path = inp.get("path")
    tag = _path_to_tag(path) if path else (memory_tag_default or "default")

    if cmd == "create":
        content = inp.get("content")
        if not isinstance(content, str):
            return False
        await put(
            db,
            api_key_id=api_key_id,
            content=content,
            conversation_id=conversation_id,
            memory_tag=tag,
            source_provider_id=source_provider_id,
            source_company=source_company,
        )
        return True

    if cmd == "str_replace":
        old_str = inp.get("old_str")
        new_str = inp.get("new_str")
        if not isinstance(old_str, str) or not isinstance(new_str, str):
            return False
        existing = await get(db, api_key_id, conversation_id, tag)
        if existing is None or old_str not in existing.content:
            return False
        updated = existing.content.replace(old_str, new_str, 1)
        await put(
            db,
            api_key_id=api_key_id,
            content=updated,
            conversation_id=conversation_id,
            memory_tag=tag,
            source_provider_id=source_provider_id,
            source_company=source_company,
        )
        return True

    if cmd == "insert":
        line = inp.get("insert_line")
        text = inp.get("content") or inp.get("new_str")
        if not isinstance(line, int) or not isinstance(text, str):
            return False
        existing = await get(db, api_key_id, conversation_id, tag)
        if existing is None:
            return False
        lines = existing.content.splitlines()
        # insert_line is 1-based per Anthropic's spec; clamp to valid range
        idx = max(0, min(len(lines), line - 1))
        lines.insert(idx, text)
        await put(
            db,
            api_key_id=api_key_id,
            content="\n".join(lines),
            conversation_id=conversation_id,
            memory_tag=tag,
            source_provider_id=source_provider_id,
            source_company=source_company,
        )
        return True

    if cmd == "delete":
        return await delete(db, api_key_id, conversation_id, tag)

    if cmd == "rename":
        new_path = inp.get("new_path")
        if not isinstance(new_path, str):
            return False
        existing = await get(db, api_key_id, conversation_id, tag)
        if existing is None:
            return False
        new_tag = _path_to_tag(new_path)
        await put(
            db,
            api_key_id=api_key_id,
            content=existing.content,
            conversation_id=conversation_id,
            memory_tag=new_tag,
            source_provider_id=source_provider_id,
            source_company=source_company,
        )
        await delete(db, api_key_id, conversation_id, tag)
        return True

    # view + anything else: read-only / unknown — no store mutation
    return False


def _path_to_tag(path: str) -> str:
    """Anthropic memory tool addresses entries by path like
    ``/memories/user_facts.md``. We collapse the path to a memory_tag
    by stripping the canonical prefix and any leading/trailing slashes.
    Unknown shapes fall back to the path verbatim."""
    s = path.strip()
    for prefix in ("/memories/", "memories/"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    s = s.strip("/")
    return s or "default"
