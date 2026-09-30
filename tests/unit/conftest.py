"""Unit-test conftest.

Sets DATABASE_URL to a tempdir path BEFORE any app module is imported,
so unit tests that exercise the real DB (R2 worker chaos tests) don't
try to open ``/app/data/llmproxy.db`` (the production-container path).

Pytest imports conftest.py at collection time, before any test module
is imported — so ``app.config.settings`` constructs against this env
var when first referenced.
"""
import os
import tempfile

import pytest

# Only override if the caller hasn't already set it (e.g. CI matrix runs)
_default_test_db = os.path.join(tempfile.gettempdir(), "llmproxy-unit-test.db")
os.environ.setdefault("DATABASE_URL", f"sqlite+aiosqlite:///{_default_test_db}")

# v5.22.32 — import the REAL litellm here, for the same
# collection-order reason as DATABASE_URL above.
#
# 26 unit-test modules open with a variant of:
#
#     sys.modules.setdefault("litellm", types.ModuleType("litellm"))
#
# meant to keep the app importable when litellm isn't installed. But
# `setdefault` fires whenever litellm merely hasn't been imported *yet* —
# and pytest imports every test module at collection time, so in a full-suite
# run whichever stubber sorts first installed a bare stub (usually
# `test_aliases.py`) and the entire session then ran against a fake litellm.
# The stub carries only `RateLimitError`, so e.g. `litellm.cost_per_token`
# raised AttributeError into `estimate_cost_split`'s `except Exception: pass`
# and every cost silently became $0.00 — four pricing tests were parked in
# known_failures.txt for exactly this, passing in isolation and failing in
# the suite.
#
# litellm is a hard dependency (pyproject + requirements), so importing it
# here makes those 26 `setdefault` calls the no-ops they were always meant
# to be, while the fallback still works if it genuinely isn't installed.
try:  # pragma: no cover - import-order guard, not logic
    import litellm  # noqa: F401
except ImportError:  # litellm genuinely absent: leave the stubs to it
    pass

# ── Dispose test engines before their event loop closes ──────────────────────
#
# v5.22.39 — a GREEN CI run carried **20 annotations at level `failure`**, every
# one ``RuntimeError: Event loop is closed``. GitHub renders those as failures in
# the run summary and in the notification email, so a passing build looked
# broken. Reproduced locally: 16 occurrences.
#
# Cause: aiosqlite runs each connection on a worker thread that talks to the
# event loop through ``call_soon_threadsafe``. Eleven test modules create an
# ``AsyncEngine`` on ``sqlite+aiosqlite:///:memory:`` and never ``dispose()`` it,
# so the pooled connection and its thread outlive the test. When pytest-asyncio
# closes that test's loop, the thread's next ``call_soon_threadsafe`` hits
# ``_check_closed`` and raises. The tests themselves had already passed.
#
# Fixed once here rather than in eleven fixtures, because the twelfth would
# reintroduce it. (``NullPool`` looked like a tidier fix and is wrong: a
# ``:memory:`` database lives inside its connection, so discarding connections
# discards the schema — measured 85 failures and 41 errors.)
#
# Teardown ordering is what makes this work: pytest-asyncio's loop fixture is set
# up before this one, so it is torn down *after* it, and the loop is still open
# while we dispose.
# (engine, loop_it_was_created_on) — the loop matters, see the teardown below.
_TEST_ENGINES: list = []


def _track_async_engines() -> None:
    import asyncio

    import sqlalchemy.ext.asyncio as _sa_asyncio

    _real_create = _sa_asyncio.create_async_engine
    if getattr(_real_create, "_llmproxy_tracking_wrapper", False):
        return  # conftest re-imported

    def _create_async_engine(url, *args, **kwargs):
        engine = _real_create(url, *args, **kwargs)
        # Remember the loop it was built on; that is the one its aiosqlite
        # threads will call back into, and the only one that can dispose it.
        # Kept in the tuple rather than set on the engine: AsyncEngine rejects
        # arbitrary attributes (measured: AttributeError on 23 modules).
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        _TEST_ENGINES.append((engine, loop))
        return engine

    _create_async_engine._llmproxy_tracking_wrapper = True
    # Patched on the module before any test module is imported, so a test doing
    # ``from sqlalchemy.ext.asyncio import create_async_engine`` binds this.
    _sa_asyncio.create_async_engine = _create_async_engine


_track_async_engines()


@pytest.fixture(autouse=True)
async def _dispose_engines_created_by_this_test():
    """Dispose every AsyncEngine the test created, while its loop is still open.

    ASYNC on purpose. A sync fixture's teardown runs after pytest-asyncio has
    already closed the loop, so ``dispose()`` could not run and the synchronous
    pool fallback merely provoked *more* close attempts against the dead loop —
    measured 24 errors, up from 16. An async fixture's teardown resumes inside
    the still-open loop, which is the only place an aiosqlite connection can be
    closed cleanly.

    ``asyncio_mode = "auto"`` (pyproject.toml) is what lets an autouse async
    fixture apply here.
    """
    _TEST_ENGINES.clear()
    yield
    for engine, _loop in list(_TEST_ENGINES):
        try:
            await engine.dispose()
        except Exception:
            # Teardown must never turn a passing test red.
            pass
    _TEST_ENGINES.clear()
