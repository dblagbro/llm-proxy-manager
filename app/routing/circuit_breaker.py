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
    # v5.9.6 — count of consecutive open→reopen cycles without a successful
    # close in between. Resets in ``record_success`` when CB transitions to
    # CLOSED. Used to scale hold_down exponentially for chronically-dead
    # providers (cap at 2^5 = 32× base) so the keepalive/auto-probe path
    # backs off instead of probing every 60s forever.
    consecutive_opens: int = 0


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
        # v5.9.7 — gate the half-open transition on BOTH the global
        # circuit_breaker_timeout_sec AND hold_down_until. Pre-fix,
        # only the static 60s timeout was checked, which meant the
        # exponential hold_down computed in record_failure (v5.9.6)
        # was logged but never actually enforced — a chronically-dead
        # provider went HALF_OPEN every 60s no matter how large its
        # hold_down had grown. The v3.0.53 billing-error 6h hold had
        # the same hidden bug. Pick the later of the two thresholds
        # so transient blips still get the snappy 60s probe but a
        # backed-off provider waits its full hold_down.
        ready_at = max(s.opened_at + settings.circuit_breaker_timeout_sec, s.hold_down_until)
        if now >= ready_at:
            async with _lock:
                s.state = CBState.HALF_OPEN
                s.successes = 0
                _export_gauge(provider_id, s.state)
            # v5.3.9 — auto-probe on hold-down expiry. Pre-fix, a CB
            # in half-open would wait for ORGANIC traffic to test it;
            # for low-volume providers that means the CB could stay in
            # half-open for tens of minutes showing "tripped" in the UI
            # while nothing was actually wrong with the upstream. Fire
            # one keepalive probe immediately — if it succeeds, the CB
            # closes (after the existing 2-success hysteresis); if it
            # fails, back to open with a fresh hold-down. Run as a
            # detached task so the caller (route selection) doesn't
            # wait on the probe — best-effort.
            _schedule_auto_probe(provider_id)
    return s.state


def _schedule_auto_probe(provider_id: str) -> None:
    """Fire-and-forget one synthetic probe when a CB transitions to
    half-open. Uses the existing keepalive probe path so the success/
    failure flows through ``record_outcome`` and the CB state machine
    closes itself if the upstream is actually fine.

    Defensive — any failure to schedule (no event loop, import error
    during shutdown, etc.) is swallowed so it can't break route
    selection."""
    try:
        import asyncio
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # not in an async context
    async def _probe():
        try:
            from app.models.database import AsyncSessionLocal
            from app.models.db import Provider
            from sqlalchemy import select as _select
            async with AsyncSessionLocal() as db:
                rs = await db.execute(_select(Provider).where(Provider.id == provider_id))
                provider = rs.scalar_one_or_none()
                if provider is None or provider.deleted_at is not None:
                    return
                from app.monitoring.keepalive import _probe_one
                await _probe_one(provider)
        except Exception as exc:
            logger.debug(
                "circuit_breaker.auto_probe_failed provider=%s err=%r",
                provider_id, exc,
            )
    try:
        loop.create_task(_probe())
    except Exception:
        pass


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
                s.consecutive_opens = 0  # v5.9.6 — provider recovered, reset backoff
                logger.info("circuit_breaker.closed", extra={"provider": provider_id})
                _export_gauge(provider_id, s.state)
        elif s.state == CBState.CLOSED:
            s.failures = max(0, s.failures - 1)


