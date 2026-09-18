"""Per-request token accounting for the CoT pipeline's internal calls.

Separate from ``pipeline.py`` deliberately. This is pure bookkeeping with no
litellm dependency, so it can be imported — and tested — without dragging in
the provider stack. ``pipeline.py`` imports litellm at module scope, which
makes it unimportable in any test session where another test has stubbed
``litellm`` in ``sys.modules``.

Why it exists
-------------
Each CoT iteration issues its own upstream completion. Those bill real tokens
but are never streamed to the client, so they produce no ``message_delta`` and
the metrics wrapper in ``_messages_streaming.py`` never saw them. That wrapper
also *assigns* rather than accumulates, so what reached ``provider_metrics``
was the final answer's usage and nothing else.

Measured against the vendor dashboard for one key over Sep 09-16 2026:
**4,354,580 input tokens billed vs 2,629,493 recorded** — 40% low, with spend
37% low for the month. Per-key spending caps were therefore unenforceable: a
cap checked against a 40%-low count lets real spend run well past it.

A ContextVar keeps the accumulator request-scoped without threading an extra
argument through every call site, and is correct under asyncio concurrency —
each request task gets its own copy.
"""

import contextvars
import logging

logger = logging.getLogger(__name__)

_cot_usage: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "cot_usage", default=None
)


def reset_cot_usage() -> None:
    """Start a fresh accumulator for this request."""
    _cot_usage.set({"input_tokens": 0, "output_tokens": 0, "calls": 0})


def get_cot_usage() -> dict:
    """Tokens consumed by internal CoT calls during this request (a copy)."""
    return dict(_cot_usage.get() or {"input_tokens": 0, "output_tokens": 0, "calls": 0})


def accumulate_usage(resp) -> None:
    """Fold one upstream response's usage into this request's accumulator."""
    acc = _cot_usage.get()
    if acc is None:
        return
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    # litellm normalises to the OpenAI shape, but fall back across spellings
    # rather than silently recording zero for a provider that differs.
    prompt = getattr(usage, "prompt_tokens", None) or getattr(usage, "input_tokens", None) or 0
    completion = (
        getattr(usage, "completion_tokens", None) or getattr(usage, "output_tokens", None) or 0
    )
    acc["input_tokens"] += int(prompt or 0)
    acc["output_tokens"] += int(completion or 0)
    acc["calls"] += 1


def fold_cot_usage(in_tok: int, out_tok: int, provider_id: str = "") -> tuple[int, int]:
    """Add this request's internal CoT usage to a caller's totals.

    Callers ASSIGN their own totals from ``message_delta`` (correct — usage
    there is cumulative for one message). Internal critique calls are
    ADDITIONAL upstream completions, so they add on top rather than overwrite.
    """
    usage = get_cot_usage()
    if not usage["calls"]:
        return in_tok, out_tok
    logger.info(
        "cot.usage_folded_in provider=%s calls=%d in_tok=%d out_tok=%d",
        provider_id, usage["calls"], usage["input_tokens"], usage["output_tokens"],
    )
    return in_tok + usage["input_tokens"], out_tok + usage["output_tokens"]
