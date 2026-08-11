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

    if current is not None and current.provider.provider_type in ("claude-oauth", "ChatGPT-oauth-plan"):
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
    """Ask select_provider for the best candidate that is not already tried.

    v2.8.8: skips claude-oauth providers — those use a different auth
    method (Bearer + CC beta flags) and aren't reachable through the
    litellm-based call_fn the fallback chain uses. They're handled by the
    OAuth dispatch in messages.py / completions.py BEFORE the chain runs.

    v5.22.6 (BUG: production wedge): this used to pass a SINGLE
    ``exclude_provider_id`` seed taken as ``next(iter(extended_excluded))``
    and, when select_provider handed back an already-tried provider, "made
    progress" via ``extended_excluded.add(candidate.provider.id)`` — a no-op,
    because that id was already in the set. The seed was then recomputed
    identically and the loop spun forever, each pass calling select_provider
    (which runs ``_load_profile`` — 2 queries per provider). One request that
    hit a provider error was enough to peg the event loop and drain the DB
    pool to 50/50 on an otherwise idle node; it never recovered.

    select_provider has accepted a cumulative ``exclude_provider_ids`` set
    since v5.7.13 (added for empty-success failover, whose comment already
    notes single-exclude "cannot escape a ping-pong"). Use it: one call, the
    full exclusion set, no chaining. Termination is now structural — every
    iteration either returns, raises, or adds an id select_provider could not
    have returned before, and the set is bounded by the provider count.
    """
    # Pinned routes have no fallback — one provider only
    if pinned_provider_id:
        raise RuntimeError("pinned provider has no fallback candidates")

    extended_excluded = set(tried_ids)
    # Preserved from the original: an empty exclusion set means the caller
    # has not actually tried anything, which is not a state the fallback
    # chain reaches (it adds to `tried` before the first call_fn).
    if not extended_excluded:
        raise RuntimeError("no untried candidate remains")

    while True:
        try:
            candidate = await select_provider(
                db, hint, has_tools=has_tools, has_images=has_images,
                key_type=key_type, pinned_provider_id=None,
                model_override=model_override,
                exclude_provider_ids=extended_excluded,
            )
        except RuntimeError:
            raise RuntimeError("no untried candidate remains")

        # Defensive: select_provider filters the exclusion set itself, so this
        # cannot normally fire. If its contract ever changes, FAIL rather than
        # spin — an infinite loop here is what took production down.
        if candidate.provider.id in extended_excluded:
            raise RuntimeError(
                "select_provider returned an excluded provider "
                f"({candidate.provider.id}); refusing to loop"
            )

        if candidate.provider.provider_type in ("claude-oauth", "ChatGPT-oauth-plan"):
            # OAuth-based providers can't go through litellm; skip them in
            # the fallback chain. Add to excluded so the next pick is fresh.
            extended_excluded.add(candidate.provider.id)
            continue
        return candidate
