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
