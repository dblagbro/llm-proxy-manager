"""v5.16.0 (#512) — Consolidated ``x-llmproxy-config`` request header.

Single JSON blob request header that carries proxy overrides that would
otherwise need one dedicated header each. Ships as a callable helper +
FastAPI dependency; every handler that reads an ``X-Proxy-*`` header
also reads from this blob via ``read_config_key()``.

Precedence (documented + tested): individual header wins over blob key.
Rationale: explicit-per-request wins over general-blob — matches HTTP's
"more specific header wins" pattern, gives callers a predictable
override path, and lets us migrate individual headers into the blob
gradually without breaking back-compat.

Wire format::

    x-llmproxy-config: {"accept_mcp":["fetch_url"],"reasoning_effort":"high"}

Empty object ``{}`` is valid + no-op. Malformed JSON → we log a debug
line and fall back to no config (never 500; the request continues
using individual-header defaults). This is soft-fail-open on the
blob because bricking a caller on a misspelled header would be
worse than silently ignoring their override.

Unknown keys silently ignored (with a debug log). Forward-compat for
future keys without version-bumping every caller.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import Request

logger = logging.getLogger(__name__)

HEADER_NAME = "x-llmproxy-config"
STATE_KEY = "llmproxy_config"

# The keys we know how to consume. Add here as more X-Proxy-* headers
# get migrated in. Unknown keys are ignored (with a debug log) — this
# is by design so a caller can send a config blob targeting a future
# version of the proxy without our code choking on it.
KNOWN_KEYS = frozenset({
    "accept_mcp",
    # v5.18.1 — reasoning_effort migrated. Callers can now pass
    # "reasoning_effort" in the x-llmproxy-config blob AS AN ALTERNATIVE
    # to setting it in the request body. If both are present, body wins
    # (existing per-request semantics preserved). If neither, provider
    # native_thinking_params default applies.
    "reasoning_effort",
    # Reserved for future migrations:
    # "cache_mode", "fallback_chain", "cascade_mode",
    # "model_family", "pii_mask", "vision_strip",
})


def parse_config_blob(raw: Optional[str]) -> dict:
    """Parse the raw header value. Never raises."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        logger.debug(
            "x-llmproxy-config: JSON parse failed — soft-ignoring. err=%s raw_prefix=%r",
            type(e).__name__, raw[:120],
        )
        return {}
    if not isinstance(parsed, dict):
        logger.debug(
            "x-llmproxy-config: top-level MUST be a JSON object, got %s — ignoring.",
            type(parsed).__name__,
        )
        return {}
    unknown = set(parsed.keys()) - KNOWN_KEYS
    if unknown:
        logger.debug(
            "x-llmproxy-config: ignoring unknown keys=%s (forward-compat)",
            sorted(unknown),
        )
    return {k: v for k, v in parsed.items() if k in KNOWN_KEYS}


def get_config_from_request(request: Request) -> dict:
    """Cached-per-request access. Callers that read multiple keys don't
    re-parse the header on each read."""
    cached = getattr(request.state, STATE_KEY, None)
    if cached is not None:
        return cached
    raw = request.headers.get(HEADER_NAME)
    parsed = parse_config_blob(raw)
    try:
        setattr(request.state, STATE_KEY, parsed)
    except Exception:
        # ``request.state`` is a mutable proxy in FastAPI but be defensive.
        pass
    return parsed


def read_config_key(
    request: Request, key: str, header_fallback: Optional[str] = None,
) -> Optional[Any]:
    """Resolve one key with the documented precedence:

    1. If an individual ``header_fallback`` header is present + non-empty,
       return that value verbatim (as a string — caller handles typed
       parsing since individual headers have always been string-typed).
    2. Else if the ``x-llmproxy-config`` blob has ``key``, return the
       JSON-typed value from the blob (list, dict, int, str, bool — whatever
       the caller sent).
    3. Else return ``None``.

    The str-vs-typed-value asymmetry is intentional: individual headers
    are always strings (HTTP), so callers already handle parsing them.
    Blob values are already typed by JSON, so no additional parsing is
    needed. This lets callers migrate one key at a time without
    changing downstream handlers.
    """
    if header_fallback:
        v = request.headers.get(header_fallback)
        if v is not None and v != "":
            return v
    blob = get_config_from_request(request)
    return blob.get(key)


def emit_config_applied_header(
    resp_headers: dict, request: Request,
) -> None:
    """Debug convenience: echo the parsed config back so a caller can
    verify their blob parsed correctly. Empty parsed blob → no header
    added (avoids noise on the vast majority of requests that don't
    send the blob at all)."""
    parsed = get_config_from_request(request)
    if not parsed:
        return
    try:
        resp_headers["X-LLMProxy-Config-Applied"] = json.dumps(
            parsed, sort_keys=True, separators=(",", ":"),
        )
    except Exception:
        pass
