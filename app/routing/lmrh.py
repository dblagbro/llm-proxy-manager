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

# Wave 4 #21 — refusal-rate human-readable alias maps to safety integer scale.
# Higher safety integer = more willing to refuse, so:
#   permissive → prefer low safety (willing to answer anything reasonable)
#   maximum    → prefer high safety (strict refusals)
_REFUSAL_RATE_TO_SAFETY_CEIL = {
    "permissive": 2,   # profile.safety ≤ 2
    "standard":   3,   # ≤ 3
    "strict":     4,   # ≤ 4
    "maximum":    5,   # ≤ 5 (any)
}
_REFUSAL_RATE_TO_SAFETY_FLOOR = {
    "permissive": 1,
    "standard":   2,
    "strict":     3,
    "maximum":    4,
}


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
    """Parse LLM-Hint header value into structured dimensions.

    Wave 4 #18 — prefers a real RFC 8941 Structured Fields parser (http-sfv)
    which handles quoted strings, numeric types, and parameter syntax
    correctly. Falls back to the legacy split-comma parser when http-sfv
    isn't available or when the input is non-conforming, so existing
    clients that send `task=reasoning,safety-min=3;require` keep working.
    """
    if not header_value:
        return None

    # First try the proper RFC 8941 parser
    parsed = _parse_hint_rfc8941(header_value)
    if parsed is not None:
        return parsed

    # Legacy fallback — preserves backward compatibility with clients that
    # send `task=reasoning,safety-min=3;require` (not strict 8941).
    return _parse_hint_legacy(header_value)


_REQUIRE_RE = re.compile(r"\s*;\s*require\s*", re.IGNORECASE)


def _parse_hint_legacy(header_value: str) -> Optional[LMRHHint]:
    hint = LMRHHint(raw=header_value)
    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue
        # Tolerate whitespace around the ;require marker
        required = bool(_REQUIRE_RE.search(part))
        if required:
            part = _REQUIRE_RE.sub("", part).strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        hint.dimensions.append(HintDimension(key.strip(), value.strip(), required))
    return hint if hint.dimensions else None


def _parse_hint_rfc8941(header_value: str) -> Optional[LMRHHint]:
    """RFC 8941 Dictionary parser.

    Maps Dictionary entries to HintDimension:
        task=reasoning → key=task, value=reasoning
        safety-min=3;require → key=safety-min, value=3, required=True
        cost="economy" → key=cost, value=economy (unwrapped)
        max-ttft=500 → key=max-ttft, value="500"

    Tokens, Integers, Decimals, Strings, Booleans, Byte-Sequences are all
    coerced to their natural string representation so the ranker's
    downstream comparisons continue to work unchanged.
    """
    try:
        import http_sfv
    except ImportError:
        return None

    try:
        d = http_sfv.Dictionary()
        d.parse(header_value.encode())
    except Exception:
        return None

    hint = LMRHHint(raw=header_value)
    for key, item in d.items():
        # Each dict entry is an InnerList or Item — both have .value and .params
        value_part = item.value if hasattr(item, "value") else item
        if isinstance(value_part, list):
            # InnerList — join values (rare for LMRH, preserve for forward compat)
            value_str = ",".join(_coerce_sfv_value(v) for v in value_part)
        else:
            value_str = _coerce_sfv_value(value_part)
        params = getattr(item, "params", {}) or {}
        required = bool(params.get("require", False))
        hint.dimensions.append(HintDimension(key, value_str, required))
    return hint if hint.dimensions else None


