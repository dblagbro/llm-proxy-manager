"""
Cost estimation per request.
Token prices are pulled from litellm's built-in model_cost dict,
with a manual override table for models litellm may not know yet.
"""
import logging
from typing import Optional

import litellm

logger = logging.getLogger(__name__)

# Manual overrides (USD per 1M tokens): {litellm_model: (input, output)}
_OVERRIDES: dict[str, tuple[float, float]] = {
    "anthropic/claude-sonnet-4-6": (3.0, 15.0),
    "anthropic/claude-opus-4-7": (15.0, 75.0),
    "anthropic/claude-haiku-4-5-20251001": (0.80, 4.0),
    "gemini/gemini-2.5-flash": (0.15, 0.60),
    "gemini/gemini-2.5-pro": (1.25, 10.0),
    "openai/gpt-4o": (2.50, 10.0),
    "openai/gpt-4o-mini": (0.15, 0.60),
    "xai/grok-2": (2.0, 10.0),
}


def estimate_cost_split(
    litellm_model: str, input_tokens: int, output_tokens: int,
) -> tuple[float, float]:
    """v3.4.0: returns ``(input_cost_usd, output_cost_usd)`` tuple.

    Splits the per-direction cost so callers (record_request, snapshot)
    can track / report input and output separately. ``estimate_cost()``
    sums the two for back-compat.

    Same lookup path as estimate_cost: litellm.cost_per_token first,
    fall back to the local override table. Override table values are
    in USD per million tokens (input_price, output_price).
    """
    # Try litellm's built-in cost lookup first
    try:
        in_cost, out_cost = litellm.cost_per_token(
            model=litellm_model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
        in_cost_f = float(in_cost)
        out_cost_f = float(out_cost)
        if in_cost_f > 0 or out_cost_f > 0:
            return (in_cost_f, out_cost_f)
    except Exception:
        pass

    # Manual override table — try as-is first, then strip a provider prefix,
    # then try the prefixed form for bare names.
    override = _OVERRIDES.get(litellm_model)
    if override is None and "/" in litellm_model:
        override = _OVERRIDES.get(litellm_model.split("/", 1)[1])
    if override is None:
        for k, v in _OVERRIDES.items():
            if k.endswith("/" + litellm_model):
                override = v
                break
    if override:
        in_price, out_price = override
        return (
            (input_tokens * in_price) / 1_000_000,
            (output_tokens * out_price) / 1_000_000,
        )

    # Unknown model: return zeros (free/local)
    return (0.0, 0.0)


def estimate_cost(litellm_model: str, input_tokens: int, output_tokens: int) -> float:
    """Returns total estimated cost in USD (input + output combined).

    v3.0.2: previous version called ``litellm.completion_cost(prompt_tokens=...,
    completion_tokens=...)`` — that kwargs signature was rejected by current
    litellm with TypeError, so every call silently returned 0 and the proxy
    reported $0.00 for every request. Switched to ``litellm.cost_per_token``
    which returns ``(input_cost, output_cost)`` already scaled by the token
    counts (the values are the totals, not per-token rates).

    v3.4.0: thin wrapper around ``estimate_cost_split()``. Existing
    callers keep working unchanged.
    """
    in_cost, out_cost = estimate_cost_split(litellm_model, input_tokens, output_tokens)
    return in_cost + out_cost


def format_cost(usd: float) -> str:
    if usd == 0:
        return "$0.00"
    if usd < 0.000001:
        return f"${usd:.8f}"
    if usd < 0.01:
        return f"${usd:.6f}"
    return f"${usd:.4f}"
