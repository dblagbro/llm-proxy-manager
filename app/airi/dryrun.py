"""AIRI dry-run engine — v4.0 milestone 3.

Before AIRI applies a change it shows the operator an honest impact
preview. For a provider change that is the priority-ranking diff plus how
much recent traffic touched the affected provider. For a rule (threshold)
change it is a plain-language description of what the cap change means.

These functions are pure — they take already-gathered data, not DB
sessions — so the proposal service stays the single place that touches
the DB, and the previews are trivially unit-testable.
"""
from __future__ import annotations


def _ranking(providers: list[dict]) -> list[str]:
    """Names of enabled providers in routing order (lower priority wins)."""
    enabled = [p for p in providers if p.get("enabled")]
    enabled.sort(key=lambda p: (p.get("priority", 9999), p.get("name", "")))
    return [p["name"] for p in enabled]


def provider_change_impact(
    *,
    field: str,
    target_name: str,
    current_value,
    new_value,
    providers: list[dict],
    traffic_counts: dict,
    traffic_total: int,
) -> dict:
    """Impact preview for a provider change.

    ``providers`` — current state, each ``{name, priority, enabled}``.
    ``traffic_counts`` — ``{provider_name: recent_request_count}``.
    """
    before = _ranking(providers)

    # Build the hypothetical "after" state.
    after_providers = []
    for p in providers:
        q = dict(p)
        if p.get("name") == target_name:
            if field == "priority":
                q["priority"] = new_value
            elif field == "enabled":
                q["enabled"] = new_value
            # auto_skip is a time-bounded skip — model it as "disabled" for
            # the ranking preview when hours > 0.
            elif field == "auto_skip_hours":
                q["enabled"] = not (int(new_value or 0) > 0) and p.get("enabled")
        after_providers.append(q)
    after = _ranking(after_providers)

    share = 0.0
    if traffic_total > 0:
        share = round(100.0 * traffic_counts.get(target_name, 0) / traffic_total, 1)

    reordered = before != after
    summary = (
        f"{target_name}: {field} {current_value!r} -> {new_value!r}. "
        + (
            f"Routing order changes ({' > '.join(before)}  =>  {' > '.join(after)})."
            if reordered
            else "Routing order is unchanged (priority ranking is the same)."
        )
        + f" {target_name} served {share}% of the last {traffic_total} requests."
    )

    warnings = []
    if field in ("enabled", "auto_skip_hours") and share >= 10.0:
        warnings.append(
            f"{target_name} is carrying {share}% of recent traffic — taking it "
            f"out of rotation will shift a meaningful share of requests."
        )
    if field == "enabled" and new_value is False and len(after) == 0:
        warnings.append("This would leave NO enabled providers.")

    return {
        "kind": "provider_change",
        "summary": summary,
        "ranking_before": before,
        "ranking_after": after,
        "reordered": reordered,
        "recent_traffic_share_pct": share,
        "recent_request_window": traffic_total,
        "warnings": warnings,
    }


def rule_change_impact(*, rule_name: str, setting: str,
                       current_value, new_value) -> dict:
    """Impact preview for a threshold-rule change. Threshold rules are caps
    /tunables — there is no past traffic to re-simulate, so the preview is a
    plain-language description of what the change widens or tightens."""
    direction = "no change"
    try:
        if int(new_value) > int(current_value):
            direction = "widens"
        elif int(new_value) < int(current_value):
            direction = "tightens"
    except (TypeError, ValueError):
        pass
    return {
        "kind": "rule_change",
        "summary": (
            f"Rule '{rule_name}' ({setting}): {current_value} -> {new_value}. "
            f"This {direction} the limit. Threshold rules govern the AI Provider "
            f"Supervisor's own caps; the change takes effect on the supervisor's "
            f"next run."
        ),
        "setting": setting,
        "from": current_value,
        "to": new_value,
        "direction": direction,
        "warnings": [],
    }
