"""Model-slug shortcut parser (v2.8.0 — OpenRouter parity).

Clients can append a sort-mode shortcut to the requested model name to
override default routing without sending an LMRH header:

    claude-sonnet-4-6:floor   → cheapest provider/model that satisfies hints
    claude-sonnet-4-6:nitro   → highest-throughput (lowest TTFT) provider
    claude-sonnet-4-6:exacto  → quality-first (default capability score)

The bare model name (no suffix) keeps the default behavior — LMRH-driven
ranking with PeakEWMA tie-break inside the top tier.

The suffix is stripped before forwarding to the upstream — Anthropic /
OpenAI / Google etc. don't know about :floor / :nitro and would 4xx.

Inspired by OpenRouter's `:nitro` / `:floor` / `:exacto` variant suffixes
(documented at https://openrouter.ai/docs/guides/routing/model-variants),
adapted to operate over our existing capability profile + circuit breaker
+ PeakEWMA telemetry instead of OpenRouter's provider-level fan-out.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

SortMode = Literal["floor", "nitro", "exacto"]
_SHORTCUT_SUFFIXES: tuple[SortMode, ...] = ("floor", "nitro", "exacto")

# v2.8.0 auto-routing alias. When the client sends ``model: "auto"`` (or
# ``"llmp-auto"``) we run the existing LMRH-driven select_provider WITHOUT
# a model_override, then substitute the chosen provider's default_model
# (or capability profile model_id) into the upstream body. Composes with
# slug shortcuts: ``auto:floor`` → cheapest auto-routed model.
AUTO_MODEL_ALIASES: frozenset[str] = frozenset({"auto", "llmp-auto"})


def is_auto_model(model: Optional[str]) -> bool:
    """True if the requested model name is one of the auto-routing aliases.
    The slug must already have been parsed (suffix stripped) before calling."""
    return bool(model) and model.lower() in AUTO_MODEL_ALIASES


@dataclass(frozen=True)
class ParsedSlug:
    bare_model: str
    sort_mode: Optional[SortMode]


def parse_model_slug(model: Optional[str]) -> ParsedSlug:
    """Strip a recognized ``:floor`` / ``:nitro`` / ``:exacto`` suffix.

    Unrecognized suffixes (or no suffix at all) pass through unchanged.
    Examples:
        parse_model_slug("claude-sonnet-4-6:floor") → ("claude-sonnet-4-6", "floor")
        parse_model_slug("gpt-4o")                  → ("gpt-4o", None)
        parse_model_slug("anthropic/claude-3:foo")  → ("anthropic/claude-3:foo", None)
        parse_model_slug(None)                      → ("", None)
        parse_model_slug("auto:floor")              → ("auto", "floor")  -- composes
    """
    if not model:
        return ParsedSlug(bare_model="", sort_mode=None)
    if ":" not in model:
        return ParsedSlug(bare_model=model, sort_mode=None)
    head, _, tail = model.rpartition(":")
    tail_lower = tail.lower()
    if tail_lower in _SHORTCUT_SUFFIXES:
        return ParsedSlug(bare_model=head, sort_mode=tail_lower)  # type: ignore[arg-type]
    # Unrecognized — preserve the whole string (some upstreams use ":" in
    # model IDs, e.g. ``anthropic/claude-3-opus:beta`` on litellm).
    return ParsedSlug(bare_model=model, sort_mode=None)
