"""Cache key / namespace construction + eligibility heuristics.

Namespace structure (hashed, so prompt search only matches within):
    tenant × model × model-version × temperature-bucket × tool-set × system-prompt

Embed key: the LAST user turn's text only. Prior-turn context is bucketed
into the namespace hash; semantic similarity measures what the user is
asking NOW, not the whole transcript.
"""
import hashlib
import json
import re
from typing import Optional


# PII heuristics — deliberately conservative; false positives are cheaper
# than leaking a cached PII response to another tenant.
_PII_PATTERNS = [
    re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),            # email
    re.compile(r"\b\d{3}[-. ]?\d{2}[-. ]?\d{4}\b"),         # SSN-shaped
    re.compile(r"\b(?:\d[ -]*?){13,19}\b"),                  # credit-card-shaped
    re.compile(r"\+?\d{1,3}?[-. ]?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}"),  # phone
]


def contains_pii(text: str) -> bool:
    if not text:
        return False
    for pat in _PII_PATTERNS:
        if pat.search(text):
            return True
    return False


def is_cacheable_temperature(temperature: Optional[float]) -> bool:
    """Only cache when output is effectively deterministic.

    T=0 → safe, T≤0.3 → low-variance, T>0.3 → skip to preserve
    statistical independence per arXiv:2511.22118.
    """
    if temperature is None:
        return True  # default T=1.0 for most providers, but absence typically = they want a single answer
    return temperature <= 0.3


def _hash(obj) -> str:
    data = json.dumps(obj, sort_keys=True, default=str).encode()
    return hashlib.sha256(data).hexdigest()[:16]


def build_namespace(
    *,
    tenant_id: str,
    model: str,
    system: Optional[object],
    tools: Optional[list],
    temperature: Optional[float],
    prior_messages: list[dict],
) -> str:
    """Build the Redis namespace key. Semantic search runs only within it.

    tenant_id should be the API-key ID (already opaque + per-tenant).
    prior_messages excludes the last user turn (which becomes the embed key).
    """
    temp_bucket = "0" if (temperature or 0) == 0 else "low" if (temperature or 0) <= 0.3 else "high"
    return ":".join([
        "smc",  # semantic-cache prefix
        tenant_id,
        model,
        temp_bucket,
        _hash(system or ""),
        _hash(tools or []),
        _hash(prior_messages or []),
    ])


def extract_query_text(messages: list[dict]) -> str:
    """Extract the text we embed for semantic lookup — the LAST user turn.

    Handles both string content and list-of-blocks content; for blocks
    we concatenate text blocks only (image blocks don't contribute to
    the semantic key).
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
            return "\n".join(parts).strip()
    return ""


def split_prior_messages(messages: list[dict]) -> tuple[list[dict], str]:
    """Return (messages_before_last_user, last_user_text)."""
    idx = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            idx = i
            break
    if idx is None:
        return messages, ""
    prior = messages[:idx]
    content = messages[idx].get("content", "")
    if isinstance(content, str):
        text = content
    else:
        text = "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return prior, text.strip()
