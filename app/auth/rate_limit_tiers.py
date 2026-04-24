"""Named rate-limit tiers — Wave 6 enterprise feature.

Provides human-friendly tier names (free / starter / pro / enterprise) that
map to rate-limit parameters. Keys can opt into a tier via the
`rate_limit_tier` column; the per-key `rate_limit_rpm` field still works
as an override for custom contracts.

Tier semantics:
  rpm:   requests per minute (sliding window, cluster-aware)
  rpd:   requests per day (soft cap, resets at UTC midnight)
  burst: max concurrent in-flight requests per key

A value of None at any dimension means "unlimited at that dimension".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RateLimitTier:
    name: str
    rpm: Optional[int]
    rpd: Optional[int]
    burst: Optional[int]
    description: str


# Default registry. Operators can extend this via settings.custom_rate_limit_tiers
# without touching code.
_DEFAULT_TIERS: dict[str, RateLimitTier] = {
    "unlimited": RateLimitTier(
        name="unlimited",
        rpm=None, rpd=None, burst=None,
        description="No rate limits applied.",
    ),
    "free": RateLimitTier(
        name="free",
        rpm=20, rpd=1000, burst=2,
        description="Free tier: 20 RPM / 1000 RPD / 2 concurrent.",
    ),
    "starter": RateLimitTier(
        name="starter",
        rpm=60, rpd=10000, burst=5,
        description="Starter tier: 60 RPM / 10k RPD / 5 concurrent.",
    ),
    "pro": RateLimitTier(
        name="pro",
        rpm=300, rpd=100000, burst=20,
        description="Pro tier: 300 RPM / 100k RPD / 20 concurrent.",
    ),
    "enterprise": RateLimitTier(
        name="enterprise",
        rpm=2000, rpd=1000000, burst=100,
        description="Enterprise tier: 2k RPM / 1M RPD / 100 concurrent.",
    ),
}


def get_tier(name: Optional[str]) -> Optional[RateLimitTier]:
    """Look up a tier by name. Returns None for unknown tiers (caller decides)."""
    if not name:
        return None
    return _DEFAULT_TIERS.get(name.lower())


def list_tiers() -> list[RateLimitTier]:
    return list(_DEFAULT_TIERS.values())


def tier_names() -> list[str]:
    return list(_DEFAULT_TIERS.keys())
