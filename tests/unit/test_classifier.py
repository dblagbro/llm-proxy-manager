"""Unit tests for prompt task classifier (Wave 3 #15)."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
sys.modules.setdefault("litellm.exceptions", _stub)

from app.routing.classifier import _mean, _cosine


class TestMean:
    def test_single_vector(self):
        assert _mean([[1.0, 2.0, 3.0]]) == [1.0, 2.0, 3.0]

    def test_two_vectors(self):
        out = _mean([[1.0, 2.0], [3.0, 4.0]])
        assert out == [2.0, 3.0]

    def test_empty(self):
        assert _mean([]) == []


class TestCosine:
    def test_identical(self):
        v = [1.0, 2.0, 3.0]
        assert abs(_cosine(v, v) - 1.0) < 1e-9

    def test_orthogonal(self):
        assert abs(_cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_opposite(self):
        assert _cosine([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_zero_vector(self):
        assert _cosine([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_length_mismatch(self):
        assert _cosine([1.0], [1.0, 2.0]) == 0.0

    def test_empty_inputs(self):
        assert _cosine([], [1.0]) == 0.0
        assert _cosine([1.0], []) == 0.0
