"""Auto-cache injection for Anthropic-shape providers (v3.0.42).

24h activity-log audit on v3.0.39 found cache_control adoption at exactly
0% across 16k events — including 3,005 Anthropic Pro Max OAuth calls with
50–80k-token contexts. Coordinator-hub bot daemons send the same large
system prompt repeatedly without `cache_control` blocks because their
callers don't know about Anthropic's prompt caching feature.

Anthropic's prompt-cache scoring is forgiving: cache_control is a no-op
below threshold (~1024 tok Sonnet, ~2048 Haiku, ~4096 Opus) and a major
cost reduction above it. Adding cache_control opportunistically when the
caller didn't is safe — small prompts skip cache silently, large ones
benefit. Verified in #147 (closed 2026-05-01) that Pro Max OAuth tier
DOES return correct cache_creation_input_tokens / cache_read_input_tokens
when above threshold.

Default: ON for Anthropic-shape providers (anthropic, anthropic-direct,
anthropic-oauth, claude-oauth). Caller can opt out with the LMRH dim
``cache=none`` — see app/routing/lmrh/score.py for dim handling.

Scope of injection:
  - Wraps the LAST block of ``system`` (if array) or the whole system
    (if string) with ``cache_control: {type: "ephemeral"}``.
  - Wraps the LAST tool definition's input_schema if tools[] is large.
  - Only when no caller-supplied cache_control is already present
    (don't double-wrap).
  - Only on Anthropic-shape providers; no-op everywhere else.

Estimated savings on current volume (per the v3.0.39 audit):
  3,005 claude-oauth events × ~50k avg input × $3/M input × 90% cache
  discount × ~50% cache-hit rate ≈ $200/day saved.
"""
from __future__ import annotations

from typing import Any


# Provider types that speak Anthropic Messages format and honor cache_control.
_ANTHROPIC_SHAPE_TYPES = frozenset({
    "anthropic", "anthropic-direct", "anthropic-oauth", "claude-oauth",
})


def _has_cache_control(blk: Any) -> bool:
    """Recursively check if a block already carries cache_control."""
    if isinstance(blk, dict):
        if "cache_control" in blk:
            return True
        # Nested in tool_result content arrays etc.
        c = blk.get("content")
        if isinstance(c, list):
            return any(_has_cache_control(x) for x in c)
    return False


def _approx_tokens(s: str) -> int:
    """Rough char→token estimate (4 chars ≈ 1 token for English/code)."""
    return len(s) // 4 if s else 0


def _string_to_text_block(s: str) -> dict:
    return {"type": "text", "text": s}


def inject_cache_control(
    body: dict, provider_type: str, min_chars: int = 4000,
) -> tuple[dict, bool]:
    """Auto-wrap stable prefix blocks with cache_control: ephemeral.

    Args:
        body: Anthropic Messages request body (mutated by returning a shallow copy)
        provider_type: e.g. ``claude-oauth``, ``anthropic``
        min_chars: byte threshold below which we don't bother (≈1000 tokens).
                   Anthropic's caching threshold is ~1024 tokens so anything
                   smaller is a guaranteed cache miss. Pass ``0`` to force
                   wrap regardless of size — used for ``cache=ephemeral``
                   per LMRH 1.2 §E2 to honor caller-explicit opt-in even
                   on small prompts (where the cache_control is a no-op
                   upstream but the dim is still honored).

    Returns:
        Tuple of ``(body, injected)`` — modified body (original unchanged)
        plus a boolean indicating whether wrapping actually happened.
        v3.0.83: the boolean is needed for LMRH 1.2 §E2 capability-header
        disclosure (``cache-injected=?1``); existing callers should now
        unpack the tuple.
    """
    if provider_type not in _ANTHROPIC_SHAPE_TYPES:
        return body, False
    if not isinstance(body, dict):
        return body, False

    out = {**body}
    injected = False

    # ── System prompt ────────────────────────────────────────────────────
    sys_field = out.get("system")
    if isinstance(sys_field, str):
        if len(sys_field) >= min_chars:
            # Convert to array form so we can attach cache_control
            out["system"] = [{
                "type": "text",
                "text": sys_field,
                "cache_control": {"type": "ephemeral"},
            }]
            injected = True
    elif isinstance(sys_field, list) and sys_field:
        # Skip if any block already has cache_control — caller knows better
        if not any(_has_cache_control(b) for b in sys_field):
            total_chars = sum(
                len(b.get("text", "")) if isinstance(b, dict) and b.get("type") == "text" else 0
                for b in sys_field
            )
            if total_chars >= min_chars:
                # Wrap the LAST text block in place. Mutate a copy so we
                # don't surprise the caller.
                new_sys = list(sys_field)
                for i in range(len(new_sys) - 1, -1, -1):
                    blk = new_sys[i]
                    if isinstance(blk, dict) and blk.get("type") == "text":
                        new_sys[i] = {**blk, "cache_control": {"type": "ephemeral"}}
                        injected = True
                        break
                out["system"] = new_sys

    # ── Tools ────────────────────────────────────────────────────────────
    tools = out.get("tools")
    if isinstance(tools, list) and tools:
        if not any(_has_cache_control(t) for t in tools):
            # Tool-definition caching threshold is roughly the same — only
            # bother if total tool spec is large.
            import json as _json
            try:
                total_chars = sum(len(_json.dumps(t)) for t in tools if isinstance(t, dict))
            except (TypeError, ValueError):
                total_chars = 0
            if total_chars >= min_chars:
                new_tools = list(tools)
                last = new_tools[-1]
                if isinstance(last, dict):
                    new_tools[-1] = {**last, "cache_control": {"type": "ephemeral"}}
                    out["tools"] = new_tools
                    injected = True

    return out, injected


