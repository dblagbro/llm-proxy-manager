"""Semantic prompt guard — Wave 6 safety feature.

Kong-style denylist check: rejects requests whose content matches any
pattern in a configurable denylist. Patterns are simple substring matches
(case-insensitive) to keep the guard lightweight; a heavier semantic-
similarity guard would build on the existing semantic-cache embedding
pipeline.

Configure via:
  settings.prompt_guard_enabled     — master toggle
  settings.prompt_guard_denylist    — comma-separated patterns

Example PROMPT_GUARD_DENYLIST="ignore previous instructions,system prompt,reveal your"

Returns a tuple (ok, matched_pattern). A denylisted request is rejected
with HTTP 400 in the endpoint handler that invokes check_messages().
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def _load_denylist() -> list[str]:
    from app.config import settings
    raw = getattr(settings, "prompt_guard_denylist", None) or ""
    if not raw:
        return []
    return [p.strip().lower() for p in raw.split(",") if p.strip()]


def is_enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "prompt_guard_enabled", False))


def check_text(text: str, denylist: Optional[list[str]] = None) -> Optional[str]:
    """Return the matching denylist entry, or None if clean."""
    if not text:
        return None
    patterns = denylist if denylist is not None else _load_denylist()
    if not patterns:
        return None
    low = text.lower()
    for p in patterns:
        if p in low:
            return p
    return None


def check_messages(
    messages: list[dict], denylist: Optional[list[str]] = None,
) -> Optional[str]:
    """Return the first matching denylist entry across all messages, or None."""
    if not messages:
        return None
    patterns = denylist if denylist is not None else _load_denylist()
    if not patterns:
        return None

    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            hit = check_text(content, patterns)
            if hit:
                return hit
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    hit = check_text(part.get("text", ""), patterns)
                    if hit:
                        return hit
    return None
