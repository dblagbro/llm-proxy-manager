"""v5.21.7 — Pool-leak fix + auto-watcher.

Two coupled ships closing the recurring outage since 2026-07-09:

1. **The leak fix**: ``runs.py::get_events`` no longer accepts
   ``Depends(get_db)``. The SSE branch holds the request-scoped
   session for the ENTIRE stream lifetime — for long runs that's
   hours. Fixed by making the handler open short-lived sessions
   on demand (lookup, replay, polling), so the pool slot is
   released before the long broker loop starts.

2. **The auto-watcher**: ``pool_leak_watcher`` background task
   samples pool utilization every 30s and auto-dumps the async
   session trace when 50/75/90% thresholds are crossed. Companion
   to the v5.21.6 SIGUSR2 dumper — that one is manual, this one
   catches accumulation before exhaustion.
"""
from __future__ import annotations
from pathlib import Path


# ── Runs endpoint fix ────────────────────────────────────────────────

def test_get_events_no_longer_uses_depends_get_db():
    """The primary leak fix. A request-scoped Depends(get_db) held
    across a StreamingResponse pins a pool slot for the entire stream
    duration."""
    src = Path("app/api/runs.py").read_text()
    # Extract the get_events function signature block
    start = src.find("async def get_events(")
    body_start = src.find("):", start)
    signature = src[start:body_start]
    assert "Depends(get_db)" not in signature, (
        "get_events must not use Depends(get_db) — pool slot held for entire SSE stream"
    )


def test_get_events_uses_short_lived_sessions():
    """The handler opens per-operation sessions via AsyncSessionLocal
    instead. Look for the three sites we care about: initial run
    lookup, catch-up replay in the generator, polling branch."""
    src = Path("app/api/runs.py").read_text()
    fn_start = src.find("async def get_events(")
    fn_end = src.find("async def adopt_run(", fn_start)
    body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
    # AsyncSessionLocal must appear at least twice (lookup + at least
    # one of the branches).
    assert body.count("AsyncSessionLocal()") >= 2, (
        f"expected 2+ AsyncSessionLocal() uses in get_events, "
        f"got {body.count('AsyncSessionLocal()')}"
    )


def test_generator_does_not_reference_request_scoped_db():
    """The gen() closure must NOT reference an outer ``db`` variable
    from Depends. Prevents accidental re-introduction of the pattern.
    Match: ``await db.execute`` inside the generator body would be a
    regression (the generator can only touch DB via its OWN session)."""
    src = Path("app/api/runs.py").read_text()
    fn_start = src.find("async def get_events(")
    fn_end = src.find("async def adopt_run(", fn_start)
    body = src[fn_start:fn_end] if fn_end > 0 else src[fn_start:]
    gen_start = body.find("async def gen():")
    gen_end = body.find("return StreamingResponse(gen()", gen_start)
    gen_body = body[gen_start:gen_end] if gen_end > 0 else body[gen_start:]
    assert "await db.execute" not in gen_body, (
        "gen() must not use the outer request-scoped `db`"
    )


# ── Pool leak watcher ────────────────────────────────────────────────

def test_watcher_module_present():
    p = Path("app/monitoring/pool_leak_watcher.py")
    assert p.exists()
    src = p.read_text()
    assert "async def pool_leak_watcher_loop" in src


def test_watcher_wired_into_lifespan():
    src = Path("app/main.py").read_text()
    assert "pool_leak_watcher_loop" in src


def test_watcher_thresholds_are_sensible():
    """Three thresholds (50/75/90%) balance early warning vs log spam.
    Also verifies the arming/rearming state machine so we don't
    fire hundreds of dumps while a leak is accumulating."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "0.50" in src and "0.75" in src and "0.90" in src
    assert "_armed" in src
    assert "rearmed" in src or "re-arm" in src.replace("_armed", "").lower()


def test_watcher_fires_at_highest_crossed_threshold():
    """If pool is at 92%, fire the 90% alert (most urgent) — not the
    50% one. The loop must iterate thresholds in REVERSE order."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "reversed(_THRESHOLDS)" in src


def test_watcher_uses_worker_heartbeat():
    """The health endpoint's workers[] list surfaces stalled workers.
    Watcher must register so we can tell if IT dies (missing worker
    = missing forensics on the next outage)."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "WorkerHeartbeat" in src
    assert "pool_leak_watcher" in src  # its own name


def test_watcher_dump_has_same_shape_as_sigusr2():
    """Operator should be able to parse either dump interchangeably.
    Both call the same trace getters + iterate top_n oldest with app
    frames printed."""
    src = Path("app/monitoring/pool_leak_watcher.py").read_text()
    assert "get_async_session_trace" in src
    assert "get_pool_checkout_trace" in src
    assert "app_frames" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 7), (
        f"expected >= 5.21.7, got {major}.{minor}.{patch}"
    )
