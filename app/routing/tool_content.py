"""v3.9.1 (#269 Fix A) — detect Anthropic-shape tool content blocks.

Used by ``/v1/messages`` to short-circuit cross-family fallback when
messages contain ``tool_use`` or ``tool_result`` blocks. The proxy
doesn't yet translate those into OpenAI's ``tool_calls`` + ``role:tool``
shape (see #269 Fix B for the translator), so sending an Anthropic-shaped
tool conversation to an OpenAI-compatible upstream produces a guaranteed
400 ("Invalid user message at index N"). Better to 503 cleanly than burn
upstream cost on broken requests.
"""
from __future__ import annotations

# Anthropic content-block types that signal a tool-using conversation.
_TOOL_BLOCK_TYPES = ("tool_use", "tool_result")


def has_anthropic_tool_content(messages: list[dict]) -> bool:
    """True iff any message has a list-of-content-blocks with at least
    one ``tool_use`` or ``tool_result`` block. Plain-text content
    (string) returns False — those convert cleanly cross-family.
    """
    for m in messages or ():
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in _TOOL_BLOCK_TYPES:
                return True
    return False


def has_anthropic_tool_defs(tools: list[dict] | None) -> bool:
    """True iff the request-level ``tools`` list contains at least one
    Anthropic-shape tool definition.

    BUG-047 (v4.3.8): the cross-family translation gate in messages.py
    pre-fix only fired when *message blocks* were Anthropic-shape
    (``has_anthropic_tool_content``). A first-turn request with
    Anthropic-shape tool DEFINITIONS (``{name, description,
    input_schema}``) but no tool-use blocks YET would fall through
    untranslated and 400 on non-anthropic upstreams with
    ``missing required field: 'type'``. This helper closes that gap.

    Anthropic shape:  ``{"name": str, "description": str,
                          "input_schema": {...}}``  — has ``input_schema``
    OpenAI shape:     ``{"type": "function", "function": {"name": ...}}``
                       — has ``type == 'function'`` AND nested ``function``

    A tool is treated as Anthropic-shape if it has ``input_schema`` OR
    lacks both ``type == 'function'`` and the nested ``function`` key.
    """
    if not tools:
        return False
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "input_schema" in t:
            return True
        # Lacks the OpenAI shell — treat as Anthropic by elimination
        if t.get("type") != "function" or not isinstance(t.get("function"), dict):
            return True
    return False