def caller_opted_out(lmrh_hint: str | None) -> bool:
    """Backward-compat shim — prefer ``parse_cache_mode`` directly.

    True when caller explicitly asked us NOT to inject cache_control via
    ``cache=none|off|disabled``. Any other value (cache=auto, cache=ephemeral,
    missing entirely) → False → we inject by default per the auto-mode
    threshold heuristic.
    """
    return parse_cache_mode(lmrh_hint).mode == "none"


# v3.0.69: full LMRH 1.2 §E2 cache-mode dim. Spec at
# docs/lmrh-1.2-cache-mode-dim.md. Replaces the v3.0.42 substring-only check.

class CacheModeDecision:
    """Resolved cache-mode intent from LLM-Hint.

    Attributes:
        mode: ``"auto"`` (default + threshold), ``"ephemeral"`` (force inject
              regardless of size), or ``"none"`` (suppress proxy injection).
        required: True if caller used ``;require`` modifier.
        force_below_threshold: True when mode is ``ephemeral`` — inject
              even on small prompts (no-op upstream below cache threshold,
              but the dim was honored end-to-end).
    """

    __slots__ = ("mode", "required", "force_below_threshold")

    def __init__(self, mode: str = "auto", required: bool = False) -> None:
        self.mode = mode
        self.required = required
        self.force_below_threshold = (mode == "ephemeral")


# Synonyms per LMRH 1.2 §E2 table. Anything outside this set falls back to
# "auto" (the proxy's default heuristic) — surfaced as
# X-LMRH-Warnings: unknown-dim-value:cache=<v> when wired in Phase 2.
_CACHE_MODE_SYNONYMS = {
    "auto": "auto",
    "ephemeral": "ephemeral",
    "none": "none",
    "off": "none",
    "disabled": "none",
    # "persistent" is reserved (per spec) — Anthropic doesn't ship it. Fall
    # through to auto so callers proposing the dim early get sane behavior.
    "persistent": "auto",
}


def parse_cache_mode(lmrh_hint: str | None) -> CacheModeDecision:
    """Parse LLM-Hint for the LMRH 1.2 §E2 ``cache=`` dim.

    Uses the same legacy comma-tolerant parser as the rest of the LMRH
    surface (v3.0.68 fix), so multi-value composite hints like
    ``provider-hint=a,b, cache=ephemeral;require`` resolve correctly.

    Returns ``CacheModeDecision(mode="auto", required=False)`` when the dim
    is absent or unparseable — the same default as pre-v3.0.69 behavior.
    """
    if not lmrh_hint:
        return CacheModeDecision()
    try:
        from app.routing.lmrh.parse import parse_hint
    except ImportError:
        return CacheModeDecision()
    parsed = parse_hint(lmrh_hint)
    if parsed is None:
        return CacheModeDecision()
    dim = parsed.get("cache")
    if dim is None:
        return CacheModeDecision()
    raw = (dim.value or "").strip().lower()
    mode = _CACHE_MODE_SYNONYMS.get(raw, "auto")
    return CacheModeDecision(mode=mode, required=bool(dim.required))


def resolve_min_chars(decision: CacheModeDecision, default: int = 4000) -> int:
    """Pick the right ``min_chars`` for the auto-inject threshold based on
    caller intent. ``cache=ephemeral`` → 0 (always wrap). Otherwise the
    operator-supplied default."""
    return 0 if decision.force_below_threshold else default
