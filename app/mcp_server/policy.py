"""v5.7.4 — Per-key MCP tool policy enforcement.

When a bot calls /mcp/list_tools or /mcp/call_tool, the policy module
filters the visible tool set according to the bearer-key's
``mcp_tools_allow`` / ``mcp_tools_deny`` lists, and rejects requests
whose total tool-schema-token estimate would exceed the per-key
``mcp_schema_token_budget``.

Wildcard syntax: standard ``fnmatch`` globs. ``read_*`` matches
``read_xlsx_to_markdown`` and ``read_pdf``; ``*_billing_*`` matches
``anthropic_billing_scrape`` etc.

Semantics:
- allow list NULL → all tools allowed.
- allow list ``[]`` → NO tools allowed (effectively disables MCP).
- deny list takes precedence: a tool matched by both allow AND deny
  is denied.
- token budget NULL → unlimited; otherwise the proxy returns
  ``X-Token-Budget-Exceeded`` and a 400 response on list_tools when
  the cumulative ``len(json.dumps(tool.input_schema))`` exceeds the
  budget. Token approximation: 1 token ≈ 4 characters of JSON
  (Anthropic claim for JSON schemas; close enough for budget gating).
"""
from __future__ import annotations

import fnmatch
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


# Approximation used to convert JSON schema bytes → tokens for the
# budget gate. Anthropic's docs claim ~1 token per 4 characters for
# JSON-formatted text; OpenAI tiktoken comes out a bit lower. 4 is
# the conservative upper bound — we'd rather err on the side of
# falsely flagging "over budget" than silently letting a 50K-token
# tool list through.
_CHARS_PER_TOKEN = 4.0


def _matches_any(name: str, globs: list[str] | None) -> bool:
    """True if any glob in ``globs`` fnmatches ``name``. NULL list
    means no match (caller decides whether NULL means 'no filter')."""
    if not globs:
        return False
    return any(fnmatch.fnmatchcase(name, g) for g in globs)


def is_tool_allowed_for_key(
    tool_name: str,
    mcp_tools_allow: list[str] | None,
    mcp_tools_deny: list[str] | None,
) -> bool:
    """Single-tool ACL check. Deny wins.

    - allow=NULL → all allowed (unless denied)
    - allow=[]   → no tools allowed
    - allow=[…]  → only listed tools allowed (unless denied)
    """
    # Deny wins
    if _matches_any(tool_name, mcp_tools_deny):
        return False
    # Allow=NULL → permissive
    if mcp_tools_allow is None:
        return True
    # Allow=[] → restrictive (empty list explicitly denies all)
    if not mcp_tools_allow:
        return False
    return _matches_any(tool_name, mcp_tools_allow)


def estimate_schema_tokens(input_schema: dict | None) -> int:
    """Conservative token estimate for a tool's JSON Schema. Uses 4
    chars / token. Always returns >= 0."""
    if not input_schema:
        return 0
    try:
        s = json.dumps(input_schema, separators=(",", ":"))
    except Exception:
        return 0
    return max(0, int(len(s) / _CHARS_PER_TOKEN))


def filter_tools_for_key(
    tools: list[Any],
    mcp_tools_allow: list[str] | None,
    mcp_tools_deny: list[str] | None,
) -> list[Any]:
    """Return the subset of ``tools`` allowed by the key's policy.

    ``tools`` is a list of MCP Tool objects (or any object with a
    ``.name`` attribute). The returned list preserves input order.
    """
    return [
        t for t in tools
        if is_tool_allowed_for_key(
            getattr(t, "name", "") or "",
            mcp_tools_allow,
            mcp_tools_deny,
        )
    ]


def check_token_budget(
    tools: list[Any],
    mcp_schema_token_budget: int | None,
) -> tuple[bool, int]:
    """Returns ``(ok, total_tokens)``. ``ok`` is False if a budget is
    set and the estimated total exceeds it. ``total_tokens`` is the
    sum across the supplied tool list."""
    total = sum(
        estimate_schema_tokens(getattr(t, "inputSchema", None))
        for t in tools
    )
    if mcp_schema_token_budget is None:
        return True, total
    return total <= int(mcp_schema_token_budget), total
