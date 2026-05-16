"""Hedged requests — Google "Tail at Scale" pattern (Dean & Barroso, CACM 2013).

If the primary's first-token latency exceeds that provider's own recent p95,
fire a backup request to the next-ranked healthy provider. Return whichever
stream emits its first chunk first; cancel the other.

Guardrails:
- Token bucket limits hedges to settings.hedge_max_per_sec (default 5/sec)
  so a full primary-provider outage doesn't 2× upstream load.
- Opt-in only: request header X-Hedge: on OR LMRH hedge=on.
- Only streaming requests hedge; non-streaming is cheaper to just retry.
"""
import asyncio
import logging
import time
from collections import deque
from statistics import quantiles
from typing import AsyncIterator, Optional

from app.config import settings

logger = logging.getLogger(__name__)


_WINDOW_SIZE = 200  # samples per provider
_MIN_SAMPLES = 20   # below this, p95 is too noisy to act on

_ttft_samples: dict[str, deque[float]] = {}

# PeakEWMA (Wave 3 #13) — exponentially-weighted moving average per provider.
# Lambda chosen to give roughly equal weight to the most recent ~10 samples.
# Finagle's "peak" variant: on each sample, we take max(ewma_after, ewma_before)
# to bias toward the *recent peak*, which is what matters for tail latency.
_PEAK_EWMA_LAMBDA = 0.2
_peak_ewma_ms: dict[str, float] = {}


def peak_ewma(provider_id: str) -> float | None:
    """Return the PeakEWMA for a provider, or None if never sampled."""
    return _peak_ewma_ms.get(provider_id)


def record_ttft_sample(provider_id: str, ttft_ms: float) -> None:
    if ttft_ms <= 0:
        return
    buf = _ttft_samples.get(provider_id)
    if buf is None:
        buf = deque(maxlen=_WINDOW_SIZE)
        _ttft_samples[provider_id] = buf
    buf.append(ttft_ms)
    # PeakEWMA update
    prev = _peak_ewma_ms.get(provider_id)
    if prev is None:
        _peak_ewma_ms[provider_id] = ttft_ms
    else:
        updated = prev * (1.0 - _PEAK_EWMA_LAMBDA) + ttft_ms * _PEAK_EWMA_LAMBDA
        # "Peak" variant — recent spike stays sticky briefly
        _peak_ewma_ms[provider_id] = max(prev, updated) if ttft_ms > prev else updated


def provider_p95_ms(provider_id: str) -> Optional[float]:
    buf = _ttft_samples.get(provider_id)
    if buf is None or len(buf) < _MIN_SAMPLES:
        return None
    samples = sorted(buf)
    # quantiles with n=20 gives the 5th/10th/.../95th percentile
    qs = quantiles(samples, n=20, method="inclusive")
    return qs[-1]  # 95th percentile


# ── Token bucket ─────────────────────────────────────────────────────────────

_bucket_tokens: float = 0.0
_bucket_last_refill: float = 0.0
_bucket_lock = asyncio.Lock()


async def _try_consume_hedge_token() -> bool:
    """Single global bucket. Default 5 tokens/sec burst 5."""
    global _bucket_tokens, _bucket_last_refill
    max_rate = float(getattr(settings, "hedge_max_per_sec", 5))
    if max_rate <= 0:
        return False
    async with _bucket_lock:
        now = time.monotonic()
        if _bucket_last_refill == 0:
            _bucket_tokens = max_rate
            _bucket_last_refill = now
        else:
            elapsed = now - _bucket_last_refill
            _bucket_tokens = min(max_rate, _bucket_tokens + elapsed * max_rate)
            _bucket_last_refill = now
        if _bucket_tokens >= 1.0:
            _bucket_tokens -= 1.0
            return True
        return False


def should_hedge_header(hedge_header: Optional[str], lmrh_hedge: Optional[str]) -> bool:
    if hedge_header and hedge_header.lower() in ("on", "true", "1"):
        return True
    if lmrh_hedge and lmrh_hedge.lower() == "on":
        return True
    return False


def wait_budget_ms(provider_id: str) -> Optional[float]:
    """How long to wait before firing the backup. None = don't hedge (no signal)."""
    p95 = provider_p95_ms(provider_id)
    if p95 is None:
        return None
    # Fire backup at 1.2 × p95 — give the primary room but cap the tail
    return p95 * 1.2


# ── Hedged streamer ──────────────────────────────────────────────────────────


def _chunk_ok(chunk: Optional[bytes]) -> bool:
    """A streamed first chunk is "healthy" — a genuine race win — only if
    it exists and is not a terminal SSE error frame. An empty stream or a
    pre-stream error frame counts as a FAILURE, not a win."""
    if chunk is None:
        return False
    try:
        from app.api._messages_streaming import _sse_frame_error
        return _sse_frame_error(chunk) is None
    except Exception:
        # Detector unavailable — treat as healthy (never regress to worse
        # than the pre-v3.10.17 first-to-yield behaviour).
        return True


