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
