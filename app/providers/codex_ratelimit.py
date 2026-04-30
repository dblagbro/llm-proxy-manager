"""
Codex rate-limit awareness + back-pressure (v3.0.16).

The chatgpt.com codex backend reports subscription-quota usage on every
response via ``x-codex-*`` headers:

    x-codex-active-limit: premium
    x-codex-plan-type: plus
    x-codex-primary-used-percent: 2          # current % of primary window
    x-codex-secondary-used-percent: 3        # current % of secondary window
    x-codex-primary-window-minutes: 300      # 5h
    x-codex-secondary-window-minutes: 10080  # weekly
    x-codex-primary-reset-after-seconds: 14180
    x-codex-secondary-reset-after-seconds: 510106
    x-codex-primary-reset-at: 1777591525
    x-codex-secondary-reset-at: 1778087450
    x-codex-credits-has-credits: False
    x-codex-credits-balance: ''
    x-codex-credits-unlimited: False

For fixed-cost subscription providers (claude-oauth, codex-oauth, future
similar), these are the back-pressure signals we need. The proxy:

  1. Reads them on every successful response and stores per-provider
     state in-memory (no DB churn — the upstream is authoritative).
  2. Exposes the state via `get_state(provider_id)` so the monitoring
     UI / admin endpoints can surface "X% used, resets in Y minutes."
  3. When a 429 or "rate limit reached" response is observed, opens
     the provider's circuit breaker for ``reset_after_seconds`` so
     the request fails over to the next-priority provider until the
     subscription window resets — a much better UX than retrying and
     getting blocked again.

State is in-memory only; resets on container restart. That's acceptable
because the upstream computes the limits authoritatively and we'll
re-discover them on the next response.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class RateLimitState:
    """Per-provider snapshot of the subscription's quota window."""
    primary_used_percent: Optional[float] = None
    primary_window_minutes: Optional[int] = None
    primary_reset_at: Optional[float] = None       # unix timestamp
    secondary_used_percent: Optional[float] = None
    secondary_window_minutes: Optional[int] = None
    secondary_reset_at: Optional[float] = None
    plan_type: Optional[str] = None                # "plus" / "pro" / "team" / ...
    active_limit: Optional[str] = None             # "premium" / etc
    last_observed_at: float = field(default_factory=time.time)


# {provider_id: RateLimitState}
_STATES: dict[str, RateLimitState] = {}


def _f(headers: dict, key: str, fallback=None):
    """Case-insensitive header pluck with float coercion."""
    v = headers.get(key) or headers.get(key.lower()) or headers.get(key.upper())
    if v is None or v == "":
        return fallback
    try:
        return float(v)
    except (TypeError, ValueError):
        return fallback


def _i(headers: dict, key: str, fallback=None):
    f = _f(headers, key)
    return int(f) if f is not None else fallback


def _s(headers: dict, key: str, fallback=None):
    return headers.get(key) or headers.get(key.lower()) or headers.get(key.upper()) or fallback


def update_from_headers(provider_id: str, headers: dict) -> RateLimitState:
    """Parse x-codex-* headers from a successful response and update state.

    Returns the new state for caller logging if useful.
    """
    state = _STATES.get(provider_id) or RateLimitState()
    state.primary_used_percent = _f(headers, "x-codex-primary-used-percent", state.primary_used_percent)
    state.primary_window_minutes = _i(headers, "x-codex-primary-window-minutes", state.primary_window_minutes)
    state.secondary_used_percent = _f(headers, "x-codex-secondary-used-percent", state.secondary_used_percent)
    state.secondary_window_minutes = _i(headers, "x-codex-secondary-window-minutes", state.secondary_window_minutes)

    primary_reset_at = _f(headers, "x-codex-primary-reset-at")
    if primary_reset_at:
        state.primary_reset_at = primary_reset_at
    else:
        primary_reset_after = _f(headers, "x-codex-primary-reset-after-seconds")
        if primary_reset_after is not None:
            state.primary_reset_at = time.time() + primary_reset_after

    secondary_reset_at = _f(headers, "x-codex-secondary-reset-at")
    if secondary_reset_at:
        state.secondary_reset_at = secondary_reset_at
    else:
        secondary_reset_after = _f(headers, "x-codex-secondary-reset-after-seconds")
        if secondary_reset_after is not None:
            state.secondary_reset_at = time.time() + secondary_reset_after

    state.plan_type = _s(headers, "x-codex-plan-type", state.plan_type)
    state.active_limit = _s(headers, "x-codex-active-limit", state.active_limit)
    state.last_observed_at = time.time()
    _STATES[provider_id] = state
    return state


def get_state(provider_id: str) -> Optional[RateLimitState]:
    """Return the most recently observed state, or None if never observed."""
    return _STATES.get(provider_id)


def all_states() -> dict[str, RateLimitState]:
    return dict(_STATES)


def detect_rate_limit_failure(status_code: int, body_text: str, headers: dict) -> Optional[float]:
    """Inspect a 4xx response; if it indicates a quota/rate-limit hit, return
    the recommended hold-down in seconds. Else None.

    Codex backends emit a 429 with a JSON body when limits are reached, and
    sometimes return 403 with a "limit reached" message when the subscription
    is fully exhausted. Both should park the CB until the relevant window
    resets — primary first, falling back to secondary, then a 5-minute default.
    """
    if status_code != 429 and status_code != 403:
        return None
    body_lower = (body_text or "").lower()
    is_limit = (
        "limit" in body_lower
        or "quota" in body_lower
        or "rate" in body_lower
        or "too many" in body_lower
    )
    if status_code == 429 or is_limit:
        primary_reset_after = _f(headers, "x-codex-primary-reset-after-seconds")
        if primary_reset_after and primary_reset_after > 0:
            return float(primary_reset_after)
        secondary_reset_after = _f(headers, "x-codex-secondary-reset-after-seconds")
        if secondary_reset_after and secondary_reset_after > 0:
            return float(secondary_reset_after)
        return 300.0  # 5-minute default if upstream didn't tell us
    return None
