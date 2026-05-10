"""v3.6.2 — per-request context carriers (client IP, etc).

Cross-cutting context for the activity log: things every llm_request
event should record but that don't naturally flow through the dispatch
chain as parameters. Set by the FastAPI middleware at request entry,
read by ``record_outcome()`` when building the activity-log meta dict.

Currently captures:

- ``client_ip``: the originating caller IP, taken from the
  ``X-Forwarded-For`` header's first hop (since we run behind nginx,
  ``request.client.host`` is the nginx container IP and useless for
  attribution). Falls back to ``request.client.host`` if no
  ``X-Forwarded-For`` is set (e.g. internal cluster sync, probe paths).

Why a contextvar and not a request parameter? ``record_outcome()`` has
~12 call sites across messages.py, completions.py, keepalive.py,
_messages_streaming.py, _completions_streaming.py, etc. Threading a
new ``client_ip`` parameter through every signature is high-churn for
a value that is fundamentally a per-request side-channel. ContextVars
were designed for exactly this.

Probes (keepalive, internal traffic) don't run inside a request scope
so the contextvar is empty and the IP field is omitted from the
activity log — which is correct, "probe-keepalive" is already its own
``api_key_prefix`` value, no IP needed.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

# Default empty so probes / non-HTTP code paths emit nothing rather
# than blow up. Avoid setting placeholders like "unknown" so dashboard
# filters can use IS NULL semantics cleanly.
_client_ip: ContextVar[Optional[str]] = ContextVar("client_ip", default=None)


def set_client_ip(ip: Optional[str]) -> None:
    """Set the per-request client IP. Idempotent — call once per
    request entry. ``None`` clears any prior value (defensive)."""
    _client_ip.set(ip if ip else None)


def get_client_ip() -> Optional[str]:
    """Read the current client IP, or ``None`` outside a request."""
    return _client_ip.get()


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

    Defensive — never raises.
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
