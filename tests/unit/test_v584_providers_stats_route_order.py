"""v5.8.4 — regression test for the providers_stats vs providers route order.

Both `providers_router` and `providers_stats_router` share the
``/api/providers`` prefix. providers_router declares ``/{provider_id}``
(single-segment parameterized) which catches any literal single-segment
path registered *later* on the same prefix — including
``/rolling-stats`` and ``/rolling-stats-by-node`` from
providers_stats_router. FastAPI walks `app.routes` in registration
order; the first match wins.

Pre-v5.8.4 ordering was providers_router (line 583) → providers_stats_router
(line 631) → both literal stats routes silently returned 404 from the
`get_provider("rolling-stats")` handler. The Providers page columns
that depend on these endpoints fell back to "no data" without surfacing
the failure.

This test pins the include order: providers_stats_router MUST register
its literal routes BEFORE providers_router's `/{provider_id}` so the
literals win the prefix race.
"""
from __future__ import annotations


def _route_index(app, path: str) -> int:
    """Return the index in app.routes where `path` is the route.path,
    or -1 if not found."""
    for i, r in enumerate(app.routes):
        if getattr(r, "path", None) == path:
            return i
    return -1


def test_rolling_stats_registered_before_provider_id_catchall():
    from app.main import app
    # Both routes belong to the /api/providers prefix.
    literal_stats = _route_index(app, "/api/providers/rolling-stats")
    literal_stats_by_node = _route_index(app, "/api/providers/rolling-stats-by-node")
    catchall = _route_index(app, "/api/providers/{provider_id}")
    assert literal_stats >= 0, "rolling-stats route missing from app.routes"
    assert literal_stats_by_node >= 0, "rolling-stats-by-node route missing from app.routes"
    assert catchall >= 0, "/{provider_id} route missing from app.routes"
    assert literal_stats < catchall, (
        f"/rolling-stats (idx {literal_stats}) must register BEFORE "
        f"/{{provider_id}} (idx {catchall}) — otherwise the parameterized "
        "route shadows it and silently returns 404. See v5.8.4 fix."
    )
    assert literal_stats_by_node < catchall, (
        f"/rolling-stats-by-node (idx {literal_stats_by_node}) must "
        f"register BEFORE /{{provider_id}} (idx {catchall})."
    )
