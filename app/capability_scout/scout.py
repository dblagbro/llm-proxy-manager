"""v5.7.6 — refusal-pattern detector + ActivityLog suggestion emitter.

Sourced from observed LLM refusal phrasings across the proxy fleet:
Claude, GPT-4o/o1, Gemini, Grok, Cohere all say variants of "I can't
read X" / "I don't have access to X" when the user asks for something
that an MCP-style tool could satisfy. Each pattern is paired with the
MCP tool that would have made the answer possible.

We deliberately keep this lightweight: regex pre-filter + first-match
wins. No NLP, no LLM-based classification — the operator already pays
for one LLM call per request and asking a second LLM to grade the
first would double cost for marginal gain.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional

logger = logging.getLogger(__name__)


SYSTEM_SETTING_KEY = "capability_scout.enabled"
EVENT_TYPE = "mcp_capability_suggestion"


@dataclass(frozen=True)
class RefusalPattern:
    """One refusal-phrase → suggested MCP tool mapping."""
    name: str                # short stable id ("cant_read_files", …)
    pattern: re.Pattern[str] # pre-compiled case-insensitive regex
    suggested_tool: str      # MCP tool that would unblock the response
    why: str                 # operator-facing rationale


def _r(p: str) -> re.Pattern[str]:
    return re.compile(p, re.IGNORECASE)


# Order matters: first match wins. Put the more specific patterns
# (excel, pdf, docx) above the generic file/url ones so a user asking
# about a .xlsx attachment doesn't get the generic "fetch_url"
# suggestion when read_xlsx_to_markdown is the better fit.
REFUSAL_PATTERNS: List[RefusalPattern] = [
    RefusalPattern(
        name="cant_read_excel",
        pattern=_r(r"\b(can't|cannot|unable to|don'?t have (?:the )?ability to)\s+(?:read|open|process|access)\b[^.]{0,40}\b(?:excel|xlsx|spreadsheet|workbook|\.xls)"),
        suggested_tool="read_xlsx_to_markdown",
        why="bot refused to read an Excel/spreadsheet — read_xlsx_to_markdown converts XLSX to markdown inline",
    ),
    RefusalPattern(
        name="cant_read_document",
        pattern=_r(r"\b(can't|cannot|unable to|don'?t have (?:the )?ability to)\s+(?:read|open|process|access)\b[^.]{0,40}\b(?:pdf|docx|word document|powerpoint|pptx|epub|html file)"),
        suggested_tool="convert_document_to_markdown",
        why="bot refused to read a document — convert_document_to_markdown handles PDF/DOCX/PPTX/EPUB/HTML",
    ),
    RefusalPattern(
        name="cant_fetch_url",
        pattern=_r(r"\b(can't|cannot|unable to|don'?t have (?:the )?ability to)\s+(?:fetch|access|read|open|retrieve|browse)\b[^.]{0,40}\b(?:url|website|web ?page|link|http)"),
        suggested_tool="fetch_url",
        why="bot refused to fetch a URL — fetch_url is the proxy-injected web reader",
    ),
    RefusalPattern(
        name="no_internet_access",
        pattern=_r(r"\b(no|don'?t have)\s+(?:internet|web|external|live)\s+access\b"),
        suggested_tool="fetch_url",
        why="bot claimed no internet access — fetch_url bridges that gap",
    ),
    RefusalPattern(
        name="no_realtime_data",
        pattern=_r(r"\b(can't|cannot|don'?t have|unable to)\s+(?:access|get|retrieve)\s+(?:real[- ]?time|live|current)\s+(?:data|information|prices|news)"),
        suggested_tool="fetch_url",
        why="bot claimed no real-time data access — fetch_url + a search URL closes the gap",
    ),
    RefusalPattern(
        name="cant_read_files_generic",
        pattern=_r(r"\b(can't|cannot|unable to|don'?t have (?:the )?ability to)\s+(?:read|open|process|access)\b[^.]{0,40}\b(?:files?|attachments?|uploads?|documents?)\b"),
        suggested_tool="convert_document_to_markdown",
        why="bot refused to read files generically — convert_document_to_markdown is the universal reader",
    ),
    RefusalPattern(
        name="no_file_system_access",
        pattern=_r(r"\b(no|don'?t have)\s+(?:access to|the ability to access)\s+(?:your|the|any)?\s*(?:file|filesystem|local|computer|machine)"),
        suggested_tool="convert_document_to_markdown",
        why="bot claimed no file/system access — proxy-injected document tools can read user-supplied bytes",
    ),
]


async def is_enabled() -> bool:
    """Worker switch — default OFF.

    Reads the ``capability_scout.enabled`` system_setting. Defaults to
    False so a fresh deploy never starts emitting suggestion rows
    until the operator opts in.
    """
    try:
        from app.models.database import AsyncSessionLocal
        from app.models.db_compliance import SystemSetting  # noqa: F401
        from sqlalchemy import select
        from app.models.db_base import Base  # noqa: F401
        # SystemSetting lives in db.py for legacy reasons; the
        # canonical accessor is via app.models.database.
        from app.models.db import SystemSetting as _SS
        async with AsyncSessionLocal() as db:
            rs = await db.execute(select(_SS).where(_SS.key == SYSTEM_SETTING_KEY))
            row = rs.scalar_one_or_none()
            if row is None:
                return False
            val = (row.value or "").strip().lower()
            return val in ("1", "true", "yes", "on")
    except Exception as exc:
        logger.debug("capability_scout.is_enabled probe failed: %s", exc)
        return False


def _extract_text_from_anthropic_response(resp: Any) -> str:
    """Pull all text content out of an Anthropic-shape response.

    ``resp`` looks like
    ``{"content":[{"type":"text","text":"…"}, {"type":"tool_use",…}, …]}``.
    We only care about text blocks. Tool_use blocks are skipped — they
    can never refuse anything.
    """
    if not isinstance(resp, dict):
        return ""
    content = resp.get("content") or []
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            t = block.get("text") or ""
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks)


def _window(text: str, span: tuple[int, int], radius: int = 40) -> str:
    """Return a small context window around the matched span."""
    start, end = span
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{text[lo:hi].strip()}{suffix}"


def scan_response_text(text: str) -> List[dict]:
    """Run every pattern over ``text``, return list of hits.

    Each hit is a dict ``{pattern_name, suggested_tool, why,
    matched_snippet}``. Empty list when no pattern fires.

    Pure function — no DB, no I/O, fast enough to run inline on every
    response (~50 µs for a 4 KB response on the typical CPU).
    """
    if not text or not isinstance(text, str):
        return []
    out: List[dict] = []
    seen_tools: set[str] = set()
    for pat in REFUSAL_PATTERNS:
        m = pat.pattern.search(text)
        if not m:
            continue
        # Dedup: only one suggestion per tool per response. Two
        # "can't read pdf" hits → one row.
        if pat.suggested_tool in seen_tools:
            continue
        seen_tools.add(pat.suggested_tool)
        out.append({
            "pattern_name": pat.name,
            "suggested_tool": pat.suggested_tool,
            "why": pat.why,
            "matched_snippet": _window(text, m.span()),
        })
    return out


async def emit_suggestions(
    *,
    db,
    api_key_id: Optional[str],
    provider_id: Optional[int],
    suggestions: Iterable[dict],
) -> int:
    """Write ``suggestions`` to activity_log as ``mcp_capability_suggestion`` rows.

    Idempotent within a 1-hour window per (api_key_id, suggested_tool)
    — a noisy bot that refuses 200 PDF requests in an hour only
    generates 1 row per tool suggestion. Returns the row count written.
    Failures are swallowed (and logged) — the scout must NEVER break
    the response path.
    """
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import select
    from app.models.db import ActivityLog

    written = 0
    try:
        suggestions = list(suggestions)
        if not suggestions:
            return 0
        look_back = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
        for s in suggestions:
            tool = s.get("suggested_tool")
            if not tool:
                continue
            try:
                existing = await db.execute(
                    select(ActivityLog)
                    .where(ActivityLog.event_type == EVENT_TYPE)
                    .where(ActivityLog.api_key_id == api_key_id)
                    .where(ActivityLog.created_at >= look_back)
                    .where(ActivityLog.event_meta["suggested_tool"].as_string() == tool)
                    .limit(1)
                )
                if existing.scalar_one_or_none() is not None:
                    continue
            except Exception:
                # Some SQLite builds don't support the JSON path syntax
                # above. Fall through and emit anyway — at worst we
                # write a duplicate suggestion.
                pass
            try:
                db.add(ActivityLog(
                    created_at=datetime.utcnow(),
                    severity="info",
                    event_type=EVENT_TYPE,
                    api_key_id=api_key_id,
                    provider_id=provider_id,
                    message=(
                        f"Capability scout suggestion: "
                        f"call could have used `{tool}` — {s.get('why', '')}"
                    ),
                    event_meta={
                        "pattern_name": s.get("pattern_name"),
                        "suggested_tool": tool,
                        "why": s.get("why"),
                        "matched_snippet": s.get("matched_snippet"),
                    },
                ))
                written += 1
            except Exception as exc:
                logger.debug("capability_scout.emit row failed: %s", exc)
            # v5.10.0 — bump caller score so the response middleware
            # can decide whether to emit X-Proxy-MCP-Suggestion. Done
            # AFTER the activity_log write so the existing audit-row
            # behavior is unchanged if score bumping fails.
            if api_key_id:
                try:
                    from app.capability_scout.score import bump_score
                    await bump_score(db, api_key_id, tool)
                except Exception as exc:
                    logger.debug("capability_scout.score_bump failed: %s", exc)
        try:
            await db.commit()
        except Exception as exc:
            logger.debug("capability_scout.emit commit failed: %s", exc)
    except Exception as exc:
        logger.debug("capability_scout.emit unexpected error: %s", exc)
    return written


async def scan_and_emit_for_response(
    *,
    db,
    api_key_id: Optional[str],
    provider_id: Optional[int],
    anthropic_response: Any,
) -> int:
    """End-to-end helper for the hook in messages.py.

    Cheap fast path — runs ``is_enabled()`` first and bails on False.
    """
    if not await is_enabled():
        return 0
    text = _extract_text_from_anthropic_response(anthropic_response)
    if not text:
        return 0
    hits = scan_response_text(text)
    if not hits:
        return 0
    return await emit_suggestions(
        db=db,
        api_key_id=api_key_id,
        provider_id=provider_id,
        suggestions=hits,
    )