async def record_failure(provider_id: str, billing_error: bool = False):
    async with _lock:
        s = _get_local(provider_id)
        s.failures += 1
        now = time.time()

        # Billing errors immediately open the breaker and set a long hold-down.
        # v3.0.53: extended from 1h → 6h. Billing failures need operator
        # intervention (refill credits, update payment method, escalate
        # subscription tier) and won't self-resolve. The 1h hold-down meant
        # each node fires a re-test probe ~24×/day per provider, contributing
        # 1-3/hr cluster-wide noise on Personal OpenAI quota burn. 6h cuts
        # that to 4 retests/day per node — still detects same-day recovery,
        # 75% less log churn while operator triages.
        if billing_error:
            s.state = CBState.OPEN
            s.opened_at = now
            s.hold_down_until = now + 21600  # 6-hour hold for billing errors
            logger.warning("circuit_breaker.billing_error", extra={"provider": provider_id})
            _export_gauge(provider_id, s.state)
            return

        if s.failures >= _failure_threshold(provider_id):
            # v5.9.6 — only log + reset hold_down on an actual state
            # *transition* into OPEN (from CLOSED or HALF_OPEN). Pre-fix,
            # the auto-probe path (v5.3.9) caused every chronically-dead
            # provider to cycle OPEN→HALF_OPEN→probe-fails→back here every
            # ~60s forever, each cycle re-logging "circuit_breaker.opened"
            # and resetting the hold-down to the base 120s. Result: 17+
            # noise lines per provider per 30min and an effective 60s
            # retest cadence on dead upstreams (vs the intended 120s base
            # × exponential backoff for chronic failures).
            was_open = s.state == CBState.OPEN
            if was_open:
                # Already OPEN — failure counter already incremented above,
                # nothing else to do. Notably: don't reset opened_at or
                # hold_down_until (would drift the half-open timeout forward
                # indefinitely under sustained failures) and don't re-log.
                return
            # State transition CLOSED|HALF_OPEN → OPEN: register a fresh
            # open cycle and compute backoff. Exponential backoff is capped
            # at 32× base (≈64 min at default 120s), bridging the gap
            # between transient (1× base) and billing-error (180× = 6h)
            # without operator tuning.
            s.consecutive_opens += 1
            s.state = CBState.OPEN
            s.opened_at = now
            base = _hold_down_sec(provider_id)
            multiplier = 1 << min(s.consecutive_opens - 1, 5)
            s.hold_down_until = now + base * multiplier
            logger.warning(
                "circuit_breaker.opened provider=%s failures=%d hold_down_sec=%d consecutive_opens=%d",
                provider_id, s.failures, base * multiplier, s.consecutive_opens,
                extra={
                    "provider": provider_id,
                    "failures": s.failures,
                    "consecutive_opens": s.consecutive_opens,
                },
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
        s.consecutive_opens = 0  # v5.9.6 — operator override clears backoff state
        _export_gauge(provider_id, s.state)


def get_all_states() -> dict[str, dict]:
    now = time.time()
    result = {}
    for pid, s in _local_states.items():
        result[pid] = {
            "state": s.state.value,
            "failures": s.failures,
            "hold_down_remaining": max(0, s.hold_down_until - now),
            # v5.17.1 — surface consecutive_opens so keepalive can gate
            # its probes on chronic re-open cycles. Same field the v5.9.6
            # exponential backoff already reads internally.
            "consecutive_opens": s.consecutive_opens,
        }
    return result


def get_consecutive_opens(provider_id: str) -> int:
    """v5.17.1 — helper for keepalive-side chronic-CB gating. Returns 0
    if the provider isn't in the CB state table (no probes have run
    for it yet)."""
    s = _local_states.get(provider_id)
    return s.consecutive_opens if s is not None else 0


BILLING_ERROR_PATTERNS = [
    # True billing / quota-exhausted signals only. A generic 429 or "rate limit"
    # message is a transient throttling signal and must flow through the retry
    # loop in app/routing/retry.py — not fail fast + open the breaker for 1h.
    # Billing-scoped 429s carry a specific substring (insufficient_quota,
    # "payment required", etc.) and will still match.
    "insufficient_quota",
    "insufficient credit",
    "quota exceeded",
    # v3.0.48: OpenAI's RateLimitError on a depleted account reads
    # "You exceeded your current quota" (ALL CAPS-Y plain English).
    # Word order doesn't match "quota exceeded", so the previous list
    # missed it — keepalive probes kept hitting the dead provider every
    # 5 min for hours instead of opening the breaker. Two flexible
    # variants catch the OpenAI message + the plural form.
    "exceeded your current quota",
    "exceeded your quota",
    "billing",
    "payment required",
    "subscription",
]


def is_billing_error(error_text: str) -> bool:
    low = error_text.lower()
    return any(p in low for p in BILLING_ERROR_PATTERNS)


# v5.3.9 — caller-side error classifier. Today the CB increments
# failures on ANY error class, which means a malformed body from a
# caller (the bot sent an orphan tool_call_id, a list-content where
# the upstream wanted string, etc.) trips OUR provider's CB as if the
# UPSTREAM had failed. 2026-06-12 c1conv audit: ~62% of CB trips that
# day were caused by caller bugs, not by the providers themselves.
# Punishing the provider for the bot's mistake forces operator
# intervention (manual re-test) when nothing is actually wrong with the
# upstream.
#
# Classes that ARE caller-side (proxy or upstream-spec rejection of a
# malformed body — the upstream is rejecting the SHAPE, not failing):
#   - Vertex Gemini strict-shape: "Missing corresponding tool call"
#   - Cursor-bridge: "request.messages.content: string expected"
#   - OpenAI strictness: "Invalid user message at index"
#   - Generic body validation: "invalid request" + 400-class shape
#     complaints. (Real "invalid auth"-type 400/401 already routes
#     through is_auth_error and is treated separately.)
#
# When this classifier fires, ``record_outcome`` skips the
# ``record_failure`` call (no CB increment). Activity log still
# captures the row with severity=warning so the failure is visible —
# operators can still see "bot is sending malformed bodies" but the
# CB stays healthy.
CALLER_SIDE_ERROR_PATTERNS = (
    "missing corresponding tool call",
    "request.messages.content: string expected",
    "invalid user message at index",
    "invalid 'messages[",
    "missing corresponding tool call for tool response",
)


def is_caller_side_error(error_text: str) -> bool:
    low = (error_text or "").lower()
    return any(p in low for p in CALLER_SIDE_ERROR_PATTERNS)


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


# v3.0.75 — error-class taxonomy. Activity log used to record
# ``error_str`` as a free-form blob, but operators investigating
# "why the failure rate spiked at 03:14" then had to grep through
# strings to bucket them. Adding an ``error_class`` enum lets the
# operator filter / chart by category directly.
#
# Order matters in classify_error() — the first matching pattern
# wins, so more-specific buckets (auth, billing) come before
# more-general ones (network, upstream_5xx).

_RATE_LIMIT_PATTERNS = [
    "rate_limit",
    "rate limit",
    "too many requests",
    "ratelimit",
    "429",
    "quota exceeded",
    "throttled",
]

_TIMEOUT_PATTERNS = [
    "timed out",
    "timeout",
    "deadline exceeded",
    "read timeout",
    "connect timeout",
]

_NETWORK_PATTERNS = [
    "connection refused",
    "connection reset",
    "name or service not known",
    "temporary failure in name resolution",
    "dns",
    "no route to host",
    "network is unreachable",
    "ssl",
    "tls",
    # v3.0.88: httpx exception names — caught these as "unknown" during a
    # 22s upstream Anthropic blip on 2026-05-06 (4 events: ConnectTimeout
    # already classified as ``timeout``, but ReadError + WriteError fell
    # through). httpx documents ReadError as "Failed to receive data
    # from the network" — that's a network class.
    "readerror",
    "writeerror",
    "remoteprotocolerror",
    "localprotocolerror",
    "proxyerror",
    # BUG-048 (v4.3.9 / 2026-05-20): the FORMATTED prose for the same
    # httpx-class errors (not the exception name) was falling through
    # to ``unknown``. Seen on grok-web bridge errors when grok-bridge
    # closes a connection mid-response; httpx surfaces it as the prose
    # below, not the camelcase exception name.
    "server disconnected",
    "without sending a response",
    "bridge unreachable",  # grok_bridge wrapper formatted-prose
]

_UPSTREAM_5XX_PATTERNS = [
    "500",
    "502",
    "503",
    "504",
    # v3.0.90 — Anthropic-specific 529 ("Overloaded") plus the
    # ``overloaded_error`` body type. 26 events of this shape during
    # the 2026-05-06 15:28-15:33 Anthropic API overload incident; pre-
    # fix they classified as ``unknown``. 529 isn't a standard HTTP
    # code but it's in the 5xx transient-upstream range and the
    # body shape is identical to other anthropic upstream errors.
    "529",
    "overloaded_error",
    "overloaded",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
]

_BAD_REQUEST_PATTERNS = [
    "400",
    # BUG-048 (v4.3.9 / 2026-05-20): 4xx codes that aren't already
    # routed to a more-specific bucket (401/403 → auth, 402 →
    # billing, 429 → rate_limit) all express "this request can't be
    # served" — client-side semantics. Most-seen in the activity log
    # was 404 from grok-web bridge ("'Conversation' with ID '…' was
    # not found") which represents a stale operator-configured
    # conversation ID, not an upstream defect; bad_request is the
    # honest bucket. 405/409/410/413/415/422 added prophylactically
    # to prevent the same fall-through on future surface.
    "404",
    "405",
    "409",
    "410",
    "413",
    "415",
    "422",
    "not found",
    "invalid_request",
    "invalid request",
    "validation error",
    "malformed",
    "bad request",
    # v3.0.89 — litellm/SDK exception names. The 7d historical scan
    # found ``litellm.ContextWindowExceededError: ...BadRequestError...``
    # falling through to unknown because the existing patterns expected
    # space-separated words (``bad request``) but exception names are
    # camelcase one-word (``BadRequestError`` → ``badrequesterror``
    # after lowercase).
    "badrequesterror",
    "contextwindowexceeded",
    "contentpolicyviolation",
    "unsupportedparams",
    # v3.8.2 (#260): coordinator-hub sent ~370 requests in 24h that
    # litellm rejects with "Invalid user message at index N" (malformed
    # messages[] array — e.g. message with role but no content). These
    # are caller-side bugs; do not retry on fallback providers.
    "invalid user message",
    "xaiexception - invalid user message",
    "openrouterexception - invalid user message",
    # v3.8.2 (#262): gemini safety-block / refusal returns response with
    # empty choices[]. ``to_anthropic_response()`` now raises a clear
    # error message on this case; classify as bad_request so the router
    # doesn't trigger CB / fallback (other providers will likely also
    # block the same content).
    "upstream returned no choices",
    "upstream returned empty choices",
]


def classify_error(error_text: str) -> str:
    """Bucket an upstream / client error string into a coarse category.

    Returns one of: ``auth``, ``billing``, ``rate_limit``, ``timeout``,
    ``network``, ``upstream_5xx``, ``bad_request``, or ``unknown``.

    Used by record_outcome to populate ``event_meta.error_class`` for
    activity-log filtering. Pattern lists deliberately overlap (e.g. a
    503 is both upstream_5xx and might mention "rate limit") — the
    caller-friendly ordering picks the most specific category first.
    """
    if not error_text:
        return "unknown"
    low = error_text.lower()
    # auth and billing first — these flag PERMANENT/operator-action conditions
    # and they share patterns like "401" with auth, "402" with billing.
    if is_auth_error(error_text):
        return "auth"
    if is_billing_error(error_text):
        return "billing"
    if any(p in low for p in _RATE_LIMIT_PATTERNS):
        return "rate_limit"
    if any(p in low for p in _TIMEOUT_PATTERNS):
        return "timeout"
    if any(p in low for p in _NETWORK_PATTERNS):
        return "network"
    if any(p in low for p in _UPSTREAM_5XX_PATTERNS):
        return "upstream_5xx"
    if any(p in low for p in _BAD_REQUEST_PATTERNS):
        return "bad_request"
    return "unknown"


# Track providers in "needs re-auth" state separately from the regular CB
# states. This survives manual `force_close` calls — the only way out is
# `clear_auth_failure(provider_id)` (called when admin re-keys via the
# Provider edit form) or a successful test request.
_auth_failed: dict[str, dict] = {}  # provider_id → {since: float, last_error: str}

# v3.7.16 — persistent-auth-failure detection (#239). When a provider
# accumulates >= ``PERSISTENT_AUTH_THRESHOLD`` failures within
# ``PERSISTENT_AUTH_WINDOW_SEC``, ``record_auth_failure`` ALSO sets
# ``Provider.auto_skip_until = now + 24h`` so the router protects
# itself without waiting for the in-memory CB. The DB field survives
# container restart (which v3.7.x in-memory ``_auth_failed`` doesn't),
# closing the gap where each fresh container deployed today re-hit
# auth-failed-once and rebuilt the CB state from scratch.
_auth_failure_history: dict[str, list[float]] = {}
PERSISTENT_AUTH_THRESHOLD = 3
PERSISTENT_AUTH_WINDOW_SEC = 1800.0  # 30 min


def get_auth_failure(provider_id: str) -> Optional[dict]:
    return _auth_failed.get(provider_id)


def clear_auth_failure(provider_id: str) -> None:
    _auth_failed.pop(provider_id, None)
    _auth_failure_history.pop(provider_id, None)


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
        # v3.7.16 — track failure history for the persistent-auth-failure
        # detector (#239). Only escalate to DB-persisted auto_skip after
        # the in-memory CB has been hit N times in a window, so a single
        # transient blip doesn't 24h-skip the provider.
        history = _auth_failure_history.setdefault(provider_id, [])
        now = time.time()
        history.append(now)
        # Prune entries older than the window so the threshold check
        # only sees recent failures.
        cutoff = now - PERSISTENT_AUTH_WINDOW_SEC
        history[:] = [t for t in history if t > cutoff]
        should_auto_skip = len(history) >= PERSISTENT_AUTH_THRESHOLD
        logger.warning(
            "circuit_breaker.auth_failure_marked",
            extra={
                "provider": provider_id,
                "error": error_text[:200],
                "consecutive_in_window": len(history),
            },
        )
        _export_gauge(provider_id, s.state)
    # Done with the lock — escalate to DB-persisted auto_skip if the
    # threshold tripped. Separate transaction so we don't hold the CB
    # lock during DB write.
    if should_auto_skip:
        try:
            await _persist_auto_skip(provider_id, error_text)
        except Exception as exc:
            logger.warning(
                "circuit_breaker.auto_skip_persist_failed provider=%s err=%s",
                provider_id, exc,
            )


async def _persist_auto_skip(provider_id: str, error_text: str) -> None:
    """v3.7.16 — write ``auto_skip_until = now + 24h`` to the Provider
    row so the router excludes this provider even after container
    restart (the in-memory ``_auth_failed`` map resets on restart, so
    DB persistence is what closes the gap).

    Idempotent: if the provider already has an auto_skip_until in the
    future for a non-billing reason, we extend it rather than overwrite
    (no need to keep re-stamping the same window)."""
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from sqlalchemy import select
    from app.models.database import AsyncSessionLocal
    from app.models.db import Provider
    new_until = _dt.now(_tz.utc) + _td(hours=24)
    async with AsyncSessionLocal() as db:
        rs = await db.execute(select(Provider).where(Provider.id == provider_id))
        p = rs.scalar_one_or_none()
        if not p:
            return
        # v4.4 M-4 (Path A) — exempt providers tagged
        # ``node_local_session=True`` from the auto-skip cluster-wide
        # propagation. Pre-fix, a persistent-auth failure on ONE node
        # set Provider.auto_skip_until which then cluster-synced to
        # every node — fleet-wide grok-web outage. With Path A the
        # per-node bridge state in provider_node_auth_state is the
        # right signal (and is NOT cluster-amplifying: each row is
        # owned by its node). The provider-level auto_skip path here
        # is bypassed entirely for these providers; in-memory
        # _auth_failed still tracks the local view for this node's
        # own short-term routing decisions.
        ec = p.extra_config or {}
        if ec.get("node_local_session"):
            logger.info(
                "circuit_breaker.auto_skip_skipped_for_node_local_session "
                "provider=%s (Path A semantic: per-node "
                "provider_node_auth_state rows are the authoritative "
                "view; not setting Provider.auto_skip_until)",
                provider_id,
            )
            return
        # Skip if already auto-skipped further out
        if p.auto_skip_until and p.auto_skip_until > new_until.replace(tzinfo=None):
            return
        p.auto_skip_until = new_until.replace(tzinfo=None)
        p.auto_skip_reason = "persistent_auth_failure"
        await db.commit()
        logger.warning(
            "circuit_breaker.auto_skip_persisted provider=%s until=%s reason=%s",
            provider_id, new_until.isoformat(), "persistent_auth_failure",
        )
