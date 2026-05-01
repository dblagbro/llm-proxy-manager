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


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass
class HintDimension:
    key: str
    value: str
    required: bool = False  # ``;require`` parameter


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
    regions: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    native_reasoning: bool = False
    native_tools: bool = True
    native_vision: bool = False
    priority: int = 10
    avg_ttft_ms: float = 0.0
    over_daily_budget: bool = False
