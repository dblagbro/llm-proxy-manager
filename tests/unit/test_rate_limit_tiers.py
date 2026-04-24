"""Unit tests for named rate-limit tiers (Wave 6)."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.auth.rate_limit_tiers import (
    get_tier, list_tiers, tier_names, RateLimitTier,
)


class TestGetTier:
    def test_none_name_returns_none(self):
        assert get_tier(None) is None

    def test_empty_name_returns_none(self):
        assert get_tier("") is None

    def test_unknown_returns_none(self):
        assert get_tier("platinum-plus-ultra") is None

    def test_case_insensitive_lookup(self):
        assert get_tier("FREE") is not None
        assert get_tier("Pro") is not None
        assert get_tier("enterprise") is not None

    def test_free_tier_values(self):
        t = get_tier("free")
        assert t.rpm == 20
        assert t.rpd == 1000
        assert t.burst == 2

    def test_starter_tier_values(self):
        t = get_tier("starter")
        assert t.rpm == 60
        assert t.rpd == 10_000
        assert t.burst == 5

    def test_pro_tier_values(self):
        t = get_tier("pro")
        assert t.rpm == 300
        assert t.rpd == 100_000
        assert t.burst == 20

    def test_enterprise_tier_values(self):
        t = get_tier("enterprise")
        assert t.rpm == 2000
        assert t.rpd == 1_000_000
        assert t.burst == 100

    def test_unlimited_tier_is_all_none(self):
        t = get_tier("unlimited")
        assert t.rpm is None
        assert t.rpd is None
        assert t.burst is None


class TestTierRegistry:
    def test_list_tiers_returns_all(self):
        tiers = list_tiers()
        names = {t.name for t in tiers}
        assert {"unlimited", "free", "starter", "pro", "enterprise"} <= names

    def test_tier_names_helper(self):
        names = tier_names()
        assert "free" in names
        assert "enterprise" in names

    def test_tiers_are_ordered_from_lowest_to_highest(self):
        """Non-unlimited tiers should have increasing RPM."""
        ordered = [get_tier(n) for n in ["free", "starter", "pro", "enterprise"]]
        rpms = [t.rpm for t in ordered]
        assert rpms == sorted(rpms)


class TestTierImmutability:
    def test_tier_is_frozen_dataclass(self):
        t = get_tier("free")
        with pytest.raises((AttributeError, Exception)):
            t.rpm = 999_999
