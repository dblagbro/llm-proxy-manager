"""Ordered fallback across ranked providers (Wave 3 #17).

When the primary provider fails with a non-retriable error (auth, 502, DNS,
context-length, etc.), try the next-best candidate instead of returning the
error. Each provider gets its own attempt budget.

Streaming is handled by hedged requests (Wave 1 #4) — this module is for
non-streaming paths only. Once a stream has started we can't fall back
without breaking the SSE contract.
"""
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.routing.router import select_provider, RouteResult
from app.routing.lmrh import LMRHHint

logger = logging.getLogger(__name__)


# Errors that are retriable on the SAME provider (litellm retry handles them).
# Anything NOT on this list triggers fallback to the next candidate.
_SAME_PROVIDER_RETRIABLE_PREFIXES = (
    "litellm.Timeout",
    "litellm.APIConnectionError",   # transient
    "litellm.InternalServerError",  # 5xx upstream
    "litellm.RateLimitError",       # 429 — retry.py already backs off
)


def is_same_provider_retriable(exc: Exception) -> bool:
    """True if the error should be retried on the same provider (already done
    upstream by retry.py); False means we should fall back to next candidate."""
    msg = str(exc)
    for prefix in _SAME_PROVIDER_RETRIABLE_PREFIXES:
        if prefix in msg:
            return True
    return False


@dataclass
class FallbackChain:
    """Track what was tried so we can expose it via response header."""
    attempts: list[str] = field(default_factory=list)

    def add(self, provider_name: str, outcome: str) -> None:
        self.attempts.append(f"{provider_name}:{outcome}")

    def as_header(self) -> str:
        return ",".join(self.attempts)


async def try_ranked_non_streaming(
    db: AsyncSession,
    hint: Optional[LMRHHint],
    *,
    has_tools: bool,
    has_images: bool,
    key_type: str,
    pinned_provider_id: Optional[str],
    model_override: Optional[str],
    primary_route: RouteResult,
    call_fn: Callable,
    max_providers: Optional[int] = None,
) -> tuple[object, RouteResult, FallbackChain]:
    """
    Run `call_fn(route)` against the primary; on non-retriable failure, fall
    through to the next-ranked provider. Returns (result, final_route, chain).
    Raises the LAST exception if all candidates exhaust.

    `call_fn` must be an async callable accepting a RouteResult and returning
    the litellm response object.
    """
    chain = FallbackChain()
    cap = max_providers if max_providers is not None else getattr(
        settings, "fallback_max_providers", 3
    )

    # v2.8.8: refuse to run a claude-oauth provider through the litellm chain.
    # If the primary route IS oauth, skip directly to the next eligible
    # provider — the dispatch layer was supposed to handle this and clearly
    # didn't, but the chain isn't the place to recover from auth-mismatch.
    current = primary_route
    tried: set[str] = set()
    last_exc: Optional[Exception] = None

    if current is not None and current.provider.provider_type in ("claude-oauth", "codex-oauth"):
        tried.add(current.provider.id)
        chain.add(current.provider.name, "skip:oauth-not-via-litellm")
        try:
            current = await _next_route(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_type, pinned_provider_id=pinned_provider_id,
                model_override=model_override, tried_ids=tried,
            )
        except Exception:
            current = None

    while current is not None:
        tried.add(current.provider.id)
        try:
            result = await call_fn(current)
            chain.add(current.provider.name, "ok")
            return result, current, chain
        except Exception as exc:
            last_exc = exc
            if is_same_provider_retriable(exc):
                # Retry.py already exhausted same-provider retries for these.
                chain.add(current.provider.name, "retry-exhausted")
            else:
                chain.add(current.provider.name, f"err:{type(exc).__name__}")
            # Fall through to next candidate
            if len(tried) >= cap:
                break

        # Pick the next-best provider, excluding anyone already attempted.
        try:
            current = await _next_route(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_type, pinned_provider_id=pinned_provider_id,
                model_override=model_override, tried_ids=tried,
            )
        except Exception as sel_exc:
            logger.info("fallback.no_more_candidates %s", sel_exc)
            current = None

    # All candidates exhausted
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("fallback chain exhausted with no exception captured")


async def _next_route(
    db: AsyncSession,
    hint: Optional[LMRHHint],
    *,
    has_tools: bool,
    has_images: bool,
    key_type: str,
    pinned_provider_id: Optional[str],
    model_override: Optional[str],
    tried_ids: set[str],
) -> RouteResult:
    """select_provider supports one exclude at a time; chain it by calling
    once per already-tried provider until a new one is returned.

    v2.8.8: skips claude-oauth providers — those use a different auth
    method (Bearer + CC beta flags) and aren't reachable through the
    litellm-based call_fn the fallback chain uses. They're handled by the
    OAuth dispatch in messages.py / completions.py BEFORE the chain runs.
    """
    # Pinned routes have no fallback — one provider only
    if pinned_provider_id:
        raise RuntimeError("pinned provider has no fallback candidates")

    # Keep excluding most-recent tried until select_provider returns a fresh
    # NON-claude-oauth candidate.
    extended_excluded = set(tried_ids)
    while True:
        seed = next(iter(extended_excluded), None)
        if seed is None:
            raise RuntimeError("no untried candidate remains")
        try:
            candidate = await select_provider(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_type, pinned_provider_id=None,
                model_override=model_override, exclude_provider_id=seed,
            )
        except RuntimeError:
            raise RuntimeError("no untried candidate remains")
        if candidate.provider.id in extended_excluded:
            extended_excluded.add(candidate.provider.id)
            continue
        if candidate.provider.provider_type in ("claude-oauth", "codex-oauth"):
            # OAuth-based providers can't go through litellm; skip them in
            # the fallback chain. Add to excluded so the next pick is fresh.
            extended_excluded.add(candidate.provider.id)
            continue
        return candidate