async def _safe_aclose(it) -> None:
    try:
        await it.aclose()
    except Exception:
        pass


async def race_streams(
    primary_factory,
    backup_factory,
    wait_ms: float,
) -> tuple[AsyncIterator[bytes], str]:
    """Start primary; if it doesn't emit a chunk within wait_ms, start backup.
    Return (winning_stream, winner_name) where winner is 'primary' or 'backup'.

    `primary_factory` and `backup_factory` are zero-arg callables that return
    the async iterator when invoked. They're not started until needed.

    v3.10.17 — a stream "wins" only if its first chunk is *healthy*. A
    first chunk that is a terminal SSE error frame (an upstream that
    fast-failed pre-stream), or an empty stream, counts as a FAILURE — so
    a fast-failing primary no longer beats a healthy backup in the race.
    If both streams fail, primary's failed stream is returned so the
    caller's ``preflight_sse`` surfaces it as a real HTTP status.
    """
    primary_iter = primary_factory()
    first_task = asyncio.create_task(_first_chunk(primary_iter))
    primary_first: Optional[bytes] = None
    primary_settled = False
    try:
        primary_first = await asyncio.wait_for(
            asyncio.shield(first_task), timeout=wait_ms / 1000.0
        )
        primary_settled = True
        if _chunk_ok(primary_first):
            # Primary won on its own with a healthy first chunk.
            return _replay(primary_first, primary_iter), "primary"
        # Primary produced an error frame / empty stream — it failed;
        # fall through and give the backup a chance.
    except asyncio.TimeoutError:
        pass  # primary slow — race the backup

    backup_iter = backup_factory()
    backup_task = asyncio.create_task(_first_chunk(backup_iter))

    if primary_settled:
        # Primary already finished and failed; the backup is the only
        # remaining candidate — await it explicitly (no race, no spin).
        try:
            backup_first = await backup_task
        except Exception:
            backup_first = None
        if _chunk_ok(backup_first):
            await _safe_aclose(primary_iter)
            return _replay(backup_first, backup_iter), "backup"
        await _safe_aclose(backup_iter)
        return _replay(primary_first, primary_iter), "primary"

    # Primary was slow (timed out, not failed yet) — race both for the
    # first HEALTHY chunk.
    await asyncio.wait({first_task, backup_task}, return_when=asyncio.FIRST_COMPLETED)

    def _task_chunk(task) -> Optional[bytes]:
        if task.done() and not task.cancelled() and task.exception() is None:
            return task.result()
        return None

    pf = _task_chunk(first_task)
    bf = _task_chunk(backup_task)
    if _chunk_ok(pf):
        backup_task.cancel()
        await _safe_aclose(backup_iter)
        return _replay(pf, primary_iter), "primary"
    if _chunk_ok(bf):
        first_task.cancel()
        await _safe_aclose(primary_iter)
        return _replay(bf, backup_iter), "backup"

    # The first-completed stream failed — await whichever is still pending.
    if not first_task.done():
        try:
            pf = await first_task
        except Exception:
            pf = None
        if _chunk_ok(pf):
            backup_task.cancel()
            await _safe_aclose(backup_iter)
            return _replay(pf, primary_iter), "primary"
    if not backup_task.done():
        try:
            bf = await backup_task
        except Exception:
            bf = None
        if _chunk_ok(bf):
            first_task.cancel()
            await _safe_aclose(primary_iter)
            return _replay(bf, backup_iter), "backup"

    # Both streams failed. Return primary's outcome (its error frame, if
    # any) so the caller's preflight_sse surfaces a real HTTP status.
    if pf is not None:
        await _safe_aclose(backup_iter)
        return _replay(pf, primary_iter), "primary"
    if bf is not None:
        await _safe_aclose(primary_iter)
        return _replay(bf, backup_iter), "backup"
    if first_task.done() and first_task.exception():
        raise first_task.exception()
    if backup_task.done() and backup_task.exception():
        raise backup_task.exception()
    raise RuntimeError("hedge race produced no stream")


async def _first_chunk(stream: AsyncIterator[bytes]):
    async for chunk in stream:
        return chunk
    return None


async def _replay(first: Optional[bytes], rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-yield the already-consumed first chunk, then the rest of the stream."""
    if first is not None:
        yield first
    async for chunk in rest:
        yield chunk


async def try_acquire_hedge() -> bool:
    """Public wrapper for callers that want to gate on the bucket before starting backup work."""
    return await _try_consume_hedge_token()
