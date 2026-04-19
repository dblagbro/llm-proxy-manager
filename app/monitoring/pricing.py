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


def estimate_cost(litellm_model: str, input_tokens: int, output_tokens: int) -> float:
    """Returns estimated cost in USD."""
    # Try litellm's built-in cost lookup first
    try:
        cost = litellm.completion_cost(
            model=litellm_model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
        if cost is not None and cost > 0:
            return float(cost)
    except Exception:
        pass

    # Manual override table
    override = _OVERRIDES.get(litellm_model)
    if override:
        in_price, out_price = override
        return (input_tokens * in_price + output_tokens * out_price) / 1_000_000

    # Unknown model: return 0 (free/local)
    return 0.0


def format_cost(usd: float) -> str:
    if usd == 0:
        return "$0.00"
    if usd < 0.000001:
        return f"${usd:.8f}"
    if usd < 0.01:
        return f"${usd:.6f}"
    return f"${usd:.4f}"
