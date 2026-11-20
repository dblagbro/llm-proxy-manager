"""v5.21.6 — Buffered-cascade mode detection.

v5.21.9 hotfix — decoupled from ``resp_headers``. The v5.21.6 version
wrote the ``X-Refusal-Cascade-Mode`` header directly into a
``resp_headers`` dict passed in, which forced the CALLER to have
``resp_headers`` in scope BEFORE calling this helper. In messages.py
that isn't true — the caller detects the mode very early (right after
parsing the request body) but ``resp_headers`` isn't built until
much later (line 476-ish via ``build_base_response_headers``). Result:
``UnboundLocalError: cannot access local variable 'resp_headers'``
on EVERY /v1/messages request. 500-ing the endpoint that carries the
majority of traffic. Shipped in v5.21.6, discovered 2026-07-16.

New contract: this function is PURE. It returns the two mode flags
plus the header VALUE to set (or None); the caller decides when to
apply it to their response-headers dict.

Trade-offs:

- Retry disabled: no mode header; v5.21.0 pass-through streaming.
- Retry enabled without heartbeat: ``buffered``; full non-streaming path,
  including tool hops, memory, MCP and tail. The client sees no bytes until
  dispatch completes.
- Retry enabled with heartbeat: ``buffered-heartbeat``; minimal LLM dispatch
  and cascade. Clients see ``: cascade-buffering`` and a ``: keepalive``
  every five seconds.

See also:
  - ``app/api/_buffered_cascade_stream.py`` — the heartbeat-mode helper
    that ``messages.py`` dispatches to when ``buffered_heartbeat=True``.
  - CHANGELOG entries for v5.20.11, v5.21.3, v5.21.6, v5.21.9.
"""

from __future__ import annotations

from typing import Any


def detect_buffered_cascade_mode(
    stream: bool,
    key_record: Any,
) -> tuple[bool, bool, str | None]:
    """Detect whether the request should route through one of the two
    buffered-cascade modes.

    Args:
        stream: The caller's requested ``stream`` param from body.
        key_record: The API key row for this request.

    Returns:
        ``(buffered_stream, buffered_heartbeat, mode_header_value)``:
        - ``buffered_stream``: True → handler MUST force ``stream = False``
        - ``buffered_heartbeat``: True → handler MUST early-return
          via the heartbeat helper (see _buffered_cascade_stream.py)
        - ``mode_header_value``: ``"buffered"`` / ``"buffered-heartbeat"``
          / None. Caller sets ``resp_headers["X-Refusal-Cascade-Mode"]``
          to this value AFTER resp_headers has been built.
    """
    buffered_stream = stream and getattr(
        key_record,
        "refusal_retry_enabled",
        False,
    )
    if not buffered_stream:
        return False, False, None

    buffered_heartbeat = bool(
        getattr(
            key_record,
            "refusal_retry_streaming_heartbeat",
            False,
        )
    )
    header_value = "buffered-heartbeat" if buffered_heartbeat else "buffered"
    return True, buffered_heartbeat, header_value
