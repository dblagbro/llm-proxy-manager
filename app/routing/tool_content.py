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
