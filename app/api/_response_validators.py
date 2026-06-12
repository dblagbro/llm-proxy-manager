"""v5.0.24 / remediation Batch 3 — generic empty-success guard.

Some upstream providers (notably ``cursor-bridge`` for the
cursor-oauth provider type — see BUG-053) return HTTP 200 with empty
content AND zero token usage AND a structured error JSON embedded in
the body. From the proxy's perspective this looks like success and
gets forwarded to the caller, who sees an empty response with no
error signal.

Cursor's failure mode (observed 2026-06-05):
  - Account downgraded Pro → Free.
  - All requests get 200 OK with body
    ``{"error":{"code":"resource_exhausted",
       "details":[{"debug":{"error":"ERROR_RATE_LIMITED_CHANGEABLE",
       "details":{"title":"Named models unavailable",
       "detail":"Free plans can only use Auto."}}}]}}``.
  - ``cursor-bridge`` wraps these as HTTP 200 because it preserves
    the body shape; the proxy then forwards the empty completion to
    the caller.

This guard runs after the dispatch but before the response is
forwarded to the caller. It looks for the empty-content + zero-token
+ embedded-error pattern and raises a 502 so the caller (and the
routing layer) see an actual failure.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Known upstream error markers that indicate "the call wasn't really
# successful". Extend this list as new patterns are observed.
_ERROR_MARKERS = (
    "ERROR_RATE_LIMITED_CHANGEABLE",
    "ERROR_BAD_MODEL_NAME",  # v5.3.8 — Cursor "Model name is not valid"
    "resource_exhausted",
    "Named models unavailable",
    "Free plans can only use Auto",
    '"error":{',  # generic — any embedded error object
)


def _content_is_empty(choices: list) -> bool:
    """OpenAI-shape: choices[0].message.content empty/blank."""
    if not choices:
        return True
    try:
        msg = choices[0].get("message") or {}
        content = msg.get("content")
    except (AttributeError, TypeError):
        return True
    if content is None:
        return True
    if isinstance(content, str):
        return content.strip() == ""
    # Anthropic-shape: content is list[{type, text}]
    if isinstance(content, list):
        joined = " ".join(
            (block.get("text") or "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ).strip()
        return joined == ""
    return False


def _zero_tokens(usage: dict) -> bool:
    if not isinstance(usage, dict):
        return True
    # Treat missing/None/0 in all relevant fields as "zero"
    fields = ("prompt_tokens", "completion_tokens", "total_tokens",
              "input_tokens", "output_tokens")
    return all(
        (usage.get(f) or 0) == 0 for f in fields if f in usage
    ) or all(
        (usage.get(f) or 0) == 0 for f in fields
    )


def _body_carries_error_marker(body_text: Optional[str]) -> bool:
    if not body_text:
        return False
    return any(marker in body_text for marker in _ERROR_MARKERS)


def _prompt_tokens_zero(usage: dict) -> bool:
    """The strongest fail-signal: an LLM can return an empty completion
    legitimately, but ``prompt_tokens == 0`` means the upstream didn't
    even process the user's prompt — a real call cannot have zero
    input tokens. Checks both OpenAI (``prompt_tokens``) and
    Anthropic (``input_tokens``) field names.
    """
    if not isinstance(usage, dict):
        return False
    if "prompt_tokens" in usage:
        return (usage.get("prompt_tokens") or 0) == 0
    if "input_tokens" in usage:
        return (usage.get("input_tokens") or 0) == 0
    return False


def looks_like_empty_success_failure(
    *, response_dict: dict, raw_body: Optional[str] = None,
) -> bool:
    """Return True when the response looks like a masked upstream failure.

    Criteria — any ONE of the following constitutes a masked failure
    when content is empty:

      A. ``prompt_tokens == 0`` (OpenAI) or ``input_tokens == 0``
         (Anthropic) — physically impossible for a real LLM call.
         The upstream didn't process the prompt at all; the response
         is synthetic / a wrapped error. CAUGHT BUG-053 (cursor-bridge
         masks Cursor's "named models unavailable" error this way:
         http 200 + empty content + ALL token counts = 0).
      B. The response_dict contains an ``error`` key (some bridges
         leak through the upstream error structure).
      C. The raw_body contains a known upstream error marker
         (``ERROR_RATE_LIMITED_CHANGEABLE``, ``resource_exhausted``,
         etc).

    All three conditions also require empty content + zero token
    counts as a precondition — we never flag a response that did real
    work.

    Conservative: returns False if any precondition can't be
    evaluated.
    """
    try:
        # v5.3.8 — shapeless error body: a 2xx response with NEITHER
        # ``choices`` (OpenAI) NOR ``content`` (Anthropic) but an
        # ``error`` key is a wrapped upstream failure, full stop. The
        # cursor-bridge passes upstream error JSON through with HTTP 200
        # and no completion shape at all; pre-v5.3.8 this fell through
        # every branch below and forwarded as success.
        if (
            isinstance(response_dict, dict)
            and "choices" not in response_dict
            and "content" not in response_dict
            and response_dict.get("error")
        ):
            return True

        # OpenAI shape first
        choices = response_dict.get("choices") if isinstance(response_dict, dict) else None
        if choices is not None:
            if not _content_is_empty(choices):
                return False
            usage = response_dict.get("usage") or {}
            if not _zero_tokens(usage):
                return False
            # A. zero prompt tokens — strongest signal
            if _prompt_tokens_zero(usage):
                return True
            # B. explicit error key
            if response_dict.get("error"):
                return True
            # C. error marker in raw body
            return _body_carries_error_marker(raw_body)

        # Anthropic /v1/messages shape
        if "content" in response_dict:
            content = response_dict.get("content")
            if isinstance(content, list):
                joined = " ".join(
                    (b.get("text") or "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ).strip()
                if joined != "":
                    return False
                usage = response_dict.get("usage") or {}
                if not _zero_tokens(usage):
                    return False
                if _prompt_tokens_zero(usage):
                    return True
                if response_dict.get("error"):
                    return True
                return _body_carries_error_marker(raw_body)
    except Exception as exc:
        logger.debug("response_validator.eval_failed err=%r", exc)
    return False


def empty_success_failure_message(response_dict: dict,
                                  raw_body: Optional[str] = None) -> str:
    """Return a short, log-safe summary for the failure path."""
    err = (response_dict or {}).get("error") if isinstance(response_dict, dict) else None
    if err:
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code") or str(err)
            return f"empty-success failure (upstream error: {str(msg)[:160]})"
        return f"empty-success failure (upstream error: {str(err)[:160]})"
    # Try to extract a marker from raw_body for diagnostic
    if raw_body:
        for marker in _ERROR_MARKERS:
            if marker in raw_body:
                return f"empty-success failure (marker: {marker})"
    return "empty-success failure (no error marker found)"
