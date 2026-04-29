"""Tool-spec translation for the Run runtime (R3).

The Run create payload uses Anthropic Messages tools format (MCP-compatible).
Different upstreams want different shapes:

  - Anthropic native:  the input shape, no conversion needed
  - OpenAI / litellm:  ``{"type": "function", "function": {"name", "description",
                          "parameters"}}``
  - Gemini / Vertex:   the OpenAI shape via litellm's adapter
  - PBTC emulation:    inject ``<tool_call>{name,input}</tool_call>`` system
                          prompt; reuse app/cot/tool_emulation.py

This module adapts ``run.tools_spec`` for whichever litellm route the
worker just picked. Per-provider ``native_tools`` capability decides
native-vs-emulation.
"""
from __future__ import annotations

from typing import Optional


def to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Anthropic ``{name, description, input_schema}`` →
    OpenAI ``{type: function, function: {name, description, parameters}}``."""
    out = []
    for t in anthropic_tools or []:
        if not isinstance(t, dict):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object"}),
            },
        })
    return out


def adapt_tools_for_route(
    anthropic_tools: list[dict],
    *,
    litellm_model: str,
    native_tools: bool,
) -> tuple[Optional[list[dict]], Optional[str]]:
    """Pick the right wire shape for this provider.

    Returns ``(tools_arg, emulation_system_prompt)``:
      - ``tools_arg`` is what to pass as ``kwargs['tools']`` to litellm
        (None if we're emulating via system-prompt)
      - ``emulation_system_prompt`` is non-None when the provider lacks
        native tool-use; the caller prepends it to the conversation.
    """
    if not anthropic_tools:
        return None, None

    if not native_tools:
        # Provider has no native tool_use; fall back to PBTC emulation.
        from app.cot.tool_emulation import build_anthropic_tool_prompt
        return None, build_anthropic_tool_prompt(anthropic_tools, allow_parallel=True)

    # litellm normalises anthropic/* model ids back to native Anthropic format
    # if the destination is Anthropic; for openai/* it expects OpenAI shape.
    # Heuristic on the model prefix is sufficient — operators tag their
    # provider rows with the right ``litellm_model`` already.
    if litellm_model.startswith("anthropic/") or litellm_model.startswith("claude-"):
        return anthropic_tools, None
    return to_openai_tools(anthropic_tools), None
