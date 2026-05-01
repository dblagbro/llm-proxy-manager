"""
Circuit breaker + hold-down timer per provider.
State is stored in Redis when available; falls back to in-process dict.
"""
import asyncio
import time
import logging
from enum import Enum
from typing import Optional
from dataclasses import dataclass, field

from app.config import settings

logger = logging.getLogger(__name__)

_REDIS_PREFIX = "llmproxy:cb:"


class CBState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"


@dataclass
class _LocalState:
    state: CBState = CBState.CLOSED
    failures: int = 0
    successes: int = 0
    opened_at: float = 0.0
    hold_down_until: float = 0.0


_local_states: dict[str, _LocalState] = {}
_lock = asyncio.Lock()
_provider_overrides: dict[str, dict] = {}  # provider_id → {hold_down_sec, failure_threshold}


def set_provider_config(provider_id: str, hold_down_sec: Optional[int], failure_threshold: Optional[int]):
    _provider_overrides[provider_id] = {
        "hold_down_sec": hold_down_sec,
        "failure_threshold": failure_threshold,
    }


def _hold_down_sec(provider_id: str) -> int:
    return _provider_overrides.get(provider_id, {}).get("hold_down_sec") or settings.hold_down_sec


def _failure_threshold(provider_id: str) -> int:
    return _provider_overrides.get(provider_id, {}).get("failure_threshold") or settings.circuit_breaker_threshold


def _get_local(provider_id: str) -> _LocalState:
    if provider_id not in _local_states:
        _local_states[provider_id] = _LocalState()
    return _local_states[provider_id]


async def get_state(provider_id: str) -> CBState:
    s = _get_local(provider_id)
    now = time.time()
    if s.state == CBState.OPEN:
        if now >= s.opened_at + settings.circuit_breaker_timeout_sec:
            async with _lock:
                s.state = CBState.HALF_OPEN
                s.successes = 0
                _export_gauge(provider_id, s.state)
    return s.state


async def is_available(provider_id: str) -> bool:
    s = _get_local(provider_id)
    now = time.time()
    if now < s.hold_down_until:
        return False
    state = await get_state(provider_id)
    return state != CBState.OPEN


def _export_gauge(provider_id: str, state: "CBState") -> None:
    # Prometheus gauge — local import keeps CB independent of observability in tests
    try:
        from app.observability.prometheus import observe_circuit_breaker_state
        observe_circuit_breaker_state(provider_id, state.value)
    except Exception:
        pass


async def record_success(provider_id: str):
    async with _lock:
        s = _get_local(provider_id)
        if s.state == CBState.HALF_OPEN:
            s.successes += 1
            if s.successes >= settings.circuit_breaker_success_needed:
                s.state = CBState.CLOSED
                s.failures = 0
                s.successes = 0
                logger.info("circuit_breaker.closed", extra={"provider": provider_id})
                _export_gauge(provider_id, s.state)
        elif s.state == CBState.CLOSED:
            s.failures = max(0, s.failures - 1)


async def record_failure(provider_id: str, billing_error: bool = False):
    async with _lock:
        s = _get_local(provider_id)
        s.failures += 1
        now = time.time()

        # Billing errors immediately open the breaker and set a long hold-down
        if billing_error:
            s.state = CBState.OPEN
            s.opened_at = now
            s.hold_down_until = now + 3600  # 1-hour hold for billing errors
            logger.warning("circuit_breaker.billing_error", extra={"provider": provider_id})
            _export_gauge(provider_id, s.state)
            return

        if s.failures >= _failure_threshold(provider_id):
            s.state = CBState.OPEN
            s.opened_at = now
            s.hold_down_until = now + _hold_down_sec(provider_id)
            # v3.0.30: include provider + failure count + hold-down in the
            # message string itself. The structlog ``extra`` was correct but
            # the std-logger formatter dropped them, so the log line was bare
            # "circuit_breaker.opened" with no provider context. Useless when
            # several providers misbehave and you're trying to find the
            # culprit by tail -f.
            logger.warning(
                "circuit_breaker.opened provider=%s failures=%d hold_down_sec=%d",
                provider_id, s.failures, _hold_down_sec(provider_id),
                extra={"provider": provider_id, "failures": s.failures},
            )
            _export_gauge(provider_id, s.state)


