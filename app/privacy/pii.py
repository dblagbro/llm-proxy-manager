"""PII masking — Wave 6 privacy feature.

Regex-based masking of common PII patterns (email, US SSN, credit card, US
phone, IPv4) in inbound request content. Heavyweight NLP-based masking
(Presidio, NeMo-Guardrails) is out of scope — this is a defensive first-line
filter that runs with zero extra dependencies.

Enabled globally via settings.pii_masking_enabled. When disabled, the
`mask_text` and `mask_messages` functions become no-ops.

Each masked token is replaced by a placeholder that carries enough type
info for the model to reason about it:
    [EMAIL_REDACTED]
    [SSN_REDACTED]
    [CC_REDACTED]
    [PHONE_REDACTED]
    [IP_REDACTED]

The number of replacements is returned so it can be emitted as an
`X-PII-Masked: <n>` header or activity-log event.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Ordered — credit card before phone, because "4111 1111 1111 1111" can
# partially match the phone pattern; SSN before phone for the same reason.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Email: RFC 5322-lite (sufficient for >99% of real-world formats)
    ("EMAIL", re.compile(
        r"(?P<value>[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})"
    )),
    # Credit card: 13-19 digits with optional spaces/dashes (Luhn-unchecked)
    ("CC", re.compile(
        r"(?P<value>\b(?:\d[ -]*?){13,19}\b)"
    )),
    # US SSN: 3-2-4 with dash or space. Require a word boundary.
    ("SSN", re.compile(
        r"(?P<value>\b\d{3}[- ]\d{2}[- ]\d{4}\b)"
    )),
    # US phone: optional +1, optional (, 3 digits, optional ), dash/space/dot, 3, dash/space/dot, 4
    ("PHONE", re.compile(
        r"(?P<value>\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}\b)"
    )),
    # IPv4
    ("IP", re.compile(
        r"(?P<value>\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b)"
    )),
]


@dataclass
class MaskResult:
    text: str
    count: int


def mask_text(text: str) -> MaskResult:
    """Apply all registered PII patterns to `text`. Returns masked text + count."""
    if not text:
        return MaskResult(text=text or "", count=0)
    total = 0
    for name, pattern in _PATTERNS:
        replacement = f"[{name}_REDACTED]"
        new_text, n = pattern.subn(replacement, text)
        total += n
        text = new_text
    return MaskResult(text=text, count=total)


def mask_messages(messages: list[dict]) -> tuple[list[dict], int]:
    """Apply mask_text to every string-valued content field in a messages list.
    Handles both plain-string content and OpenAI/Anthropic part-list formats.

    Returns (new_messages, total_pii_count).
    """
    if not messages:
        return messages, 0
    out: list[dict] = []
    total = 0
    for msg in messages:
        new_msg = dict(msg)
        content = msg.get("content")
        if isinstance(content, str):
            r = mask_text(content)
            new_msg["content"] = r.text
            total += r.count
        elif isinstance(content, list):
            new_parts = []
            for part in content:
                if isinstance(part, dict):
                    # Anthropic {"type":"text","text":"..."} or OpenAI {"type":"text","text":"..."}
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        r = mask_text(part["text"])
                        new_part = dict(part)
                        new_part["text"] = r.text
                        total += r.count
                        new_parts.append(new_part)
                    else:
                        new_parts.append(part)
                else:
                    new_parts.append(part)
            new_msg["content"] = new_parts
        out.append(new_msg)
    return out, total


def is_enabled() -> bool:
    from app.config import settings
    return bool(getattr(settings, "pii_masking_enabled", False))
