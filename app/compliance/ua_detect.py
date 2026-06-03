"""Client User-Agent → owning-company detection (v5.0.0).

Powers the 451 path in ``app/api/messages.py`` and ``completions.py``: if
the request's UA is a product of a banned company, refuse the request
deterministically (HTTP 451 — Unavailable For Legal Reasons), regardless
of which model was asked for.

Decision 16 + 22. Case-insensitive matching. Custom companies from
``SystemSetting.compliance_custom_companies`` are merged with
``KNOWN_COMPANIES`` at detection time.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from app.compliance.company_map import KNOWN_COMPANIES


def _product_label(matched_value: str) -> str:
    """Best-effort human label for the matched product, derived from the
    pattern value. e.g. ``claude-cli/`` → ``claude-cli``,
    ``anthropic-sdk-python/`` → ``anthropic-sdk-python``,
    ``@anthropic-ai/claude-code`` → ``@anthropic-ai/claude-code``.
    """
    if not matched_value:
        return "unknown"
    # Strip trailing version-delimiter slash
    if matched_value.endswith("/"):
        return matched_value[:-1]
    # Regex patterns: surface as-is (operator can read the pattern)
    return matched_value


def detect_client_company(
    user_agent: Optional[str],
    custom_companies: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[Tuple[str, str, str]]:
    """Match a UA against all known + custom UA patterns.

    Returns ``(company_id, matched_pattern_value, product_label)`` on the
    first match, else ``None``. Patterns are checked in dictionary
    insertion order (KNOWN_COMPANIES first, then custom).

    The match short-circuits on the first hit — UAs that could match
    multiple companies (rare; would mean operator overlap in custom
    patterns) attribute to the first matched company.
    """
    if not user_agent:
        return None
    ua_lower = user_agent.lower()
    companies = dict(KNOWN_COMPANIES)
    if custom_companies:
        companies.update(custom_companies)
    for company_id, info in companies.items():
        for rule in info.get("ua_patterns", []):
            ptype = rule.get("type")
            value = rule.get("value", "")
            if not value:
                continue
            value_lower = value.lower()
            if ptype == "prefix":
                if ua_lower.startswith(value_lower):
                    return (company_id, value, _product_label(value))
            elif ptype == "contains":
                if value_lower in ua_lower:
                    return (company_id, value, _product_label(value))
            elif ptype == "exact":
                if ua_lower == value_lower:
                    return (company_id, value, _product_label(value))
            elif ptype == "regex":
                try:
                    if re.search(value, user_agent, re.IGNORECASE):
                        return (company_id, value, _product_label(value))
                except re.error:
                    continue
    return None


__all__ = ["detect_client_company"]