def _coerce_sfv_value(v) -> str:
    """Coerce any RFC 8941 Item value (Token, String, Integer, etc.) to str."""
    # http-sfv wraps primitives; str() of its classes produces the serialised form
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    # bytes (ByteSequence), Token, etc.
    try:
        return v.value if hasattr(v, "value") else str(v)
    except Exception:
        return str(v)


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
    native_tools: bool = True
    native_vision: bool = False
    priority: int = 10
    avg_ttft_ms: float = 0.0
    over_daily_budget: bool = False


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

            # Wave 4 #19 — numeric variants of latency & cost
            case "max-ttft":
                # Hard ceiling on TTFT in milliseconds (only meaningful when we
                # have a measured sample — otherwise treat as satisfied).
                try:
                    cap_ms = float(dim.value)
                except ValueError:
                    cap_ms = 0.0
                if profile.avg_ttft_ms == 0 or profile.avg_ttft_ms <= cap_ms:
                    score += WEIGHTS["latency"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            case "max-cost-per-1k":
                # Informational at scoring time — exact $/1k isn't stored on
                # the profile. Map to cost_tier buckets as a proxy until we
                # add precise pricing metadata (Wave 5 scope).
                try:
                    cap_usd = float(dim.value)
                except ValueError:
                    cap_usd = 0.0
                _TIER_USD = {"economy": 0.002, "standard": 0.01, "premium": 0.05}
                actual_usd = _TIER_USD.get(profile.cost_tier, 0.01)
                if actual_usd <= cap_usd:
                    score += WEIGHTS["cost"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

            # Wave 4 #19 — pass-through dims (consumed by endpoint, not scorer)
            case "effort" | "cascade" | "hedge" | "tenant" | "freshness":
                pass

            # Wave 4 #21 — refusal-rate human-readable alias for safety
            case "refusal-rate":
                ceil_ = _REFUSAL_RATE_TO_SAFETY_CEIL.get(dim.value.lower())
                if ceil_ is None:
                    unmet.append(dim.key)
                elif profile.safety <= ceil_:
                    score += WEIGHTS["safety-max"]
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

    # TTFT bonus: up to +5 for fast providers (0 ms→+5, 3000 ms→0, no data→no adjustment)
    if profile.avg_ttft_ms > 0:
        score += 5.0 * max(0.0, 1.0 - profile.avg_ttft_ms / 3000.0)

    # Budget demotion: heavy penalty when over daily spend cap, but not a hard block
    # (keeps provider as last-resort fallback if all providers are over budget)
    if profile.over_daily_budget:
        score -= 50.0

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


def rank_candidates_with_scores(
    profiles: list[CapabilityProfile], hint: Optional[LMRHHint]
) -> list[tuple[CapabilityProfile, list[str], float]]:
    """Same as rank_candidates but returns scores — used by the P2C balancer
    to identify ties within the top LMRH tier."""
    if not hint:
        # Fabricate descending scores from priority so the caller can still
        # identify "same score" via approximate equality.
        return [
            (p, [], float(1000 - p.priority))
            for p in sorted(profiles, key=lambda p: p.priority)
        ]
    scored = []
    for p in profiles:
        s, unmet = score_candidate(p, hint)
        if s == float("-inf"):
            continue
        scored.append((p, unmet, s))
    scored.sort(key=lambda x: -x[2])
    return scored


def build_hint_set_header(hint: Optional[LMRHHint], unmet: list[str]) -> str:
    """Wave 4 #20 — LLM-Hint-Set echo: which hint dims were HONORED.

    Parallels the HTTP `Vary:` header pattern. Absent when no hint was sent.
    Example: `task=reasoning,safety-min=3` (dims that influenced routing)
    """
    if not hint or not hint.dimensions:
        return ""
    unmet_set = set(unmet or [])
    honored = [f"{d.key}={d.value}" for d in hint.dimensions if d.key not in unmet_set]
    return ",".join(honored)


def build_capability_header(
    profile: CapabilityProfile,
    unmet: list[str],
    cot_engaged: bool = False,
    tool_emulation: bool = False,
    chosen_because: str = "score",
) -> str:
    """Build the LLM-Capability response header.

    chosen_because (Wave 4 #20): explains how routing landed on this profile.
        "score"          — ranked #1 under LMRH scoring (default)
        "hard-constraint"— only candidate satisfying `;require` dims
        "fallback"       — primary failed; this was an ordered-fallback pick
        "cheapest"       — cascade cheap-first step picked this
        "p2c"            — PeakEWMA + power-of-two-choices tie-break
    """
    parts = [
        "v=1",
        f"provider={profile.provider_id}",
        f"model={profile.model_id}",
        f"task={','.join(profile.tasks)}",
        f"safety={profile.safety}",
        f"latency={profile.latency}",
        f"cost={profile.cost_tier}",
        f"region={','.join(profile.regions) if profile.regions else 'any'}",
        f"chosen-because={chosen_because}",
    ]
    if unmet:
        parts.append(f"unmet={' '.join(unmet)}")
    if cot_engaged:
        parts.append("cot-engaged=?1")
    if tool_emulation:
        parts.append("tool-emulation=?1")
    return ", ".join(parts)


