"""Unit tests for cost estimation."""
import pytest
from app.monitoring.pricing import estimate_cost, format_cost


def test_known_model_override():
    cost = estimate_cost("openai/gpt-4o", 1000, 500)
    # $2.50/M input + $10.00/M output
    expected = (1000 * 2.50 + 500 * 10.0) / 1_000_000
    assert abs(cost - expected) < 0.000001


def test_unknown_model_returns_zero():
    cost = estimate_cost("ollama/llama3-local", 1000, 1000)
    assert cost == 0.0


def test_format_cost_small():
    assert format_cost(0.0) == "$0.00"
    s = format_cost(0.000123)
    assert s.startswith("$")


def test_format_cost_normal():
    assert format_cost(1.2345) == "$1.2345"
