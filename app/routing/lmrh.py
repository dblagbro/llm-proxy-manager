"""
LMRH — LLM Model Routing Hint Protocol
Reference implementation of draft-blagbrough-lmrh-00.

Parses the LLM-Hint request header (RFC 8941 Structured Fields subset),
scores provider+model pairs against the hint dimensions, enforces hard
constraints, and returns an ordered candidate list.

Response header: LLM-Capability: v=1, provider=..., model=..., ...
"""
import re
from dataclasses import dataclass, field
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# ── Dimension weights (higher = more influence on score) ─────────────────────
WEIGHTS = {
    "task": 10,
    "safety-min": 8,
    "safety-max": 8,
    "modality": 5,
    "region": 6,
    "latency": 4,
    "cost": 3,
    "context-length": 2,
}

TASK_ALIASES = {
    "chat": ["chat"],
    "reasoning": ["reasoning", "analysis", "code"],
    "analysis": ["analysis", "reasoning"],
    "code": ["code", "reasoning"],
    "creative": ["creative", "chat"],
    "audio": ["audio"],
    "vision": ["vision"],
}

LATENCY_RANK = {"low": 0, "medium": 1, "high": 2}
COST_RANK = {"economy": 0, "standard": 1, "premium": 2}


@dataclass
class HintDimension:
    key: str
    value: str
    required: bool = False  # ;require suffix


@dataclass
class LMRHHint:
    raw: str
    dimensions: list[HintDimension] = field(default_factory=list)

    def get(self, key: str) -> Optional[HintDimension]:
        for d in self.dimensions:
            if d.key == key:
                return d
        return None


def parse_hint(header_value: str) -> Optional[LMRHHint]:
    """Parse LLM-Hint header value into structured dimensions."""
    if not header_value:
        return None
    hint = LMRHHint(raw=header_value)
    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue
        required = ";require" in part
        part = part.replace(";require", "").strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        hint.dimensions.append(HintDimension(key.strip(), value.strip(), required))
    return hint if hint.dimensions else None


@dataclass
class CapabilityProfile:
    """Capability profile for a provider+model pair (from DB or inferred)."""
    provider_id: str
    provider_type: str
    model_id: str
    tasks: list[str] = field(default_factory=lambda: ["chat"])
    latency: str = "medium"
    cost_tier: str = "standard"
    safety: int = 3
    context_length: int = 128000
    regions: list[str] = field(default_factory=list)
    modalities: list[str] = field(default_factory=lambda: ["text"])
    native_reasoning: bool = False
    priority: int = 10


def score_candidate(profile: CapabilityProfile, hint: LMRHHint) -> tuple[float, list[str]]:
    """
    Score a candidate profile against a hint.
    Returns (score, unmet_soft_dims).
    Returns (-inf, [...]) if any hard constraint fails.
    """
    score = 0.0
    unmet: list[str] = []

    for dim in hint.dimensions:
        match dim.key:
            case "task":
                compatible = TASK_ALIASES.get(dim.value, [dim.value])
                if any(t in profile.tasks for t in compatible):
                    score += WEIGHTS["task"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "safety-min":
                min_req = int(dim.value)
                if profile.safety >= min_req:
                    score += WEIGHTS["safety-min"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "safety-max":
                max_req = int(dim.value)
                if profile.safety <= max_req:
                    score += WEIGHTS["safety-max"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "latency":
                desired = LATENCY_RANK.get(dim.value, 1)
                actual = LATENCY_RANK.get(profile.latency, 1)
                if actual <= desired:
                    score += WEIGHTS["latency"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "cost":
                desired = COST_RANK.get(dim.value, 1)
                actual = COST_RANK.get(profile.cost_tier, 1)
                if actual <= desired:
                    score += WEIGHTS["cost"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "region":
                if not profile.regions or dim.value in profile.regions:
                    score += WEIGHTS["region"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "context-length":
                required_ctx = int(dim.value)
                if profile.context_length >= required_ctx:
                    score += WEIGHTS["context-length"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "modality":
                if dim.value in profile.modalities or "multimodal" in profile.modalities:
                    score += WEIGHTS["modality"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

    # Base score from provider priority (lower priority number = higher score)
    score += max(0, 100 - profile.priority)

    return score, unmet


def rank_candidates(
    profiles: list[CapabilityProfile], hint: Optional[LMRHHint]
) -> list[tuple[CapabilityProfile, list[str]]]:
    """
    Return profiles sorted by descending score.
    Hard-constraint failures are excluded entirely.
    Without a hint, sorts by priority only.
    """
    if not hint:
        return [(p, []) for p in sorted(profiles, key=lambda p: p.priority)]

    scored = []
    for p in profiles:
        s, unmet = score_candidate(p, hint)
        if s == float("-inf"):
            continue
        scored.append((p, unmet, s))

    scored.sort(key=lambda x: -x[2])
    return [(p, unmet) for p, unmet, _ in scored]


def build_capability_header(
    profile: CapabilityProfile,
    unmet: list[str],
    cot_engaged: bool = False,
) -> str:
    parts = [
        "v=1",
        f"provider={profile.provider_id}",
        f"model={profile.model_id}",
        f"task={','.join(profile.tasks)}",
        f"safety={profile.safety}",
        f"latency={profile.latency}",
        f"cost={profile.cost_tier}",
        f"region={','.join(profile.regions) if profile.regions else 'any'}",
    ]
    if unmet:
        parts.append(f"unmet={' '.join(unmet)}")
    if cot_engaged:
        parts.append("cot-engaged=?1")
    return ", ".join(parts)


def infer_capability_profile(provider_id: str, provider_type: str, model_id: str, priority: int = 10) -> CapabilityProfile:
    """Infer capability profile from model name heuristics."""
    m = model_id.lower()
    profile = CapabilityProfile(
        provider_id=provider_id,
        provider_type=provider_type,
        model_id=model_id,
        priority=priority,
    )

    # Reasoning / native thinking detection
    if any(x in m for x in ["opus", "o1", "o3", "o4", "r1", "deepseek-r", "gemini-2.5", "claude-3-7"]):
        profile.native_reasoning = True
        profile.tasks = ["reasoning", "analysis", "code", "chat"]
        profile.cost_tier = "premium"
        profile.latency = "high"
        profile.safety = 4

    elif any(x in m for x in ["sonnet", "gpt-4o", "gemini-2.0", "gpt-4-turbo", "grok-2"]):
        profile.tasks = ["reasoning", "analysis", "code", "chat"]
        profile.cost_tier = "standard"
        profile.latency = "medium"
        profile.safety = 4

    elif any(x in m for x in ["haiku", "flash", "mini", "gpt-3.5", "grok-beta"]):
        profile.tasks = ["chat", "analysis"]
        profile.cost_tier = "economy"
        profile.latency = "low"
        profile.safety = 3

    # Vision
    if any(x in m for x in ["vision", "vl", "gpt-4o", "gemini", "claude-3", "llava"]):
        profile.modalities = ["text", "vision"]

    # Provider-specific region defaults
    if provider_type == "google":
        profile.regions = ["us", "eu", "asia"]
    elif provider_type in ("anthropic", "openai", "grok"):
        profile.regions = ["us"]
    elif provider_type == "ollama":
        profile.regions = ["local"]
        profile.cost_tier = "economy"
        profile.latency = "medium"

    return profile
