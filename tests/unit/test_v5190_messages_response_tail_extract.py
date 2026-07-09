"""v5.19.0 — messages.py response-tail extract.

Pulls four try/except blocks (v5.7.6 suggestion header + v5.12.2 accept
handler + v5.16.0 config echo + v5.14.0 hook runner) into
``app/api/_messages_response_tail.py`` behind one call. No behavior
change. Extracted because ``messages.py`` was at 1146 LOC (over the
project's own 1000-line ceiling) and every recent ship (v5.14, v5.15.1,
v5.16.0, v5.18.0) added ~10 LOC to the same block.

This test pins the shape of the extraction so future tail-block
additions land in the extracted module, not back in messages.py.
"""
from __future__ import annotations
from pathlib import Path


# ── (1) The extracted module exists ─────────────────────────────────


def test_extract_module_exists_and_exports_apply_response_tail():
    from app.api._messages_response_tail import apply_response_tail
    import inspect
    assert inspect.iscoroutinefunction(apply_response_tail)


def test_extract_module_ships_all_four_blocks():
    """Static grep on the extract file — every original tail block is
    present. Same-line-count check would be too brittle."""
    src = Path("app/api/_messages_response_tail.py").read_text()
    # (1) Capability scout suggestion header
    assert "apply_suggestion_header" in src
    # (2) Accept-MCP handler (individual + blob)
    assert "process_accept_header" in src
    assert 'read_config_key(' in src
    assert '"accept_mcp"' in src
    assert '"X-Proxy-Accept-MCP"' in src
    # (3) Config-applied echo
    assert "emit_config_applied_header" in src
    # (4) Response hooks runner
    assert "apply_response_hooks(" in src
    assert 'handler_id="messages"' in src


def test_extract_preserves_hook_context_fields():
    """v5.14 + v5.15.1 + v5.18.0 accumulated these fields on HookContext
    — the extraction MUST keep all of them wired."""
    src = Path("app/api/_messages_response_tail.py").read_text()
    for field in (
        "requested_model",
        "served_model",
        "api_key_id",
        "provider_id",
        "compliance_event_id",
        "substituted",
        "key_record",
        "request",
    ):
        assert f"{field}=" in src, f"HookContext.{field} missing from extract"
    # v5.18.0 threading — substitution_reason via extra dict
    assert "substitution_reason" in src


def test_extract_preserves_per_block_exception_swallow():
    """Each block must independently swallow Exception so a failure in
    (1) doesn't skip (2) etc. Static-grep counts try/except pairs."""
    src = Path("app/api/_messages_response_tail.py").read_text()
    try_count = src.count("try:")
    except_count = src.count("except Exception:")
    # v5.20.0 added a 5th block (refusal detection at position 0) with
    # a nested try around the activity_log write, giving 6 try blocks
    # total (5 outer + 1 nested inside the refusal block).
    assert try_count == 6, f"expected 6 try blocks after v5.20.0, got {try_count}"
    assert except_count == 6, f"expected 6 except blocks after v5.20.0, got {except_count}"


# ── (2) messages.py is smaller + calls the extract ─────────────────


def test_messages_calls_extract_not_inline():
    """messages.py MUST NOT still contain the inline versions of the
    extracted blocks — otherwise the extraction did nothing."""
    src = Path("app/api/messages.py").read_text()
    # Positive: the call site is present.
    assert "from app.api._messages_response_tail import apply_response_tail" in src
    assert "await apply_response_tail(" in src


def test_messages_inline_blocks_removed():
    """These inline strings existed pre-v5.19.0. If they reappear it
    means someone re-inlined a block; the extract stops paying off."""
    src = Path("app/api/messages.py").read_text()
    # Suggestion header block preamble is gone from messages.py
    assert "from app.capability_scout.suggestion_emit import apply_suggestion_header" not in src, (
        "response-tail block re-inlined into messages.py; extract regressed"
    )
    # accept_handler block preamble is gone
    assert "from app.capability_scout.accept_handler import process_accept_header" not in src, (
        "response-tail block re-inlined into messages.py; extract regressed"
    )


def test_messages_line_count_dropped():
    """Sanity: messages.py should be smaller than pre-refactor 1146 LOC."""
    lines = Path("app/api/messages.py").read_text().count("\n") + 1
    assert lines < 1146, (
        f"messages.py at {lines} LOC — v5.19.0 extract meant to drop it"
    )


# ── (3) v5.14.1 handler-runner pin still passes ─────────────────────


def test_hook_runner_pin_still_holds():
    """v5.14.1 pin: messages.py must still call apply_response_hooks
    with handler_id='messages'. After extract, the call lives in
    _messages_response_tail but the pin should still recognize it."""
    tail_src = Path("app/api/_messages_response_tail.py").read_text()
    assert "await apply_response_hooks(" in tail_src
    assert 'handler_id="messages"' in tail_src


# ── (4) Version ────────────────────────────────────────────────────


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 19, 0), (
        f"expected >= 5.19.0, got {major}.{minor}.{patch}"
    )
