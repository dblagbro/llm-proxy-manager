"""v3.9.16 (P6) — OpenAI Assistants flush + recovery handlers.

Future-ready scaffolding. Today no provider of type ``openai-assistants``
exists in the deployment, so these handlers never fire. When/if the
operator adds one (Assistants API has different shape than Chat
Completions — thread-keyed, persistent message history server-side),
these handlers light up automatically.

The Assistants API documents both endpoints:
  DELETE /v1/threads/{thread_id}                — flush a thread
  GET    /v1/threads/{thread_id}/messages       — read message history

Both require a standard ``Authorization: Bearer <api_key>`` header.
``last_known_external_ref`` on the marker holds the upstream thread_id;
when the proxy starts using Assistants, the dispatch path should write
it on every create_thread / first-message call.

Why ship these now if no provider uses them yet:
1. The registry pattern in app/memory/flush.py + app/memory/recover.py
   already supports per-provider-type handlers. Adding one is
   ~150 lines. Doing it now means zero code change on the day we
   actually adopt Assistants.
2. Tests exercise the handler logic against a recorded API response
   shape so the integration is locked in.
3. Documentation is fresher than if we wait until adoption is
   imminent.

Registration: at startup, app/memory/__init__.py imports this module
and calls ``flush.register_handler`` + ``recover.register_handler``.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


_OPENAI_ASSISTANTS_BASE_URL = "https://api.openai.com/v1"
# Beta header — Assistants API requires this on every request as of
# 2025-04. The string is versioned by OpenAI; track upstream if they
# bump.
_ASSISTANTS_BETA_HEADER = "assistants=v2"


async def _api_headers(provider) -> dict:
    """Construct Bearer-auth + beta headers from a Provider row.

    The Provider's ``api_key`` column holds the OpenAI API key. Custom
    base_url is honored when set (some operators front Assistants
    behind a corporate gateway)."""
    return {
        "Authorization": f"Bearer {provider.api_key}",
        "OpenAI-Beta": _ASSISTANTS_BETA_HEADER,
        "Content-Type": "application/json",
    }


def _base_url(provider) -> str:
    return (provider.base_url or _OPENAI_ASSISTANTS_BASE_URL).rstrip("/")


# ── Phase 6 — flush handler ────────────────────────────────────────


async def flush_openai_assistants(ctx: dict) -> bool:
    """Delete the upstream Assistants thread when routing away from
    this provider.

    Returns True on success or when there's nothing to flush (no
    thread_id known). Returns False on network/auth error.

    Best-effort by RFC decision #3: the proxy's king-store is
    authoritative, so a failed flush only leaves a stale thread
    on OpenAI's side — annoying for the operator's account history
    but not data loss.
    """
    provider = ctx.get("old_provider")
    thread_id = ctx.get("last_known_external_ref")
    if not provider or not thread_id:
        # No thread to flush — succeed trivially. Marker still advances.
        logger.info(
            "memory.flush.openai_assistants: no thread_id known, skipping"
        )
        return True

    url = f"{_base_url(provider)}/threads/{thread_id}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.delete(url, headers=await _api_headers(provider))
        if r.status_code == 200 or r.status_code == 204:
            logger.info(
                f"memory.flush.openai_assistants: deleted thread "
                f"{thread_id} provider={provider.id}"
            )
            return True
        if r.status_code == 404:
            # Already gone — semantically a successful flush
            logger.info(
                f"memory.flush.openai_assistants: thread {thread_id} "
                f"already 404 — treating as success"
            )
            return True
        logger.warning(
            f"memory.flush.openai_assistants: DELETE returned "
            f"{r.status_code} body={r.text[:200]}"
        )
        return False
    except Exception as e:
        logger.warning(
            f"memory.flush.openai_assistants: network error err={e!r}"
        )
        return False


# ── Phase 7 — recovery handler ─────────────────────────────────────


async def recover_openai_assistants(ctx: dict) -> Optional[str]:
    """Read message history back from the upstream Assistants thread
    and assemble a plain-text representation as the recovered content.

    Returns the assembled text on success, None when the thread can't
    be read (missing ref, network error, 404, empty messages).

    Schema: the Assistants ``/messages`` endpoint returns
    ``{data: [{role, content: [{type:"text", text:{value: "..."}}, ...], ...}]}``.
    Newest-first. We reverse to chronological order for the
    reconstructed memory blob.
    """
    provider = ctx.get("old_provider")
    thread_id = ctx.get("last_known_external_ref")
    if not provider or not thread_id:
        return None

    url = f"{_base_url(provider)}/threads/{thread_id}/messages"
    try:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.get(url, headers=await _api_headers(provider), params={"limit": 100})
        if r.status_code != 200:
            logger.info(
                f"memory.recover.openai_assistants: GET returned "
                f"{r.status_code} (thread {thread_id})"
            )
            return None
        data = r.json().get("data") or []
        if not data:
            return None
        # data is newest-first; reverse for chronological reconstruction
        chronological = list(reversed(data))
        lines: list[str] = []
        for msg in chronological:
            role = msg.get("role") or "?"
            blocks = msg.get("content") or []
            parts: list[str] = []
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                if blk.get("type") == "text":
                    txt = (blk.get("text") or {}).get("value") or ""
                    if txt:
                        parts.append(txt)
            joined = "\n".join(parts).strip()
            if joined:
                lines.append(f"[{role}] {joined}")
        if not lines:
            return None
        recovered = "\n".join(lines)
        logger.info(
            f"memory.recover.openai_assistants: reconstructed "
            f"{len(recovered)} chars from thread {thread_id}"
        )
        return recovered
    except Exception as e:
        logger.warning(
            f"memory.recover.openai_assistants: network error err={e!r}"
        )
        return None


# ── Registration ───────────────────────────────────────────────────


def register() -> None:
    """Idempotent registration. Called from ``app/memory/__init__.py``
    at module import. The proxy doesn't have an ``openai-assistants``
    provider type today, so the handlers never fire — they're staged
    for when the operator adds one."""
    from app.memory.flush import register_handler as register_flush
    from app.memory.recover import register_handler as register_recover
    register_flush("openai-assistants", flush_openai_assistants)
    register_recover("openai-assistants", recover_openai_assistants)
