"""
M6 — upstream 429 retry with exponential backoff + jitter.

Wraps litellm.acompletion: on RateLimitError, reads Retry-After header,
waits (capped at 30 s), and retries up to max_retries times.
Billing errors are never retried — caller's record_outcome handles CB trip.
"""
import asyncio
import logging
import random

import litellm
from litellm import RateLimitError

from app.routing.circuit_breaker import is_billing_error

logger = logging.getLogger(__name__)

_MAX_WAIT_SEC = 30.0


async def acompletion_with_retry(
    model: str,
    messages: list,
    max_retries: int = 3,
    **kwargs,
):
    for attempt in range(max_retries + 1):
        try:
            return await litellm.acompletion(model=model, messages=messages, **kwargs)
        except RateLimitError as exc:
            if is_billing_error(str(exc)):
                raise
            if attempt >= max_retries:
                raise
            retry_after = _parse_retry_after(exc)
            wait = _backoff(attempt, retry_after)
            logger.warning(
                "upstream_rate_limit",
                extra={"model": model, "attempt": attempt + 1, "wait_sec": round(wait, 1)},
            )
            await asyncio.sleep(wait)


def _parse_retry_after(exc: RateLimitError) -> float:
    response = getattr(exc, "response", None)
    if response is not None:
        ra = response.headers.get("Retry-After") or response.headers.get("retry-after")
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                pass
    return 5.0  # default when header is absent


def _backoff(attempt: int, retry_after: float) -> float:
    exp = min(retry_after, (2 ** attempt) * 2.0)
    jitter = random.uniform(0, exp * 0.25)
    return min(exp + jitter, _MAX_WAIT_SEC)
