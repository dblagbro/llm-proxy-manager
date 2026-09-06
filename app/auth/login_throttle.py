"""v5.22.15 — rate-limit failed admin logins.

``POST /api/auth/login`` had no attempt limiting of any kind. The admin UI is
internet-facing at ``/llm-proxy2/``, and ``app/middleware/ip_block.py`` names
the login path in ``_ALWAYS_ALLOW`` — a deliberate lockout-recovery exemption
— so the one IP control the app had explicitly did not cover it. bcrypt's
work factor is the only thing that slowed an attacker down, and that is a
speed bump, not a limit.

That mattered more than usual here: the admin password was published in a
public repo until 2026-08-28, so an attacker's candidate list starts with a
known-real password and any near-variants of it.

Design constraints, in order:

  1. **Never lock the operator out permanently.** ip_block.py's recovery
     exemption exists because a permanent self-lockout on the login path is
     unrecoverable without shell access. So this is a *time-boxed* penalty
     that always expires on its own, never a persistent ban.
  2. **Count failures, not requests.** A correct password clears the counter
     immediately, so ordinary typo-then-retry never trips it.
  3. **Per-IP, not per-username.** Keying on username lets an attacker lock
     the operator out of their own account by guessing at it — a denial of
     service handed over for free.

In-memory by design: it mirrors ip_block.py, survives exactly as long as it
needs to, and a restart clearing the counters is an acceptable trade for
having no new shared-state dependency.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque

logger = logging.getLogger(__name__)

# Ten wrong passwords from one address in five minutes is not a human who
# forgot theirs.
MAX_FAILURES = 10
WINDOW_SEC = 300
LOCKOUT_SEC = 900

# Bound the map so a spray across forged X-Forwarded-For values cannot grow
# it without limit. Evicting the coldest entry is safe: the worst case is an
# attacker regaining a few attempts, which the window would have returned
# shortly anyway.
MAX_TRACKED_IPS = 10_000

_failures: dict[str, deque[float]] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _expire_locks(now: float) -> None:
    for ip in list(_locked_until):
        if _locked_until[ip] <= now:
            del _locked_until[ip]
            _failures.pop(ip, None)


def _enforce_size_cap() -> None:
    """Trim the coldest entries back to the cap.

    Must run AFTER the new entry is recorded, not before: trimming first
    leaves the map one over the cap on every call, which is how the bound
    was originally missed.
    """
    if len(_failures) <= MAX_TRACKED_IPS:
        return
    coldest = sorted(_failures.items(), key=lambda kv: kv[1][-1] if kv[1] else 0)
    for ip, _ in coldest[: len(_failures) - MAX_TRACKED_IPS]:
        _failures.pop(ip, None)
        _locked_until.pop(ip, None)


def seconds_remaining(ip: str, *, now: float | None = None) -> int:
    """Seconds until this IP may try again. 0 means it is not locked out."""
    now = time.time() if now is None else now
    until = _locked_until.get(ip)
    if until is None or until <= now:
        return 0
    return int(until - now) + 1


def record_failure(ip: str, *, now: float | None = None) -> int:
    """Record one failed login. Returns the lockout seconds if this trips it."""
    now = time.time() if now is None else now
    _expire_locks(now)
    attempts = _failures[ip]
    attempts.append(now)
    while attempts and attempts[0] < now - WINDOW_SEC:
        attempts.popleft()
    _enforce_size_cap()
    if len(attempts) >= MAX_FAILURES:
        _locked_until[ip] = now + LOCKOUT_SEC
        logger.warning(
            "auth.login throttled ip=%s failures=%d lockout_sec=%d",
            ip,
            len(attempts),
            LOCKOUT_SEC,
        )
        return LOCKOUT_SEC
    return 0


def record_success(ip: str) -> None:
    """A correct password clears the record — a typo must not accumulate."""
    _failures.pop(ip, None)
    _locked_until.pop(ip, None)


def reset() -> None:
    """Test hook — drop all state."""
    _failures.clear()
    _locked_until.clear()
