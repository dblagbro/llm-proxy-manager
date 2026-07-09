"""v5.12.2 — Ship 3 of the v5.10 design: caller-side accept handler.

The closed-loop story is now:

1. Capability scout sees a refusal pattern → bumps the score (Ship 2).
2. Score crosses threshold → response emits ``X-Proxy-MCP-Suggestion``
   (Ship 1, dual-emit via header + MCP notification).
3. Caller's next request includes ``X-Proxy-Accept-MCP: <tool>`` (or
   comma-separated tools). THIS module handles that request side:
   - Validate each tool exists in the proxy's MCP catalog.
   - Append to the calling api_key's ``mcp_tools_allow`` list (dedupe).
   - Write a ``compliance_policy_changes`` row with
     ``reason='mcp_tool_adopted_via_accept_header'`` (audit scope
     ``api_key`` per operator decision 2026-06-30).
   - For unknown tools: set a ``Warning:`` header on the response so
     the caller knows the request was honored partially.
4. Subsequent requests get the tool via Path B auto-injection.

Audit row pattern matches the v5.1.2 retention-edit + v5.10 Ship 1
emission contract. ``before_state`` / ``after_state`` capture the
exact mcp_tools_allow list mutation, so the audit reads cleanly.

Failures NEVER raise — the accept header is best-effort observability,
not enforcement. If the audit row fails to write or the policy mutation
fails to commit, the response goes out unchanged. The bot will see no
``X-Proxy-MCP-Accept-Status: ok`` on the response and can retry.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


HEADER_NAME = "X-Proxy-Accept-MCP"
RESPONSE_STATUS_HEADER = "X-Proxy-MCP-Accept-Status"
WARNING_HEADER = "Warning"


def _parse_accept_header(value: str) -> list[str]:
    """Parse ``tool1,tool2,tool3`` (with optional ``tool=name`` prefix
    for legacy compatibility) into a clean list. Whitespace is
    stripped; empty entries are dropped; duplicates are deduped while
    preserving caller-supplied order."""
    if not value:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in value.split(","):
        chunk = raw.strip()
        if not chunk:
            continue
        # Tolerate "tool=name" form (some clients may borrow the
        # X-Proxy-MCP-Suggestion header's JSON shape and copy the
        # value verbatim).
        if "=" in chunk and chunk.lower().startswith("tool="):
            chunk = chunk.split("=", 1)[1].strip()
        if not chunk or chunk in seen:
            continue
        seen.add(chunk)
        cleaned.append(chunk)
    return cleaned


async def _known_tool_names() -> set[str]:
    """The set of tool names the proxy's MCP server actually exposes.
    Used to filter accept requests so a bot can't ask for a tool the
    proxy doesn't host (which would be a silent no-op at Path B
    injection time).

    Cached per-process for 5s — the FastMCP list_tools surface is
    cheap to call but accumulating it on every accept-header request
    would still be measurable under heavy fan-out.
    """
    try:
        from app.mcp_server.server import build_mcp_app
        # build_mcp_app() returns the FastMCP instance via state. We
        # only need the inner ``mcp`` reference. Memoize after first
        # build (FastMCP is idempotent + we only need the tool names).
        global _CACHED_TOOLS, _CACHED_TS
        import time
        now = time.time()
        if _CACHED_TOOLS is not None and (now - _CACHED_TS) < 5.0:
            return _CACHED_TOOLS
        sub_app = build_mcp_app()
        mcp = sub_app.state.mcp
        # FastMCP's list_tools returns rich objects; ``.name`` is the
        # tool identifier we register via the @mcp.tool() decorator.
        tools = await mcp.list_tools()
        names = {getattr(t, "name", None) for t in tools}
        names.discard(None)
        _CACHED_TOOLS = set(names)
        _CACHED_TS = now
        return _CACHED_TOOLS
    except Exception as exc:
        logger.debug("_known_tool_names lookup failed: %s", exc)
        return set()


_CACHED_TOOLS: Optional[set[str]] = None
_CACHED_TS: float = 0.0


async def _append_to_allow_list(
    db: AsyncSession,
    api_key_id: str,
    new_tools: list[str],
) -> tuple[list[str], list[str]]:
    """Append ``new_tools`` to the api_key's mcp_tools_allow column.
    Returns ``(before, after)`` snapshots for the audit row.

    If the column is currently None we treat that as "no per-key
    allow-list yet" — accepted tools become the FIRST entries
    (effectively switching the key into per-key allow-list mode).
    If it's an empty list, same behavior.
    """
    from app.models.db import ApiKey
    rs = await db.execute(select(ApiKey).where(ApiKey.id == api_key_id).limit(1))
    key = rs.scalar_one_or_none()
    if key is None:
        return ([], [])
    before = list(getattr(key, "mcp_tools_allow", None) or [])
    after = list(before)
    for t in new_tools:
        if t not in after:
            after.append(t)
    key.mcp_tools_allow = after
    await db.commit()
    return (before, after)


async def _record_adoption_audit(
    db: AsyncSession,
    api_key_id: str,
    accepted: list[str],
    rejected: list[str],
    before: list[str],
    after: list[str],
) -> None:
    """Audit row in ``compliance_policy_changes`` — scope ``per_key``
    per operator decision 2026-06-30, reason
    ``mcp_tool_adopted_via_accept_header``. Captures the before/after
    allow-list snapshots so an auditor can reconstruct any past
    adoption purely from the audit trail."""
    try:
        from app.models.db import CompliancePolicyChange
        now = datetime.utcnow()
        before_state = json.dumps({"mcp_tools_allow": before})
        after_state = json.dumps({
            "mcp_tools_allow": after,
            "accepted_tools": accepted,
            "rejected_unknown_tools": rejected,
        })
        db.add(CompliancePolicyChange(
            policy_change_id=(
                f"mcp-accept:{api_key_id}:{','.join(sorted(accepted))}:"
                f"{int(now.timestamp())}"
            ),
            changed_at=now,
            changed_by_user_id="system:capability_scout_accept",
            scope="per_key",
            target_id=api_key_id,
            before_state=before_state,
            after_state=after_state,
            reason="mcp_tool_adopted_via_accept_header",
            applied_to_peers="[]",
            cluster_sync_status="local-only",
        ))
        await db.commit()
    except Exception as exc:
        logger.debug("accept_handler audit row failed: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass


async def process_accept_header(
    db: AsyncSession,
    api_key_id: Optional[str],
    header_value: Optional[str],
    resp_headers: dict,
) -> dict:
    """Top-level entry point. Called from messages.py / completions.py
    before the upstream dispatch so the new tool is reflected in the
    very same request's Path B injection (next-request semantics would
    waste one round-trip).

    Returns a small status dict for logging / observability:
    ``{"requested": [...], "accepted": [...], "rejected": [...]}``.

    Sets these response headers:
    - ``X-Proxy-MCP-Accept-Status: ok`` (always, when any tool was
      processed — success or partial)
    - ``Warning: 299 - "Tool 'X' not in proxy MCP catalog; ignored"``
      for each unknown tool (comma-joined if multiple)
    """
    out = {"requested": [], "accepted": [], "rejected": []}
    if not api_key_id or not header_value:
        return out
    try:
        requested = _parse_accept_header(header_value)
        out["requested"] = list(requested)
        if not requested:
            return out
        known = await _known_tool_names()
        accepted = [t for t in requested if t in known]
        rejected = [t for t in requested if t not in known]
        out["accepted"] = accepted
        out["rejected"] = rejected

        if accepted:
            before, after = await _append_to_allow_list(db, api_key_id, accepted)
            if before != after:
                await _record_adoption_audit(
                    db, api_key_id, accepted, rejected, before, after,
                )

        if accepted or rejected:
            resp_headers[RESPONSE_STATUS_HEADER] = (
                "ok" if accepted and not rejected
                else "partial" if accepted and rejected
                else "rejected"
            )

        if rejected:
            warn = ", ".join(
                f'299 - "Tool {t!r} not in proxy MCP catalog; ignored"'
                for t in rejected
            )
            existing = resp_headers.get(WARNING_HEADER)
            resp_headers[WARNING_HEADER] = (
                f"{existing}, {warn}" if existing else warn
            )

        return out
    except Exception as exc:
        logger.debug("process_accept_header failed: %s", exc)
        return out
