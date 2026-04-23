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


def record_ttft_sample(provider_id: str, ttft_ms: float) -> None:
    if ttft_ms <= 0:
        return
    buf = _ttft_samples.get(provider_id)
    if buf is None:
        buf = deque(maxlen=_WINDOW_SIZE)
        _ttft_samples[provider_id] = buf
    buf.append(ttft_ms)


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


async def race_streams(
    primary_factory,
    backup_factory,
    wait_ms: float,
) -> tuple[AsyncIterator[bytes], str]:
    """Start primary; if it doesn't emit a chunk within wait_ms, start backup.
    Return (winning_stream, winner_name) where winner is 'primary' or 'backup'.

    `primary_factory` and `backup_factory` are zero-arg callables that return
    the async iterator when invoked. They're not started until needed.
    """
    primary_iter = primary_factory()
    # Race the first chunk
    first_task = asyncio.create_task(_first_chunk(primary_iter))
    try:
        first = await asyncio.wait_for(asyncio.shield(first_task), timeout=wait_ms / 1000.0)
        # Primary won on its own
        return _replay(first, primary_iter), "primary"
    except asyncio.TimeoutError:
        pass

    # Primary slow — start backup
    backup_iter = backup_factory()
    backup_first = asyncio.create_task(_first_chunk(backup_iter))
    # Wait for either to produce
    done, pending = await asyncio.wait(
        {first_task, backup_first}, return_when=asyncio.FIRST_COMPLETED
    )
    if first_task in done and not first_task.exception():
        first = first_task.result()
        # Cancel backup
        backup_first.cancel()
        try:
            await backup_iter.aclose()
        except Exception:
            pass
        return _replay(first, primary_iter), "primary"
    else:
        # Backup won (or primary errored)
        try:
            backup_first_chunk = backup_first.result()
        except Exception as exc:
            # Both failed — fall back to propagating primary's error
            raise exc
        # Cancel primary
        first_task.cancel()
        try:
            await primary_iter.aclose()
        except Exception:
            pass
        return _replay(backup_first_chunk, backup_iter), "backup"


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
