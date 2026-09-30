"""Walk a FastAPI app's routes in a way that survives FastAPI's own changes.

v5.22.38 — FastAPI **0.141.1** / Starlette **1.6.0** changed what
``app.routes`` contains. ``include_router()`` used to flatten the child
router's routes into the parent, so ``app.routes`` held every ``APIRoute`` with
a usable ``.path``. It now appends one opaque ``_IncludedRouter`` per included
router, with ``path = None``:

    Counter({'_IncludedRouter': 49, 'Route': 4, 'APIRoute': 3, 'Mount': 1})

Six tests introspected ``app.routes`` looking for a literal path, so on that
version they all reported core endpoints "not registered" — ``/v1/messages``,
``/v1/audio/speech``, ``/v1/images/generations``, the rolling-stats routes, the
SPA catch-all. The app is fine: a ``TestClient`` POST to ``/v1/audio/speech``
returns 401 (auth required), not 404. Only the introspection was wrong.

It surfaced the first time CI gated the full unit suite (v5.22.38), because
``requirements.txt`` says ``fastapi>=0.115.0`` and a clean runner resolves the
newest, while the dev box had an older one that still flattened. Exactly the
class of breakage a dependency bump delivers silently.

``iter_routes`` descends into anything route-shaped, so it works on both the
flattening and the non-flattening versions. Prefer it over touching
``app.routes`` directly in a test.
"""
from __future__ import annotations


def _children(node):
    """(child, path_prefix) pairs to descend into, for any route-ish object.

    Covers both FastAPI shapes:

    - **<= 0.140-ish**: ``include_router`` flattened, so an app/router simply has
      ``.routes`` full of ``APIRoute``. ``Mount`` nests via ``.routes`` or
      ``.app``.
    - **0.141.1+**: ``include_router`` appends one ``_IncludedRouter`` whose
      child hangs off ``include_context.included_router`` (with the include's
      ``prefix``) or ``original_router``. Neither ``.routes`` nor ``.router``
      exists on it, which is why a naive walk finds nothing.
    """
    out = []

    ctx = getattr(node, "include_context", None)
    if ctx is not None:
        child = getattr(ctx, "included_router", None)
        if child is not None:
            out.append((child, getattr(ctx, "prefix", "") or ""))
    orig = getattr(node, "original_router", None)
    if orig is not None:
        out.append((orig, ""))

    for attr in ("routes", "router", "app"):
        child = getattr(node, attr, None)
        if child is None or child is node:
            continue
        if isinstance(child, (list, tuple)):
            out.extend((item, "") for item in child)
        elif hasattr(child, "routes") or hasattr(child, "include_context"):
            out.append((child, ""))
    return out


def iter_routes(app_or_router, _seen=None, _prefix=""):
    """Yield ``(path, route)`` for every leaf route reachable from an app.

    A leaf carries a string ``path``; containers are descended into, with any
    include prefix accumulated. Identity-guarded, since the same router object
    can legitimately be included more than once.
    """
    if _seen is None:
        _seen = set()
    key = (id(app_or_router), _prefix)
    if key in _seen:
        return
    _seen.add(key)

    path = getattr(app_or_router, "path", None)
    if isinstance(path, str):
        yield (_prefix + path, app_or_router)

    for child, extra in _children(app_or_router):
        yield from iter_routes(child, _seen, _prefix + extra)


def route_paths(app) -> set:
    """Every path the app can route to."""
    return {p for p, _ in iter_routes(app)}


def route_methods(app, path: str) -> set:
    """HTTP methods registered for ``path`` (empty set if not registered)."""
    methods = set()
    for p, r in iter_routes(app):
        if p == path:
            methods |= set(getattr(r, "methods", None) or [])
    return methods


def ordered_paths(app) -> list:
    """Paths in routing order — for tests asserting one route shadows another.

    Order matters: the literal ``/api/providers/rolling-stats`` must be
    registered before the parameterized ``/api/providers/{provider_id}``, or the
    catch-all shadows it (v5.8.4).
    """
    return [p for p, _ in iter_routes(app)]
