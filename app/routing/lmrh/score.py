"""LMRH candidate scoring + ranking.

Given a parsed LMRHHint and a list of CapabilityProfile candidates,
score each candidate against each hint dimension, enforce ``;require``
hard constraints, and return an ordered list.

Split out from the monolithic ``routing/lmrh.py`` in the 2026-04-23
refactor. This is where the bulk of LMRH changes land (new dims +
tweaks to the weight/rank math), so isolating it from parser +
header-builder code reduces navigation cost on feature edits.
"""
from __future__ import annotations

from typing import Optional

from app.routing.lmrh.types import (
    CapabilityProfile, LMRHHint,
    WEIGHTS, TASK_ALIASES, LATENCY_RANK, COST_RANK,
    _REFUSAL_RATE_TO_SAFETY_CEIL,
)


def score_candidate(profile: CapabilityProfile, hint: LMRHHint) -> tuple[float, list[str]]:
    """Score a candidate profile against a hint.

    Returns (score, unmet_soft_dims). Returns (-inf, [...]) when any
    hard constraint (``;require``) fails.
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
                # v3.0.51 (LMRH 1.2 §E3): comma-separated any-of + hierarchy
                # matching. ``region=eu`` is satisfied by a profile tagged
                # ``eu-west`` or ``eu-central``; ``region=us,ca`` is
                # satisfied by either. Profiles with no regions configured
                # are treated as compatible (the proxy hasn't classified
                # the upstream yet — soft pass for backwards compat).
                # v3.0.52: ``;sovereign`` rejects unconfigured profiles
                # (uncertainty = reject) — compliance workloads can't
                # accept "we don't know what region this provider serves
                # from."
                if not profile.regions:
                    if dim.sovereign:
                        return float("-inf"), [dim.key]
                    score += WEIGHTS["region"]
                else:
                    wanted = {v.strip().lower() for v in dim.value.split(",") if v.strip()}
                    wanted.discard("any")
                    wanted.discard("*")
                    if not wanted:
                        score += WEIGHTS["region"]
                    else:
                        profile_regions_lower = {r.lower() for r in profile.regions}
                        matched = (
                            bool(wanted & profile_regions_lower)
                            or any(
                                pr.startswith(w + "-")
                                for w in wanted for pr in profile_regions_lower
                            )
                        )
                        if matched:
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

            # v3.0.25 — exclude=name1,name2 — negative provider selection.
            # Soft: drops score by exclude weight if profile.provider matches.
            # Hard (;require): handled in select_provider (filters out the
            # candidate entirely BEFORE this loop runs). Here we just
            # apply the soft penalty for unrequired exclude.
            case "exclude":
                excluded = {n.strip().lower() for n in dim.value.split(",") if n.strip()}
                pname = (profile.provider_name or "").lower()
                ptype = (profile.provider_type or "").lower()
                if pname in excluded or ptype in excluded:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    score -= WEIGHTS.get("exclude", 5)
                    unmet.append(dim.key)

            # v3.0.25 — provider-hint=name (positive selection; existing).
            # Score-only here — endpoint's pinned_provider_id handles hard.
            case "provider-hint":
                wanted = {n.strip().lower() for n in dim.value.split(",") if n.strip()}
                pname = (profile.provider_name or "").lower()
                ptype = (profile.provider_type or "").lower()
                if pname in wanted or ptype in wanted:
                    score += WEIGHTS.get("provider-hint", 5)
                else:
                    if dim.required:
                        return float("-inf"), [dim.key]
                    unmet.append(dim.key)

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
    """Return profiles sorted by descending score. Hard-constraint failures
    are excluded entirely. Without a hint, sorts by priority only."""
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
