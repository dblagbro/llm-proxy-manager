"""Response-side LMRH header builders.

- LLM-Capability (emitted on every response): what routing decided.
- LLM-Hint-Set (optional): which input dimensions were honored.

Split out from the monolithic ``routing/lmrh.py`` in the 2026-04-23
refactor.
"""
from __future__ import annotations

from typing import Optional

from app.routing.lmrh.types import CapabilityProfile, LMRHHint


def build_hint_set_header(hint: Optional[LMRHHint], unmet: list[str]) -> str:
    """Wave 4 #20 — LLM-Hint-Set echo: which hint dims were HONORED.

    Parallels the HTTP ``Vary:`` header pattern. Absent when no hint was
    sent. Example: ``task=reasoning,safety-min=3``.
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
    model_override: str = "",
    hint: Optional[LMRHHint] = None,
) -> str:
    """Build the LLM-Capability response header.

    chosen_because (Wave 4 #20): explains how routing landed on this profile.
        "score"           — ranked #1 under LMRH scoring (default)
        "hard-constraint" — only candidate satisfying ``;require`` dims
        "fallback"        — primary failed; this was an ordered-fallback pick
        "cheapest"        — cascade cheap-first step picked this
        "p2c"             — PeakEWMA + power-of-two-choices tie-break

    v3.0.37: model_override (DevinGPT report 2026-05-01). The capability
    profile carries the provider's canonical model id (e.g. ``gpt-4o``)
    while the caller may have asked for a specific variant (e.g.
    ``gpt-4o-mini``). The header used to report the canonical, leaving
    DevinGPT's substitution detector with a header/body mismatch.
    Passing the caller's model resolves to the correct value.
    """
    model_str = model_override or profile.model_id
    parts = [
        "v=1",
        f"provider={profile.provider_id}",
        f"model={model_str}",
        f"task={','.join(profile.tasks)}",
        f"safety={profile.safety}",
        f"latency={profile.latency}",
        f"cost={profile.cost_tier}",
        f"region={','.join(profile.regions) if profile.regions else 'any'}",
        f"chosen-because={chosen_because}",
    ]
    if unmet:
        parts.append(f"unmet={' '.join(unmet)}")
    # v3.0.52 (LMRH 1.2 §E3): served-region + region-honored when caller
    # sent a region= dim. served-region echoes the most-specific region
    # the profile claims; region-honored signals strict (exact match) vs
    # loose (matched via hierarchy) so compliance auditors can verify.
    if hint:
        region_dim = hint.get("region")
        if region_dim and "region" not in (unmet or []) and profile.regions:
            wanted = {v.strip().lower() for v in region_dim.value.split(",") if v.strip()}
            wanted.discard("any"); wanted.discard("*")
            profile_regions_lower = [r.lower() for r in profile.regions]
            served = profile_regions_lower[0]
            if wanted:
                exact = wanted & set(profile_regions_lower)
                if exact:
                    served = sorted(exact)[0]
                    honored = "strict"
                else:
                    served = next(
                        (pr for w in wanted for pr in profile_regions_lower
                         if pr.startswith(w + "-")),
                        served,
                    )
                    honored = "loose"
                parts.append(f"served-region={served}")
                parts.append(f"region-honored={honored}")
    if cot_engaged:
        parts.append("cot-engaged=?1")
    if tool_emulation:
        parts.append("tool-emulation=?1")
    return ", ".join(parts)
