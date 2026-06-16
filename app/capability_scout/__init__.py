"""v5.7.6 — Capability scout: refusal-pattern detector.

Surfaces the original operator MCP vision: "AI that monitors traffic,
if it detects traffic could benefit from MCP features, it would add
them". v5.7.6 ships the detector half — every non-streaming
``/v1/messages`` response is scanned for refusal patterns ("I can't
read", "I don't have access to", …) and a structured
``mcp_capability_suggestion`` activity_log row is emitted with the
suggested MCP tool. Operator reviews suggestions in the MCP dashboard
and flips per-key opt-ins (system_prompt_mcp_augmentation +
allow-list) accordingly.

Off by default — set ``capability_scout.enabled = true`` in
system_settings to turn on. Privacy-respecting: only the matched
phrase + 80-char context window is stored; the full response is never
copied into activity_log.

Streaming path is covered by v5.6.1 (separate ship).
"""

from app.capability_scout.scout import (
    is_enabled,
    scan_response_text,
    emit_suggestions,
    REFUSAL_PATTERNS,
)

__all__ = [
    "is_enabled",
    "scan_response_text",
    "emit_suggestions",
    "REFUSAL_PATTERNS",
]
