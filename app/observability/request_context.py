"""v3.6.2/v3.6.3 — per-request context carriers (client IP, etc).

Cross-cutting context for the activity log: things every llm_request
event should record but that don't naturally flow through the dispatch
chain as parameters. Set by the FastAPI middleware at request entry,
read by ``record_outcome()`` when building the activity-log meta dict.

Currently captures:

- ``client_ip``: the originating caller IP (post-rewrite, see v3.6.3
  notes below).
- ``client_ip_inside``: the raw IP nginx reported, before any rewrite.
  Useful for debugging when the rewrite layer obscures something.

Why a contextvar and not a request parameter? ``record_outcome()`` has
~12 call sites across messages.py, completions.py, keepalive.py,
_messages_streaming.py, _completions_streaming.py, etc. Threading new
parameters through every signature is high-churn for values that are
fundamentally per-request side-channels. ContextVars were designed
for exactly this.

Probes (keepalive, internal traffic) don't run inside a request scope
so the contextvar is empty and the IP field is omitted from the
activity log — which is correct, "probe-keepalive" is already its own
``api_key_prefix`` value, no IP needed.

----

v3.6.3 — LAN-egress IP rewrite via DNS lookup.

Hairpin NAT problem: when a LAN host calls the proxy via the public
URL, the LAN router NATs the TCP source to the LAN gateway IP
(e.g. ``192.168.18.1``), and that's what nginx sees. The actual public
egress IP is invisible to the HTTP layer — IP NAT happens below it.

Workaround: operator declares a mapping from internal IPs to
*resolvable hostnames* whose A record reflects the LAN's current public
IP. We resolve the hostname (with TTL caching to avoid blocking the
hot path) and substitute the public IP in the log.

Settings:

    client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}

When a request arrives with ``client_ip = 192.168.18.1``, we look up
``ip.voipguru.org``, get e.g. ``24.168.14.36``, and:

- ``client_ip`` → ``"24.168.14.36"`` (the public egress)
- ``client_ip_inside`` → ``"192.168.18.1"`` (the raw inside)

Both go into the activity log meta, so dashboards can use either.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

# Default empty so probes / non-HTTP code paths emit nothing rather
# than blow up. Avoid setting placeholders like "unknown" so dashboard
# filters can use IS NULL semantics cleanly.
_client_ip: ContextVar[Optional[str]] = ContextVar("client_ip", default=None)
_client_ip_inside: ContextVar[Optional[str]] = ContextVar(
    "client_ip_inside", default=None,
)
# v3.7.15 — BUG-017: tag for internal proxy callers (currently only the
# AI rate limiter). When set, `record_outcome()` stamps the meta dict
# and the AI rate limiter excludes those events from its next review
# so it doesn't see (and re-amplify) its own previous calls.
_internal_source: ContextVar[Optional[str]] = ContextVar(
    "internal_source", default=None,
)

# v4.4.23 — per-request gating-header presence flags. Set at the
# /v1/messages + /v1/completions entry points; read by
# ``_build_outcome_meta`` so every activity_log row carries the
# verifiable bool of "did this request carry X-Conversation-Id".
#
# Why: 2026-05-27 DevinGPT follow-up asked us to confirm whether two
# specific 2026-05-17 events had the header. We couldn't — activity_log
# event_meta didn't capture header presence at all, only request body
# fields. The Prometheus counter does (F-OBS-003, v4.4.15) but it's
# in-process and resets on restart, so it can't tell us about
# historical individual events. This contextvar closes the gap.
#
# Stored as bool (presence only), NOT the header value — the value can
# be a privacy-sensitive client identifier (conversation id) so we
# keep the activity-log row schema-stable + privacy-clean.
_had_x_conversation_id: ContextVar[bool] = ContextVar(
    "had_x_conversation_id", default=False,
)
_had_x_memory_tag: ContextVar[bool] = ContextVar(
    "had_x_memory_tag", default=False,
)


# v3.6.3 — DNS cache for the LAN-egress hostname rewrite. Map keys are
# hostnames; values are ``(resolved_ip_or_none, expiry_at_monotonic)``.
# A 5-minute TTL is generous enough to keep blocking DNS off the hot
# path under normal request rates (max one socket.gethostbyname() per
# 5 min per configured hostname) but short enough to track ISP-rotated
# dynamic IPs without an operator restart.
_dns_cache: dict[str, tuple[Optional[str], float]] = {}
_dns_lock = threading.Lock()
_DNS_TTL_SEC = 300.0


def _resolve_cached(hostname: str) -> Optional[str]:
    """TTL-cached ``socket.gethostbyname`` for the LAN-egress lookup.

    Returns the resolved IPv4 string, or ``None`` if the lookup fails
    (NXDOMAIN, network error, etc). ``None`` results are also cached
    for the TTL window so a misconfigured hostname doesn't burn DNS
    on every request.

    Thread-safe — uses a module-level lock around cache mutations.
    The lookup itself is blocking (sync stdlib) but only fires once
    per TTL window per hostname, which is acceptable trade-off for
    the simplicity. If this ever becomes a hot-path concern we can
    move to ``loop.getaddrinfo()`` async resolution.
    """
    if not hostname:
        return None
    now = time.monotonic()
    with _dns_lock:
        cached = _dns_cache.get(hostname)
        if cached and cached[1] > now:
            return cached[0]
    # Outside the lock — DNS lookup may block briefly
    resolved: Optional[str]
    try:
        resolved = socket.gethostbyname(hostname)
    except Exception as exc:
        logger.warning(
            "lan_egress_dns_resolve_failed",
            extra={"hostname": hostname, "error": str(exc)},
        )
        resolved = None
    with _dns_lock:
        _dns_cache[hostname] = (resolved, now + _DNS_TTL_SEC)
    return resolved


def prewarm_lan_egress_dns() -> None:
    """Warm the DNS cache for every configured LAN-egress hostname so
    the first request after startup doesn't pay the synchronous DNS
    cost. Called from the FastAPI startup hook.
    """
    try:
        from app.config import settings
        mapping = getattr(settings, "client_ip_lan_resolve_map", {}) or {}
        for hostname in set(mapping.values()):
            ip = _resolve_cached(hostname)
            logger.info(
                "lan_egress_dns_prewarm",
                extra={"hostname": hostname, "resolved_ip": ip},
            )
    except Exception as exc:
        # Defensive — startup must not crash on a config typo
        logger.warning("lan_egress_dns_prewarm_failed", extra={"error": str(exc)})


def _maybe_rewrite_lan_ip(ip: str) -> Optional[str]:
    """If ``ip`` matches a known LAN-internal gateway, return the
    DNS-resolved public IP for that LAN. Else return ``None``.

    Settings example:
        client_ip_lan_resolve_map = {"192.168.18.1": "ip.voipguru.org"}
    """
    try:
        from app.config import settings
        mapping = getattr(settings, "client_ip_lan_resolve_map", {}) or {}
        hostname = mapping.get(ip)
        if not hostname:
            return None
        return _resolve_cached(hostname)
    except Exception:
        return None


def set_client_ip(ip: Optional[str]) -> None:
    """Set the per-request client IP. Idempotent — call once per
    request entry. ``None`` clears any prior value (defensive).

    v3.6.3: if ``ip`` matches a configured LAN-internal gateway, the
    public-facing IP (DNS-resolved from the configured hostname) is
    stored as ``client_ip`` and the raw inside IP as ``client_ip_inside``.
    Both fields end up in the activity log meta dict.
    """
    if not ip:
        _client_ip.set(None)
        _client_ip_inside.set(None)
        return
    _client_ip_inside.set(ip)
    rewrite = _maybe_rewrite_lan_ip(ip)
    _client_ip.set(rewrite if rewrite else ip)


def get_client_ip() -> Optional[str]:
    """Read the current public-facing client IP (post-rewrite if
    applicable), or ``None`` outside a request."""
    return _client_ip.get()


def get_client_ip_inside() -> Optional[str]:
    """v3.6.3: the raw inside IP nginx reported, before any
    LAN-egress rewrite. Same as ``get_client_ip()`` for non-LAN
    callers; differs only when the rewrite map applied."""
    return _client_ip_inside.get()


def extract_client_ip_from_request(request) -> Optional[str]:
    """Pull the originating caller IP from a Starlette/FastAPI
    ``Request`` object.

    Order of preference:
    1. First hop of ``X-Forwarded-For`` (nginx prepends the real
       caller). Multi-hop XFF chains take the leftmost (closest to
       client). Trim whitespace around comma separators.
    2. ``X-Real-IP`` header (some reverse proxies use this).
    3. ``request.client.host`` (raw socket peer; usually nginx
       container IP in our deployment, but useful for direct-to-
       container probes during development).

    Defensive — never raises. The LAN-egress rewrite happens in
    ``set_client_ip`` not here, so this function still returns the
    raw value the proxy chain saw.
    """
    try:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first_hop = xff.split(",", 1)[0].strip()
            if first_hop:
                return first_hop
        xri = request.headers.get("x-real-ip")
        if xri and xri.strip():
            return xri.strip()
        client = getattr(request, "client", None)
        if client and getattr(client, "host", None):
            return client.host
    except Exception:
        pass
    return None


def set_internal_source(source: Optional[str]) -> None:
    """v3.7.15 — tag a request as internally-generated (e.g. from the
    AI rate limiter) so its activity-log row carries that label and
    later sweeps can exclude it. Pass ``None`` to clear."""
    _internal_source.set(source or None)


def get_internal_source() -> Optional[str]:
    """v3.7.15 — read the current internal-source tag, or None if the
    request is from an external caller."""
    return _internal_source.get()


def set_caller_memory_headers(
    has_conversation_id: bool, has_memory_tag: bool = False,
) -> None:
    """v4.4.23 — capture which caller-memory gating headers this
    request carried. Set at the /v1/messages + /v1/completions entry
    points so activity_log can verifiably record the per-event
    presence — the Prometheus counter (v4.4.15 F-OBS-003) only gives
    in-process running totals."""
    _had_x_conversation_id.set(bool(has_conversation_id))
    _had_x_memory_tag.set(bool(has_memory_tag))


def get_had_x_conversation_id() -> bool:
    """v4.4.23 — was X-Conversation-Id present on the current request?
    False outside a request scope (probes, internal calls)."""
    return _had_x_conversation_id.get()


def get_had_x_memory_tag() -> bool:
    """v4.4.23 — was X-Memory-Tag present on the current request?"""
    return _had_x_memory_tag.get()


def _clear_dns_cache_for_tests() -> None:
    """Test helper — clear the cache between tests so order doesn't matter."""
    with _dns_lock:
        _dns_cache.clear()
