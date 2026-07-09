"""v5.10.0 Ship 1 — `X-Proxy-MCP-Suggestion` response-header emitter.

Wire path:

  /v1/messages (etc) → upstream call → response shaped → ``apply_suggestion_header()``
  reads the caller's max score from ``caller_capability_score``.

  If max score >= threshold (default 50, operator-tunable via
  ``mcp_suggestion_threshold``), set the header. Otherwise no-op.

Dual-emit (per 2026-06-29 v5.10 design addendum + 2026-06-30 operator
interview):

- REST callers always get the JSON-in-header
  ``X-Proxy-MCP-Suggestion: {"tool":"…","score":NN,"reason":"…"}``.
- Callers with an open /mcp connection ALSO get an MCP
  ``notifications/message`` event over that transport. The MCP path
  is best-effort; failure does not affect the REST header. The MCP
  half is gated on whether we can resolve a live FastMCP session for
  this api_key — Ship 1 ships the REST half synchronously; the MCP
  half is wired as a follow-up hook in suggestion_emit_mcp.py.

Audit (per operator interview decision: scope ``api_key``): every
emission writes one ``compliance_policy_changes`` row capturing
``api_key_id, scope='api_key', reason='mcp_suggestion_emitted',
event_meta={tool, score}``. Single source of truth for "did the proxy
nudge this caller toward tool X?" — matches the existing v5.1.2
retention-edit pattern.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.capability_scout.score import best_suggestion_for_key, is_emission_enabled

logger = logging.getLogger(__name__)


HEADER_NAME = "X-Proxy-MCP-Suggestion"


# Lightweight per-(api_key, tool) memo so we don't write the SAME audit
# row + header on every response in a tight loop. Caller will still see
# the header on the NEXT distinct response after a 60s cooldown.
_RECENT_EMITS: Dict[str, float] = {}
_RECENT_TTL_SEC = 60.0


def _why_for_tool(tool: str) -> str:
    """Short human-readable rationale rendered into the header JSON.
    Looked up by tool name. Falls back to a generic phrase if a
    future tool is added without a why entry — the score is the
    primary signal, the why is decoration."""
    table = {
        "read_xlsx_to_markdown": "spreadsheets refused as binary in recent calls",
        "convert_document_to_markdown": "non-text docs requested in recent calls",
        "fetch_url": "URL fetches refused in recent calls",
    }
    return table.get(tool, "repeated capability-refusal pattern detected")


async def _record_audit_row(
    db: AsyncSession,
    api_key_id: str,
    tool: str,
    score: int,
) -> None:
    """Audit row in ``compliance_policy_changes``. Schema fields
    (scope='per_key', target_id=api_key_id) match the existing v5.1.2
    retention-edit pattern. Failures are swallowed; this is
    observability, not enforcement.
    """
    try:
        from datetime import datetime
        import json as _json
        from app.models.db import CompliancePolicyChange
        now = datetime.utcnow()
        diff_after = _json.dumps({
            "mcp_suggestion": {
                "suggested_tool": tool,
                "score": score,
            }
        })
        db.add(CompliancePolicyChange(
            policy_change_id=f"mcp-suggest:{api_key_id}:{tool}:{int(now.timestamp())}",
            changed_at=now,
            changed_by_user_id="system:capability_scout",
            scope="per_key",
            target_id=api_key_id,
            before_state="{}",
            after_state=diff_after,
            reason="mcp_suggestion_emitted",
            applied_to_peers="[]",
            cluster_sync_status="local-only",
        ))
        await db.commit()
    except Exception as exc:
        logger.debug("suggestion_emit audit row failed: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass


async def apply_suggestion_header(
    db: AsyncSession,
    api_key_id: Optional[str],
    response_headers: Dict[str, str],
) -> Optional[str]:
    """Read the caller's max score and, if it crosses the threshold,
    set ``X-Proxy-MCP-Suggestion`` on the response and return the
    suggested tool name. Returns None when no header was emitted.

    Idempotent on repeated calls within the cooldown window — caller
    sees the header on a fresh response after ~60s, not every
    response in a tight retry loop.

    Failure semantics: NEVER raises. If anything goes sideways we log
    at DEBUG and return None; the response goes out without the
    header. This is observability, not enforcement.
    """
    if not api_key_id:
        return None
    try:
        if not await is_emission_enabled():
            return None
        best = await best_suggestion_for_key(db, api_key_id)
        if best is None:
            return None
        tool, score = best

        # Cooldown — same (key, tool) doesn't re-emit in tight loop.
        import time
        cache_key = f"{api_key_id}:{tool}"
        now = time.time()
        last = _RECENT_EMITS.get(cache_key)
        if last is not None and (now - last) < _RECENT_TTL_SEC:
            # Header re-emitted (so the caller sees consistent signal)
            # but audit row is skipped (no double-write).
            payload = json.dumps({
                "tool": tool,
                "score": score,
                "why": _why_for_tool(tool),
            }, separators=(",", ":"))
            response_headers[HEADER_NAME] = payload
            return tool
        _RECENT_EMITS[cache_key] = now

        # GC stale memo entries — keep the dict bounded.
        if len(_RECENT_EMITS) > 1000:
            cutoff = now - _RECENT_TTL_SEC
            for k, t in list(_RECENT_EMITS.items()):
                if t < cutoff:
                    _RECENT_EMITS.pop(k, None)

        payload = json.dumps({
            "tool": tool,
            "score": score,
            "why": _why_for_tool(tool),
        }, separators=(",", ":"))
        response_headers[HEADER_NAME] = payload

        # v5.12.2 Ship 1.1 — MCP-native dual-emit half. Push the same
        # suggestion to the per-key notification buffer so the next MCP
        # tool call from this caller (if any) surfaces it as a standard
        # MCP notifications/message event. Path A callers see it in
        # their protocol; Path B / REST callers already have the
        # X-Proxy-MCP-Suggestion header above.
        try:
            from app.capability_scout.suggestion_buffer_mcp import push_suggestion_notification
            push_suggestion_notification(api_key_id, tool, score, _why_for_tool(tool))
        except Exception:
            pass

        # Audit row (best-effort; see _record_audit_row).
        await _record_audit_row(db, api_key_id, tool, score)
        return tool
    except Exception as exc:
        logger.debug("apply_suggestion_header failed: %s", exc)
        return None
