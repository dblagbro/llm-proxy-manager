"""v3.7.20 — BUG-020 fix: utilization bucket filter applies BEFORE
the P2C/PeakEWMA random sample, so the v3.7.4 reorder is no longer
silently overridden by score re-sort + EWMA-with-samples preference."""
from __future__ import annotations

from pathlib import Path


def test_router_imports_utilization_bucket():
    """The fix requires _utilization_bucket to be importable in router."""
    src = Path("app/routing/router.py").read_text()
    assert "_utilization_bucket" in src
    assert "from app.routing.external_rotation import" in src


def test_util_map_hoisted_to_broader_scope():
    """util_map must be initialized before the try block so the P2C
    selection (~100 lines later) can reference it."""
    src = Path("app/routing/router.py").read_text()
    # Initialize before try
    assert "util_map: dict[str, float] = {}" in src
    # Then assigned inside try
    assert "util_map = await get_utilization_map(db)" in src


def test_bucket_filter_runs_before_p2c_random_sample():
    """The bucket filter must drop higher-bucket candidates BEFORE the
    P2C random sample picks 2."""
    src = Path("app/routing/router.py").read_text()
    # Find the P2C block
    idx = src.index("# Wave 3 #13 — PeakEWMA + P2C")
    block = src[idx:idx + 3000]
    # The bucket filter must appear BEFORE the random.sample call
    bucket_idx = block.index("BUG-020 fix")
    random_idx = block.index("_random.sample")
    assert bucket_idx < random_idx, "bucket filter must precede random sample"


def test_bucket_filter_keeps_lowest_bucket_only():
    """If candidates span multiple utilization buckets, only the lowest
    bucket entries survive into the P2C step."""
    src = Path("app/routing/router.py").read_text()
    # Check the actual filter logic
    assert "bucket_min = min(" in src
    assert "low_bucket = [t for t in top_tier if _bucket_of(t) == bucket_min]" in src


def test_bucket_filter_no_op_when_buckets_equal():
    """When all candidates are in the same bucket, top_tier is unchanged
    and the existing P2C/EWMA logic runs as before. Source check: the
    narrowing only assigns top_tier=low_bucket when the filter shrinks
    the list."""
    src = Path("app/routing/router.py").read_text()
    assert "if len(low_bucket) < len(top_tier):" in src
    assert "top_tier = low_bucket" in src


def test_bucket_filter_no_op_when_util_map_empty():
    """If util_map is empty (snapshot table issue), the filter must be
    skipped entirely so routing continues to work."""
    src = Path("app/routing/router.py").read_text()
    # The guard requires util_map to be truthy AND len(top_tier)>=2
    assert "if util_map and len(top_tier) >= 2:" in src


def test_fall_through_uses_top_tier_not_ranked_scored():
    """When the bucket filter narrows top_tier to a single entry, the
    fall-through path must pick from top_tier (the narrowed list), not
    ranked_scored (the original list — would yield the wrong candidate)."""
    src = Path("app/routing/router.py").read_text()
    # Find the else branch after the if len(top_tier) >= 2:
    block = src[src.index("# Wave 3 #13 — PeakEWMA + P2C"):]
    # Locate the else: line after the sample block
    # Look specifically for `best_profile, unmet, _ = top_tier[0]`
    assert "best_profile, unmet, _ = top_tier[0]" in block


# ── Integration-level simulation of the routing decision ──────────


class _FakeProfile:
    def __init__(self, provider_id, priority, cost_tier="standard"):
        self.provider_id = provider_id
        self.priority = priority
        self.cost_tier = cost_tier


def test_simulated_p2c_with_different_buckets_prefers_lower():
    """Reproduce the BUG-020 scenario in isolation: two candidates
    within 1.0 score band, different utilization buckets. The lower
    bucket must win regardless of EWMA samples on either side.

    This test simulates the patched selection logic directly using the
    fix's data flow (without spinning up the full router).
    """
    from app.routing.external_rotation import _utilization_bucket

    # Simulate state right before the P2C block: two candidates tied
    # in score, both within the top_tier band.
    p_vg = _FakeProfile("d5123a00", priority=4)
    p_gm = _FakeProfile("91bafda9", priority=5)
    top_tier = [(p_vg, set(), 5.0), (p_gm, set(), 5.0)]
    util_map = {"d5123a00": 49.0, "91bafda9": 4.0}
    bucket_size = 25.0

    def _bucket_of(t):
        u = util_map.get(t[0].provider_id)
        return _utilization_bucket(u, bucket_size)

    # Apply the same filter the patch installs
    bucket_min = min(_bucket_of(t) for t in top_tier)
    low_bucket = [t for t in top_tier if _bucket_of(t) == bucket_min]
    assert len(low_bucket) == 1
    assert low_bucket[0][0].provider_id == "91bafda9"  # Gmail wins


def test_simulated_p2c_same_bucket_does_not_narrow():
    """When both candidates are in the same bucket, the filter is a
    no-op and the original P2C logic applies."""
    from app.routing.external_rotation import _utilization_bucket

    p_a = _FakeProfile("aaa", priority=4)
    p_b = _FakeProfile("bbb", priority=5)
    top_tier = [(p_a, set(), 5.0), (p_b, set(), 5.0)]
    util_map = {"aaa": 30.0, "bbb": 40.0}  # both in bucket 1 (25-49)

    def _bucket_of(t):
        return _utilization_bucket(util_map.get(t[0].provider_id), 25.0)

    bucket_min = min(_bucket_of(t) for t in top_tier)
    low_bucket = [t for t in top_tier if _bucket_of(t) == bucket_min]
    assert len(low_bucket) == 2  # no narrowing


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 20)
