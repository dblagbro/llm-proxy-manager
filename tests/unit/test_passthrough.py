"""Regression tests for app/api/oauth_capture/passthrough.py.

Caught in v2.6.3: the cap= strip code iterated a dict expecting
(key, value) tuples, which yielded only the keys. Every capture
request produced `ValueError: too many values to unpack`.

Keep the shape check small and specific — integration tests would
need a running sidecar, which we don't run in unit tests.
"""
from __future__ import annotations

import sys
import types

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

import inspect
from app.api.oauth_capture import passthrough


class TestStripCapQuery:
    """The cap= strip must NOT use `dict` iteration (that was the v2.6.3 bug)."""

    def test_query_pairs_name_present(self):
        # After v2.6.3 we store the filtered pairs as a list-of-tuples,
        # not a dict. This asserts the source still uses that shape.
        src = inspect.getsource(passthrough.capture_passthrough)
        assert "query_pairs = [" in src, (
            "passthrough.capture_passthrough must filter cap= into a list "
            "of tuples; iterating a dict yields only keys and that was the "
            "v2.6.3 ValueError."
        )
        assert "for k, v in query_pairs" in src, (
            "Unpack (k, v) must come from `query_pairs`, not a dict."
        )

    def test_no_dict_comprehension_for_query_strip(self):
        # Belt-and-suspenders — explicitly forbid the broken pattern.
        src = inspect.getsource(passthrough.capture_passthrough)
        assert (
            "query = {k: v for k, v in request.query_params.multi_items() if k != \"cap\"}"
            not in src
        ), "dict-based cap= strip was the v2.6.3 bug; don't re-introduce it."