async def force_open(provider_id: str):
    async with _lock:
        s = _get_local(provider_id)
        s.state = CBState.OPEN
        s.opened_at = time.time()
        _export_gauge(provider_id, s.state)


async def force_close(provider_id: str):
    async with _lock:
        s = _get_local(provider_id)
        s.state = CBState.CLOSED
        s.failures = 0
        s.successes = 0
        s.hold_down_until = 0.0
        _export_gauge(provider_id, s.state)


def get_all_states() -> dict[str, dict]:
    now = time.time()
    result = {}
    for pid, s in _local_states.items():
        result[pid] = {
            "state": s.state.value,
            "failures": s.failures,
            "hold_down_remaining": max(0, s.hold_down_until - now),
        }
    return result


BILLING_ERROR_PATTERNS = [
    # True billing / quota-exhausted signals only. A generic 429 or "rate limit"
    # message is a transient throttling signal and must flow through the retry
    # loop in app/routing/retry.py — not fail fast + open the breaker for 1h.
    # Billing-scoped 429s carry a specific substring (insufficient_quota,
    # "payment required", etc.) and will still match.
    "insufficient_quota",
    "insufficient credit",
    "quota exceeded",
    "billing",
    "payment required",
    "subscription",
]


def is_billing_error(error_text: str) -> bool:
    low = error_text.lower()
    return any(p in low for p in BILLING_ERROR_PATTERNS)


# v2.7.8 BUG-002: Auth errors are PERMANENT until admin re-keys the provider.
# Treating them as transient (default CB behaviour) means we keep retrying a
# provider whose api_key is stale, burning latency and producing cryptic
# user-facing errors. When detected:
#   1. Open the breaker indefinitely (or until admin re-keys / re-auths)
#   2. Surface a "needs re-auth" status the UI can render as a red badge
#   3. Stop including in route-selection candidates (handled by select_provider
#      filtering on circuit-breaker state)
AUTH_ERROR_PATTERNS = [
    "authentication_error",
    "invalid x-api-key",
    "invalid api key",
    "invalid authentication",
    "unauthorized",
    "401",
    "403",
    "permission_denied",
    "expired_token",
    "invalid_token",
    "invalid_grant",
    "missing gemini api key",
    "missing openai api key",
    "missing anthropic api key",
    "the api_key client option must be set",
]


def is_auth_error(error_text: str) -> bool:
    """True if the error indicates the provider's auth credentials are
    permanently broken (need admin intervention) rather than transient."""
    if not error_text:
        return False
    low = error_text.lower()
    # Don't flag generic 401/403 hits without context — only when they're
    # paired with auth-error semantics. We keep "401"/"403" in the list because
    # status-code-based error messages from upstream usually pair with body
    # text ("HTTP 401: ...") and the lookup is a substring match.
    return any(p in low for p in AUTH_ERROR_PATTERNS)


# Track providers in "needs re-auth" state separately from the regular CB
# states. This survives manual `force_close` calls — the only way out is
# `clear_auth_failure(provider_id)` (called when admin re-keys via the
# Provider edit form) or a successful test request.
_auth_failed: dict[str, dict] = {}  # provider_id → {since: float, last_error: str}


def get_auth_failure(provider_id: str) -> Optional[dict]:
    return _auth_failed.get(provider_id)


def clear_auth_failure(provider_id: str) -> None:
    _auth_failed.pop(provider_id, None)


def get_all_auth_failures() -> dict[str, dict]:
    return dict(_auth_failed)


async def record_auth_failure(provider_id: str, error_text: str) -> None:
    """Mark a provider as needing re-auth. Opens the breaker with an extended
    hold-down (24h) so the auto-half-open transition still re-checks but rarely.
    Admin can clear via the API or by saving a new key."""
    async with _lock:
        s = _get_local(provider_id)
        s.state = CBState.OPEN
        s.opened_at = time.time()
        # 24h hold-down — long enough to not waste latency, short enough to
        # auto-recover if admin fixes it externally and forgets to clear.
        s.hold_down_until = time.time() + 86400
        _auth_failed[provider_id] = {
            "since": time.time(),
            "last_error": (error_text or "")[:300],
        }
        logger.warning(
            "circuit_breaker.auth_failure_marked",
            extra={"provider": provider_id, "error": error_text[:200]},
        )
        _export_gauge(provider_id, s.state)
