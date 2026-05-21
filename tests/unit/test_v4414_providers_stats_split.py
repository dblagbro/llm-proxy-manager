"""v4.4.14 ``providers.py`` 4th-sibling split — invariants for the
read-side / stats endpoints extraction.

Pre-split: ``app/api/providers.py`` was 958 LOC. v3.9.8 had already
split out ``provider_lifecycle.py`` + ``provider_capabilities.py``
(comment in main.py: "providers.py split into 3 siblings to stay
<1000 lines"). The file grew back. v4.4.14 extracts the 4 read-side
endpoints (``list_providers``, ``provider_rolling_stats``,
``provider_rolling_stats_by_node``, ``provider_usage``) into a 4th
sibling ``providers_stats.py``, mirroring the existing pattern.

Guarded invariants:
1. Both files load cleanly.
2. The 4 stats endpoints register at their expected paths.
3. The mutation endpoints (POST/PUT/DELETE) stay in ``providers.py``.
4. ``main.py`` includes the new router.
5. Neither file exceeds 800 LOC (soft ceiling after the split).
"""
from __future__ import annotations

from pathlib import Path
import importlib


def test_both_files_load_cleanly():
    importlib.import_module("app.api.providers")
    importlib.import_module("app.api.providers_stats")


def test_stats_endpoints_register_on_stats_router():
    """The 4 read-side endpoints are owned by the new sibling router."""
    from app.api.providers_stats import router as stats_router
    paths = {r.path for r in stats_router.routes}
    for expected in (
        "/api/providers",
        "/api/providers/rolling-stats",
        "/api/providers/rolling-stats-by-node",
        "/api/providers/{provider_id}/usage",
    ):
        assert expected in paths, (
            f"stats endpoint {expected} missing from providers_stats_router. "
            f"Got: {sorted(paths)}"
        )


def test_mutation_endpoints_stay_on_providers_router():
    """POST/PUT/DELETE + non-stats GETs stay in providers.py."""
    from app.api.providers import router as providers_router
    paths_methods = {(r.path, tuple(sorted(r.methods or set()))) for r in providers_router.routes}
    paths = {p for (p, _) in paths_methods}
    # Mutation paths still in providers.py
    assert "/api/providers" in paths  # POST (create) — distinct from the stats GET
    assert "/api/providers/{provider_id}" in paths  # GET + PUT + DELETE
    assert "/api/providers/_purge-test-tombstones" in paths  # POST
    assert "/api/providers/{provider_id}/rate-limit" in paths  # GET (not stats)
    # The 4 stats endpoints should NOT be in providers_router
    stats_only = {
        "/api/providers/rolling-stats",
        "/api/providers/rolling-stats-by-node",
        "/api/providers/{provider_id}/usage",
    }
    moved = stats_only & paths
    # The list endpoint ("/api/providers" GET) overlaps with the POST in
    # providers.py — both map to the same path with different methods.
    # FastAPI handles that, so we don't need to assert non-overlap by path.
    # But the stats-specific paths must not appear on providers_router.
    assert not moved, (
        f"stats endpoints leaked into providers.py router: {moved}"
    )


def test_main_includes_providers_stats_router():
    """``main.py`` must include the new router so the endpoints are
    actually served. Source-level check (importing main is expensive
    + has side effects we don't want in unit tests)."""
    src = Path("app/main.py").read_text()
    assert "from app.api.providers_stats import router as providers_stats_router" in src
    assert "app.include_router(providers_stats_router)" in src


def test_neither_file_exceeds_800_loc():
    """Soft ceiling per split file. If either crosses 800, signal
    "re-split this domain". Pre-split was 958 in one file; after the
    split both should be well under 800."""
    too_big = []
    for fn in ("app/api/providers.py", "app/api/providers_stats.py"):
        loc = sum(1 for _ in Path(fn).read_text().splitlines())
        if loc > 800:
            too_big.append((fn, loc))
    assert not too_big, (
        f"providers split files exceed 800 LOC: {too_big}. Time to split further."
    )


def test_stats_file_uses_lazy_serialize_import():
    """The stats file imports ``_serialize`` lazily inside the
    handler body to avoid the circular-import trap (providers.py
    imports providers_stats... no it doesn't — but the helper's
    canonical home is providers.py, and putting the import at the
    top would be fine here; the lazy form is defensive against a
    future change that DOES add a back-import). Lock in the lazy
    form."""
    src = Path("app/api/providers_stats.py").read_text()
    # The lazy import lives inside list_providers
    idx = src.index("async def list_providers(")
    body = src[idx:idx + 3000]
    assert "from app.api.providers import _serialize" in body
