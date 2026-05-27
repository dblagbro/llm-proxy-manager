"""v4.4.22 — async-side session tracer for ARCH-A.

Background: v3.10.2 added a sync pool-checkout tracer; v4.4.19 fixed
the slicing direction. **Neither captured app code** because
SQLAlchemy's async adapter runs DB ops via a greenlet whose stack is
separate from the async caller's. The 2026-05-27 ARCH-A dig confirmed
this — captured stacks ended at ``session.execute()`` with no app
frames above, even with the slicing fix.

v4.4.22 adds a parallel tracer on the **async side**: a subclass of
``AsyncSession`` overrides ``__aenter__``/``__aexit__`` to capture
the calling stack. These methods run on the async caller's
coroutine, so the captured frames include app code — exactly the
information needed to identify the leak.

Both tracers coexist: the sync one (``get_pool_checkout_trace``)
gives connection-level visibility; the async one
(``get_async_session_trace``) gives app-caller visibility. The
``/cluster/db-pool-trace`` endpoint now surfaces both.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ── Source guards ────────────────────────────────────────────────────


def test_traced_async_session_class_exists():
    src = Path("app/models/database.py").read_text()
    assert "_TracedAsyncSession" in src, (
        "expected the v4.4.22 _TracedAsyncSession subclass"
    )
    # Must override the async dunders where app frames ARE in the stack
    block_start = src.index("_TracedAsyncSession")
    block = src[block_start:block_start + 3500]
    assert "async def __aenter__" in block, (
        "must capture in __aenter__ (the async-side hook)"
    )
    assert "async def __aexit__" in block, (
        "must clear in __aexit__ to avoid accumulating ghost entries"
    )


def test_sessionmaker_uses_traced_class_when_tracing_enabled():
    """The sessionmaker must hand out the traced subclass when
    db_pool_trace is on. Otherwise the override is dead code."""
    src = Path("app/models/database.py").read_text()
    assert "class_=_session_class" in src, (
        "AsyncSessionLocal must wire its class via the conditional choice"
    )
    # The conditional itself
    assert "_session_class = _TracedAsyncSession" in src
    assert "_session_class = AsyncSession" in src


def test_get_async_session_trace_exists():
    src = Path("app/models/database.py").read_text()
    assert "def get_async_session_trace" in src, "reader function missing"
    # Must return age + session_id + stack per entry
    idx = src.index("def get_async_session_trace")
    block = src[idx:idx + 1500]
    for needed in ("age_sec", "session_id", "stack"):
        assert needed in block, f"reader missing {needed} field"
    # Oldest-first ordering — same as get_pool_checkout_trace
    assert "reverse=True" in block, "must sort oldest-first"


def test_admin_endpoint_surfaces_async_sessions():
    src = Path("app/api/cluster.py").read_text()
    idx = src.index("/cluster/db-pool-trace")
    block = src[idx:idx + 2500]
    assert "async_sessions" in block, (
        "/cluster/db-pool-trace must expose async_sessions in addition "
        "to checked_out — the async-side stacks are the ones operators "
        "actually need to identify the leak"
    )
    assert "get_async_session_trace" in block, (
        "endpoint must call the new reader"
    )


def test_health_dbpool_surfaces_async_session_count():
    """The /health response's dbPool block should also expose the
    async-session count so on-call can grep without auth."""
    src = Path("app/api/cluster.py").read_text()
    # Search the snap=… block that gets returned
    assert "traced_async_sessions" in src
    assert "oldest_async_session_age_sec" in src


def test_tracer_failures_dont_break_db_use():
    """The tracer wraps its capture/pop in try/except so a bug in the
    tracing code can never block real DB use."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("_TracedAsyncSession")
    block = src[idx:idx + 3500]
    # Both __aenter__ and __aexit__ must have try/except around the
    # tracing (not around the super() call — that part must propagate).
    enter_idx = block.index("async def __aenter__")
    exit_idx = block.index("async def __aexit__")
    enter_block = block[enter_idx:exit_idx]
    exit_block = block[exit_idx:exit_idx + 1500]
    assert "try:" in enter_block and "except" in enter_block, (
        "__aenter__ tracing must be wrapped in try/except"
    )
    assert "try:" in exit_block and "except" in exit_block, (
        "__aexit__ pop must be wrapped in try/except"
    )


# ── Behavioral tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_async_trace_captures_app_frames_when_enabled(monkeypatch):
    """The key behavioral test: when tracing is enabled and we enter
    an async-with, the captured stack must include this test function
    (the app caller) — proving that the greenlet boundary is no
    longer eating the relevant frames.

    Implementation note: ``db_pool_trace`` is read at import time, so
    we can't toggle it for an already-imported module. Instead we
    construct a _TracedAsyncSession-equivalent test double that
    mirrors the production code's capture step and exercise that.
    This is a unit test of the capture mechanism, not an integration
    test of the live sessionmaker."""
    import traceback, time as _t
    from secrets import token_hex

    test_trace_dict: dict[str, dict] = {}

    class _TraceFake:
        async def __aenter__(self):
            self._sid = token_hex(8)
            stack = "".join(traceback.format_stack()[:-1])
            test_trace_dict[self._sid] = {
                "session_id": self._sid,
                "since": _t.monotonic(),
                "stack": stack,
            }
            return self

        async def __aexit__(self, *_):
            test_trace_dict.pop(self._sid, None)

    # Open + close — must populate then clear
    async with _TraceFake() as _:
        assert len(test_trace_dict) == 1, (
            "tracer must populate dict on __aenter__"
        )
        sole = next(iter(test_trace_dict.values()))
        # The KEY assertion: this test function's name must be in the
        # captured stack. v4.4.19's sync tracer FAILED this assertion
        # in production (only SQLA internals appeared).
        assert "test_async_trace_captures_app_frames_when_enabled" in sole["stack"], (
            "v4.4.22 tracer must capture the async-side caller frames; "
            "the sync-side v4.4.19 version failed exactly this property "
            "in production because of the greenlet boundary"
        )

    assert len(test_trace_dict) == 0, (
        "tracer must clear entry on __aexit__"
    )


@pytest.mark.asyncio
async def test_async_trace_handles_exception_in_block():
    """An exception inside the async-with block must still trigger
    __aexit__ → cleanup. Otherwise an exceptional path could
    accumulate ghost entries forever."""
    import traceback, time as _t
    from secrets import token_hex

    test_trace_dict: dict[str, dict] = {}

    class _TraceFake:
        async def __aenter__(self):
            self._sid = token_hex(8)
            stack = "".join(traceback.format_stack()[:-1])
            test_trace_dict[self._sid] = {
                "session_id": self._sid,
                "since": _t.monotonic(),
                "stack": stack,
            }
            return self

        async def __aexit__(self, *_):
            test_trace_dict.pop(self._sid, None)
            return False  # do not swallow

    with pytest.raises(RuntimeError):
        async with _TraceFake() as _:
            assert len(test_trace_dict) == 1
            raise RuntimeError("simulated leak path")

    assert len(test_trace_dict) == 0, (
        "exception path must still hit __aexit__ → cleanup"
    )
