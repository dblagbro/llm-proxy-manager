"""LMRH type system: dataclasses + lookup tables + weights.

Split out from the monolithic ``routing/lmrh.py`` in the 2026-04-23
refactor. Consumers import from the package (``from app.routing.lmrh
import ...``) which re-exports everything here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Dimension weights (higher = more influence on score) ─────────────────────

WEIGHTS: dict[str, int] = {
    "task": 10,
    "safety-min": 8,
    "safety-max": 8,
    "modality": 5,
    "region": 6,
    "latency": 4,
    "cost": 3,
    "context-length": 2,
    # v3.0.25 — provider-hint = positive selection bias (soft);
    # exclude = negative selection bias (soft). Both go hard with ;require.
    "provider-hint": 5,
    "exclude": 5,
    # v5.21.0 — per-request refuse tolerance (strict/default/lenient).
    # Weight matches ``safety-max`` (8) — it's expressing the same axis.
    "refuse-tolerance": 8,
}

TASK_ALIASES: dict[str, list[str]] = {
    "chat": ["chat"],
    "reasoning": ["reasoning", "analysis", "code"],
    "analysis": ["analysis", "reasoning"],
    "code": ["code", "reasoning"],
    "creative": ["creative", "chat"],
    "audio": ["audio"],
    "vision": ["vision"],
}

LATENCY_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
COST_RANK: dict[str, int] = {"economy": 0, "standard": 1, "premium": 2}

# Wave 4 #21 — refusal-rate human-readable alias maps to safety integer scale.
#   permissive → prefer low safety (willing to answer anything reasonable)
#   maximum    → prefer high safety (strict refusals)
_REFUSAL_RATE_TO_SAFETY_CEIL: dict[str, int] = {
    "permissive": 2, "standard": 3, "strict": 4, "maximum": 5,
}
_REFUSAL_RATE_TO_SAFETY_FLOOR: dict[str, int] = {
    "permissive": 1, "standard": 2, "strict": 3, "maximum": 4,
}

# v5.21.0 — refuse-tolerance dim: per-request routing hint for how
# strict/lenient the CALLER wants the model to be for THIS request.
# Complements the existing per-provider ``refusal-rate`` (which
# describes the provider's baseline behavior).
#
# Semantics (from DevinGPT team's named use cases):
#   strict   — creative-writing / policy-sensitive contexts; caller
#              wants a model that WILL refuse edgy content
#   default  — no opinion; leave routing alone
#   lenient  — automation / tool-firing contexts; caller wants a model
#              LESS likely to refuse legitimate operational calls
#              (fewer "I can't do that" for edge cases the caller
#              is authorized for)
#
# Wire mapping mirrors refusal-rate but with a coarser 3-way scale
# because per-request routing hints are typed by humans and simpler
# vocab wins:
#
#   strict   → prefers safety >= 4 (same as refusal-rate=strict)
#   default  → prefers safety in [2, 4] range (broad middle)
#   lenient  → prefers safety <= 2 (same as refusal-rate=permissive)
#
# Taxonomy is v1 — after the 2026-07-12 rollup, we may add
# ``ambivalent`` / ``domain-specific`` if the data warrants it.
_REFUSE_TOLERANCE_TO_SAFETY_FLOOR: dict[str, int] = {
    "strict":  4,
    "default": 2,
    "lenient": 1,
}
_REFUSE_TOLERANCE_TO_SAFETY_CEIL: dict[str, int] = {
    "strict":  5,
    "default": 4,
    "lenient": 2,
}


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class HintDimension:
    key: str
    value: str
    required: bool = False  # ``;require`` parameter
    # v3.0.52 (LMRH 1.2 §E3): ``;sovereign`` strengthens ``;require`` for
    # the region dim. Sovereign rejects providers with unconfigured regions
    # (uncertainty = reject) and providers known to fail over across
    # borders. Implies required=True. Currently meaningful only for
    # ``region=`` hints; other dims accept the param but treat it the
    # same as ``;require``.
    sovereign: bool = False


@dataclass
class LMRHHint:
    raw: str
    dimensions: list[HintDimension] = field(default_factory=list)

    def get(self, key: str) -> Optional[HintDimension]:
        for d in self.dimensions:
            if d.key == key:
                return d
        return None


@dataclass
class CapabilityProfile:
    """Capability profile for a provider+model pair (from DB or inferred)."""
    provider_id: str
    provider_type: str
    model_id: str
    # v3.0.25: surface the provider's display name on the profile so the
    # LMRH scorer can match exclude= / provider-hint= dims against either
    # provider_type ("anthropic") or display name ("Devin-Cohere").
    provider_name: str = ""
    tasks: list[str] = field(default_factory=lambda: ["chat"])
    latency: str = "medium"
    cost_tier: str = "standard"
    safety: int = 3
    context_length: int = 128000
    # v5.22.13 — the model's maximum OUTPUT tokens, from model_pricing_catalog.
    # ``None`` = unknown (catalog covers ~90% of models) and MUST be treated as
    # "do not filter"; excluding providers on missing data would be worse than
    # the occasional upstream rejection this guards against. Distinct from
    # ``context_length``, which bounds INPUT.
    max_output_tokens: int | None = None
    regions: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    native_reasoning: bool = False
    native_tools: bool = True
    native_vision: bool = False
    priority: int = 10
    avg_ttft_ms: float = 0.0
    over_daily_budget: bool = False
    # v3.8.5 (#265) — rolling tool-call success rate from the v3.8.4
    # prober (0.0-1.0). When the request has tools=[] AND this value
    # is non-None, the router penalizes low-success-rate candidates.
    # None means "no probe data yet" — router falls back to the binary
    # native_tools flag.
    tool_call_success_rate: float | None = None
