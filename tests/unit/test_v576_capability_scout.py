"""v5.7.6 — capability scout (refusal-pattern detector) tests."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_scout_module_exists():
    from app.capability_scout import (
        scan_response_text,
        emit_suggestions,
        is_enabled,
        REFUSAL_PATTERNS,
    )
    assert callable(scan_response_text)
    assert callable(emit_suggestions)
    assert callable(is_enabled)
    assert len(REFUSAL_PATTERNS) >= 5


def test_scan_detects_excel_refusal():
    from app.capability_scout import scan_response_text
    txt = "I'm sorry, but I can't read Excel files directly. Could you paste the data?"
    hits = scan_response_text(txt)
    assert len(hits) >= 1
    assert hits[0]["suggested_tool"] == "read_xlsx_to_markdown"
    assert "matched_snippet" in hits[0]


def test_scan_detects_pdf_refusal():
    from app.capability_scout import scan_response_text
    txt = "Unfortunately I cannot read PDF documents. Please share the text directly."
    hits = scan_response_text(txt)
    tools = {h["suggested_tool"] for h in hits}
    assert "convert_document_to_markdown" in tools


def test_scan_detects_url_refusal():
    from app.capability_scout import scan_response_text
    txt = "I can't fetch URLs or access websites — I work only with what you give me."
    hits = scan_response_text(txt)
    tools = {h["suggested_tool"] for h in hits}
    assert "fetch_url" in tools


def test_scan_detects_no_internet_access():
    from app.capability_scout import scan_response_text
    txt = "I don't have internet access, so I can't look that up live."
    hits = scan_response_text(txt)
    tools = {h["suggested_tool"] for h in hits}
    assert "fetch_url" in tools


def test_scan_no_false_positive_on_normal_response():
    from app.capability_scout import scan_response_text
    txt = "Here's the answer: 42. The result was computed in 3 ms and stored in memory."
    hits = scan_response_text(txt)
    assert hits == []


def test_scan_dedups_same_tool_within_response():
    """If a response triggers two patterns that map to the same tool,
    only one suggestion fires per tool. Keeps noise down."""
    from app.capability_scout import scan_response_text
    txt = "I can't fetch URLs, and I don't have internet access either."
    hits = scan_response_text(txt)
    tools = [h["suggested_tool"] for h in hits]
    # both patterns suggest fetch_url; only one row.
    assert tools.count("fetch_url") == 1


def test_extract_text_skips_tool_use_blocks():
    from app.capability_scout.scout import _extract_text_from_anthropic_response
    resp = {
        "content": [
            {"type": "text", "text": "Sure, calling the tool."},
            {"type": "tool_use", "name": "x", "input": {}},
            {"type": "text", "text": "Done."},
        ]
    }
    out = _extract_text_from_anthropic_response(resp)
    assert "Sure" in out and "Done" in out
    assert "tool_use" not in out


def test_extract_text_handles_malformed_response():
    from app.capability_scout.scout import _extract_text_from_anthropic_response
    assert _extract_text_from_anthropic_response(None) == ""
    assert _extract_text_from_anthropic_response("not a dict") == ""
    assert _extract_text_from_anthropic_response({"no_content": True}) == ""
    assert _extract_text_from_anthropic_response({"content": "not a list"}) == ""


def test_messages_handler_wires_capability_scout():
    """Static-grep contract: the non-streaming /v1/messages return path
    invokes the scout. If this fails, someone removed the hook."""
    src = Path("app/api/messages.py").read_text()
    assert "scan_and_emit_for_response" in src
    assert "X-Capability-Scout-Suggestions" in src


def test_admin_capability_router_registered():
    src = Path("app/main.py").read_text()
    assert "admin_mcp_capability_router" in src
    assert "app.include_router(admin_mcp_capability_router)" in src


def test_admin_capability_endpoint_exists():
    from app.api.admin_mcp_capability_suggestions import router
    paths = {r.path for r in router.routes if hasattr(r, "path")}
    assert any("/capability-suggestions" in p for p in paths)


@pytest.mark.asyncio
async def test_emit_suggestions_writes_one_row_per_tool():
    """emit_suggestions(...) deduplicates per (key, tool) within 1h.
    On a clean DB (no existing rows), each unique tool → 1 row."""
    from app.capability_scout import emit_suggestions
    db = MagicMock()
    empty_rs = MagicMock(); empty_rs.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=empty_rs)
    db.add = MagicMock()
    db.commit = AsyncMock()

    n = await emit_suggestions(
        db=db,
        api_key_id="key-abc",
        provider_id=42,
        suggestions=[
            {"pattern_name": "cant_read_excel", "suggested_tool": "read_xlsx_to_markdown", "why": "x", "matched_snippet": "y"},
            {"pattern_name": "cant_fetch_url", "suggested_tool": "fetch_url", "why": "u", "matched_snippet": "v"},
        ],
    )
    assert n == 2
    assert db.add.call_count == 2


@pytest.mark.asyncio
async def test_scan_and_emit_skips_when_disabled(monkeypatch):
    """When the system_setting is off, scout never touches the DB."""
    from app.capability_scout import scout
    monkeypatch.setattr(scout, "is_enabled", AsyncMock(return_value=False))
    db = MagicMock()
    db.execute = AsyncMock()
    n = await scout.scan_and_emit_for_response(
        db=db,
        api_key_id="x",
        provider_id=1,
        anthropic_response={"content": [{"type": "text", "text": "I can't read PDF files."}]},
    )
    assert n == 0
    db.execute.assert_not_called()
