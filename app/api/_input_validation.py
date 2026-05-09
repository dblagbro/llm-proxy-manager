"""
v3.5.8 — front-line input validation for /v1/messages + /v1/chat/completions.

Closes BUG-005 (empty body returns 200 with auto-substituted model) and
BUG-004 (missing model/messages returns 502 leaking upstream errors).

Pre-v3.5.8 each endpoint did ``body = await request.json()`` and then
``body.get("model")``, ``body.get("messages", [])`` with safe defaults.
A caller sending ``{}`` would route to the priority-1 default provider,
spend real budget, and get a 200 back with a substituted model. That's
a denial-of-wallet vector if any API key ever leaked.

This helper validates the basic request shape AT THE INPUT BOUNDARY so:
  - empty body / missing required fields → HTTP 400 with clear message
  - malformed values (negative max_tokens, invalid role) → HTTP 400
    BEFORE we attempt dispatch, so upstream errors don't leak through

The validation is INTENTIONALLY permissive on edge cases the proxy
already handles correctly (auto-routing with model="auto", flexible
content shapes). We only reject what is unambiguously broken.
"""
from __future__ import annotations

from fastapi import HTTPException


# Anthropic and OpenAI both use these roles. Different shapes for content
# (Anthropic allows list-of-blocks; OpenAI accepts string + tool_calls)
# but role enum is shared.
_VALID_ROLES = frozenset({"system", "user", "assistant", "tool", "function"})


def validate_completion_request(body: dict, *, endpoint: str) -> None:
    """Validate a chat-completions / messages request body.

    Raises ``HTTPException(400)`` with a clean ``{"detail": "..."}`` message
    when the body is unambiguously broken. Returns silently on success.

    Args:
        body: parsed JSON body from the request
        endpoint: ``"messages"`` (Anthropic shape) or ``"completions"``
            (OpenAI shape) — used in error messages only

    Validation rules (intentionally minimal — only reject the
    unambiguous):

    1. ``body`` must be a dict (not None, list, scalar)
    2. ``body["model"]`` must be present and truthy (string or
       ``"auto"`` / ``"llmp-auto"`` for auto-routing)
    3. ``body["messages"]`` must be a non-empty list
    4. Each message must have a valid ``role`` value in the union of
       Anthropic + OpenAI roles
    5. ``max_tokens`` (if present) must be a positive integer

    Anything else is left for the dispatch layer to handle. We do NOT
    validate ``content`` shape (Anthropic accepts string OR list, OpenAI
    accepts string OR list of parts — too divergent for input layer).
    """
    if not isinstance(body, dict):
        raise HTTPException(
            400, f"{endpoint}: request body must be a JSON object",
        )

    # 1. model required (or "auto")
    model = body.get("model")
    if not model or not isinstance(model, str):
        raise HTTPException(
            400,
            f"{endpoint}: 'model' field is required and must be a non-empty string. "
            "Use 'auto' or 'llmp-auto' to opt into automatic provider selection.",
        )

    # 2. messages required + non-empty
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(
            400, f"{endpoint}: 'messages' must be an array",
        )
    if len(messages) == 0:
        raise HTTPException(
            400,
            f"{endpoint}: 'messages' must contain at least one entry. "
            "Send a system + user pair, or at minimum a single user message.",
        )

    # 3. role validation (per-message)
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise HTTPException(
                400,
                f"{endpoint}: messages[{i}] must be an object with 'role' and 'content'",
            )
        role = msg.get("role")
        if not role or not isinstance(role, str) or role not in _VALID_ROLES:
            raise HTTPException(
                400,
                f"{endpoint}: messages[{i}].role={role!r} is invalid. "
                f"Valid roles: {sorted(_VALID_ROLES)}",
            )

    # 4. max_tokens must be positive if present
    if "max_tokens" in body:
        mt = body["max_tokens"]
        if not isinstance(mt, int) or isinstance(mt, bool) or mt < 1:
            raise HTTPException(
                400,
                f"{endpoint}: 'max_tokens' must be a positive integer (got {mt!r})",
            )

    # 5. v3.5.11 BUG-015 — cap stop_sequences at 16 entries.
    # Anthropic and OpenAI both error past ~4-16; pre-validation
    # saves an upstream round-trip and gives the caller a clearer
    # error than ``upstream 400: too many stop sequences``.
    # T23 reproduced: 1000 entries returned 200 (silently truncated
    # by upstream? wasteful either way).
    ss = body.get("stop_sequences") if "stop_sequences" in body else body.get("stop")
    if ss is not None:
        if isinstance(ss, list) and len(ss) > 16:
            raise HTTPException(
                400,
                f"{endpoint}: 'stop_sequences'/'stop' may have at most 16 "
                f"entries (got {len(ss)})",
            )


def validate_webhook_url(url: str) -> None:
    """v3.5.11 BUG-013 — reject unsafe webhook URL schemes.

    Pre-fix: ``X-Webhook-URL: file:///etc/passwd`` was accepted and
    httpx would attempt to read the local file. Same for ``gopher://``,
    ``data:``, ``ftp://``, etc. SSRF surface: ``http://localhost:*``
    or ``http://169.254.169.254`` (cloud metadata) — though those
    require network access from the proxy container.

    Strategy: require ``http://`` or ``https://`` scheme. We do NOT
    block private IPs by default — operators may legitimately POST
    to internal infra (their hub, their alerting webhook). Document
    that and let network policy enforce egress restrictions.

    Raises ``HTTPException(400)`` on invalid scheme. Returns silently
    on success.
    """
    if not url:
        return
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(400, "x-webhook-url: invalid URL")
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(
            400,
            f"x-webhook-url: scheme must be http:// or https:// "
            f"(got {parsed.scheme!r})",
        )
    if not parsed.netloc:
        raise HTTPException(400, "x-webhook-url: missing host")


def sanitize_upstream_error(error_text: str, max_chars: int = 300) -> str:
    """Convert a raw upstream / litellm exception string into a caller-safe
    error message. Strips file paths, line numbers, Python tracebacks, and
    anything else that leaks proxy infrastructure.

    Closes BUG-007 (litellm stack trace on invalid role) and BUG-008
    (Gemini error trace on negative max_tokens). Pre-v3.5.8 the proxy
    returned ``str(exc)`` directly, leaking ``/usr/local/lib/python3.13/
    site-packages/litellm/...`` paths to anyone who sent malformed input.

    Strategy:
      1. Drop any line containing ``File "..."`` — Python traceback marker
      2. Drop any line containing ``Traceback (most recent call last)``
      3. Drop file paths anywhere in the remaining text
      4. Truncate to ``max_chars`` so a chatty upstream doesn't leak more
         than a couple sentences worth

    Returns a string suitable for inclusion in HTTP 4xx/5xx ``detail``.
    Empty input returns a generic "upstream error" string.
    """
    if not error_text:
        return "upstream provider error (empty)"
    import re
    # Drop traceback frames + file references
    cleaned_lines: list[str] = []
    for raw in str(error_text).splitlines():
        ln = raw.strip()
        if not ln:
            continue
        if ln.startswith("Traceback "):
            continue
        if ln.startswith('File "'):
            continue
        # Strip ``/path/to/file.py:NNN`` markers within a line
        ln = re.sub(r'/[a-zA-Z0-9_./-]+\.py:\d+', '<...>', ln)
        # Strip standalone absolute paths
        ln = re.sub(r'/[a-zA-Z0-9_./-]+/[a-zA-Z0-9_./-]+\.py', '<...>', ln)
        cleaned_lines.append(ln)
    text = " ".join(cleaned_lines).strip()
    if not text:
        return "upstream provider error"
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text
