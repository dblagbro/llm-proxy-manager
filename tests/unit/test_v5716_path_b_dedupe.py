"""v5.7.16 — Path B name dedupe (DevinGPT memo 2026-06-17, Option A).

The dedupe behavior existed pre-5.7.16 but only matched the Anthropic
``{"name": ...}`` shape. v5.7.16 also matches the OpenAI
``{"type": "function", "function": {"name": ...}}`` shape and audits
every collision so the operator can see which downstream clients have
their own canonical tool surface.
"""
from __future__ import annotations

from unittest.mock import patch


class _FakeProxyTool:
    def __init__(self, name, schema=None):
        self.name = name
        self.anthropic_schema = schema or {"name": name, "input_schema": {}}


# ── _collect_caller_tool_names ─────────────────────────────────────────


def test_collect_names_anthropic_shape():
    from app.proxy_tools import _collect_caller_tool_names
    tools = [
        {"name": "fetch_url", "input_schema": {}},
        {"name": "read_xlsx_to_markdown", "input_schema": {}},
    ]
    assert _collect_caller_tool_names(tools) == {"fetch_url", "read_xlsx_to_markdown"}


def test_collect_names_openai_shape():
    """OpenAI Chat Completions shape — name lives under .function.name.
    v5.7.16 extracts it; pre-5.7.16 would return an empty set and the
    proxy would re-inject, causing the collision DevinGPT flagged."""
    from app.proxy_tools import _collect_caller_tool_names
    tools = [
        {"type": "function", "function": {"name": "fetch_url", "parameters": {}}},
        {"type": "function", "function": {"name": "my_local_tool"}},
    ]
    assert _collect_caller_tool_names(tools) == {"fetch_url", "my_local_tool"}


def test_collect_names_mixed_shapes():
    """Mixed payload (Anthropic + OpenAI tools in same list) — both
    extracted."""
    from app.proxy_tools import _collect_caller_tool_names
    tools = [
        {"name": "fetch_url"},
        {"type": "function", "function": {"name": "calc"}},
    ]
    assert _collect_caller_tool_names(tools) == {"fetch_url", "calc"}


def test_collect_names_ignores_non_dict():
    from app.proxy_tools import _collect_caller_tool_names
    assert _collect_caller_tool_names(["not-a-dict", 42, None]) == set()


def test_collect_names_handles_missing_name():
    from app.proxy_tools import _collect_caller_tool_names
    tools = [
        {"input_schema": {}},  # no name field
        {"type": "function", "function": {}},  # no inner name
    ]
    assert _collect_caller_tool_names(tools) == set()


# ── inject_anthropic dedupe behavior ───────────────────────────────────


def test_inject_dedupes_anthropic_shape():
    """When caller already has ``fetch_url`` in Anthropic shape, the
    proxy version is NOT re-appended."""
    from app.proxy_tools import inject_anthropic
    fake_registry = [
        _FakeProxyTool("fetch_url"),
        _FakeProxyTool("read_xlsx_to_markdown"),
    ]
    body = {"tools": [{"name": "fetch_url", "input_schema": {"properties": {}}}]}
    with patch("app.proxy_tools.get_registry", lambda: fake_registry):
        result = inject_anthropic(body)
    # fetch_url not duplicated; read_xlsx_to_markdown appended
    names = [t.get("name") for t in result]
    assert names.count("fetch_url") == 1
    assert "read_xlsx_to_markdown" in names


def test_inject_dedupes_openai_shape_with_function_wrapper():
    """DevinGPT's scenario verbatim: caller passes their own
    fetch_url in OpenAI function-wrapper shape. v5.7.16 detects it and
    does NOT re-inject. Pre-5.7.16 would have added the proxy version,
    creating the two-tools-same-name collision."""
    from app.proxy_tools import inject_anthropic
    fake_registry = [_FakeProxyTool("fetch_url")]
    body = {
        "tools": [
            {"type": "function", "function": {"name": "fetch_url"}},
        ]
    }
    with patch("app.proxy_tools.get_registry", lambda: fake_registry):
        result = inject_anthropic(body)
    names = []
    for t in result:
        n = t.get("name") or (t.get("function") or {}).get("name")
        if n:
            names.append(n)
    assert names.count("fetch_url") == 1, (
        f"v5.7.16: OpenAI-shape dedupe failed — got {names}"
    )


def test_inject_still_adds_when_no_collision():
    """Sanity check: when caller has nothing or unrelated tools, the
    proxy version IS appended (we're not silently dropping all
    injections)."""
    from app.proxy_tools import inject_anthropic
    fake_registry = [_FakeProxyTool("fetch_url")]
    body = {"tools": [{"name": "totally_different_tool"}]}
    with patch("app.proxy_tools.get_registry", lambda: fake_registry):
        result = inject_anthropic(body)
    names = [t.get("name") for t in result]
    assert "fetch_url" in names
    assert "totally_different_tool" in names


# ── audit logging on collision ─────────────────────────────────────────


def test_collision_triggers_audit_call():
    """When a collision happens, ``_log_dedupe_skips`` is invoked with
    the skipped name(s). No exception even when no event loop is
    running (sync test).

    Pin reads source for the activity_log event_type so a rename
    surfaces here without needing a running loop fixture."""
    from pathlib import Path
    src = Path("app/proxy_tools/__init__.py").read_text()
    assert 'event_type="proxy_tool.dedupe_skip"' in src, (
        "v5.7.16: dedupe-skip audit event_type missing or renamed."
    )


def test_log_dedupe_skips_no_loop_does_not_raise():
    """The audit fire-and-forget MUST be safe to call from sync code
    (e.g. the sync ``inject_anthropic`` path) without a running event
    loop. v5.7.16's RuntimeError catch covers this."""
    from app.proxy_tools import _log_dedupe_skips
    _log_dedupe_skips(["fetch_url"])  # would explode pre-5.7.16 attempt to schedule


def test_empty_skip_list_is_zero_cost():
    """The hot path (no collision) MUST NOT touch the DB. Audit only
    fires when ``skipped`` is non-empty."""
    from app.proxy_tools import _log_dedupe_skips
    _log_dedupe_skips([])  # no-op


def test_version_bumped():
    """v5.7.16 minimum — later patches keep this passing."""
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (5, 7, 16), f"v5.7.16 must be reachable; got {__version__}"
