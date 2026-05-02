"""Per-request tenant context (v3.0.45).

ContextVar that carries the calling api_key_id into select_provider and
its internal helpers (cascade walk, critique, hedge, grader) so the
v3.0.45 ownership filter (Provider.owned_by_key_id) can identify the
caller without plumbing the value through every internal route call.

Set by chat handlers at request entry; read by select_provider's
ownership filter. Null when no chat request is in flight (background
keepalive probes, scan-models, test buttons) — those bypass the
ownership filter intentionally because they're operator-internal.
"""
from contextvars import ContextVar
from typing import Optional


current_api_key_id: ContextVar[Optional[str]] = ContextVar(
    "current_api_key_id", default=None
)
