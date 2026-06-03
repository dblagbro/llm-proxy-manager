"""Effective blocklist + provider filter (v5.0.0).

The two functions that callers actually use:

- ``get_effective_blocklist(db, api_key_id)`` — union of per-key
  ``blocked_companies`` and the system-wide
  ``compliance_system_blocked_companies`` setting. Cached for 30s per key;
  invalidated on PATCH /api/keys/{id} and PATCH /api/settings.
- ``filter_providers(providers, blocklist, requested_model)`` — drops
  providers whose ``owner_company`` is banned OR whose resolved model
  family is banned. Raises ``ComplianceNoSubstituteError`` if the filter
  empties an originally non-empty list (decision 4 — converted to HTTP 503
  by ``select_provider_with_503``).

Plus two exception classes the dispatch layer catches:

- ``ComplianceNoSubstituteError`` — 503 path
- ``ComplianceUaBlockedError`` — 451 path (raised from messages.py /
  completions.py once the UA detection fires, carrying the audit_id for
  downstream serialization)
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import select

from app.compliance.company_map import (
    KNOWN_COMPANIES,
    model_family_to_company,
    model_family_companies,
)
from app.models.db import ApiKey


class ComplianceNoSubstituteError(Exception):
    """Raised by ``filter_providers`` when every candidate is blocked by
    policy. The dispatch layer catches and returns HTTP 503 (decision 4)
    with ``X-Compliance-Refusal: true``.
    """

    def __init__(self, message: str, blocked_companies: Set[str], n_dropped: int):
        super().__init__(message)
        self.blocked_companies = blocked_companies
        self.n_dropped = n_dropped


class ComplianceUaBlockedError(Exception):
    """Raised when the incoming request's User-Agent matches a banned
    client product (decision 16). Carries enough fields for messages.py
    / completions.py to serialize the 451 response + audit row in one
    place.
    """

    def __init__(
        self,
        *,
        matched_company: str,
        matched_pattern: str,
        matched_product: str,
        audit_id: str,
    ):
        super().__init__(
            f"client product '{matched_product}' is a product of banned company '{matched_company}'"
        )
        self.matched_company = matched_company
        self.matched_pattern = matched_pattern
        self.matched_product = matched_product
        self.audit_id = audit_id


_BLOCKLIST_CACHE: Dict[str, tuple] = {}
_CACHE_TTL_SEC = 30.0


def invalidate_blocklist_cache(api_key_id: Optional[str] = None) -> None:
    """Called from:
    - apikeys.py update_key() when ``blocked_companies`` changes
    - settings_api.py put_settings() when
      ``compliance_system_blocked_companies`` changes
    - cluster/sync.py apply when either of the above syncs in from a peer
    Passing ``None`` clears the entire cache (used by settings update).
    """
    if api_key_id is None:
        _BLOCKLIST_CACHE.clear()
    else:
        _BLOCKLIST_CACHE.pop(api_key_id, None)


def _get_system_blocklist() -> Set[str]:
    """Pull the cluster-synced system-wide blocklist from the live
    ``settings`` singleton. Lazy import to avoid an import cycle at
    module load.

    The value is stored as a JSON-encoded string in
    ``settings.compliance_system_blocked_companies`` (so it round-trips
    through the same SQLite-backed key/value store the other settings
    use). Empty string / parse error → empty set (fail open at the
    type-coercion layer; the rest of the system fails closed by checking
    for explicit company IDs).
    """
    import json
    try:
        from app.config import settings
        raw = getattr(settings, "compliance_system_blocked_companies", "") or ""
        if not raw or not raw.strip():
            return set()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {str(x) for x in parsed if x}
        return set()
    except Exception:
        return set()


def get_custom_companies() -> Dict[str, Dict[str, Any]]:
    """Custom companies from ``settings.compliance_custom_companies``
    (JSON-encoded list). Merged with KNOWN_COMPANIES at every UA / filter
    check.
    """
    import json
    try:
        from app.config import settings
        raw = getattr(settings, "compliance_custom_companies", "") or ""
        if not raw or not raw.strip():
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return {}
        out = {}
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            cid = entry.get("id")
            if cid:
                out[cid] = entry
        return out
    except Exception:
        return {}


async def get_effective_blocklist(db, api_key_id: str) -> Set[str]:
    """Union of per-key ``blocked_companies`` and the system-wide setting.

    Cached for 30s per api_key_id; invalidated by edits on either side.
    Returns an empty set when the key has no policy and the system has
    none (so callers can skip filtering with a single truthiness check).
    """
    if not api_key_id:
        return _get_system_blocklist()
    now = time.monotonic()
    cached = _BLOCKLIST_CACHE.get(api_key_id)
    if cached and cached[0] > now:
        return cached[1]
    try:
        result = await db.execute(
            select(ApiKey.blocked_companies).where(ApiKey.id == api_key_id)
        )
        row = result.scalar_one_or_none()
        per_key: Set[str] = set()
        if row:
            if isinstance(row, list):
                per_key = {str(x) for x in row if x}
            elif isinstance(row, str):
                # JSON-string fallback (some sync paths persist as text)
                import json
                try:
                    parsed = json.loads(row)
                    if isinstance(parsed, list):
                        per_key = {str(x) for x in parsed if x}
                except Exception:
                    pass
    except Exception:
        per_key = set()
    system = _get_system_blocklist()
    effective = per_key | system
    _BLOCKLIST_CACHE[api_key_id] = (now + _CACHE_TTL_SEC, effective)
    return effective


def filter_providers(
    providers: List[Any],
    blocklist: Set[str],
    requested_model: Optional[str] = None,
) -> List[Any]:
    """Drop banned providers; raise ``ComplianceNoSubstituteError`` if all
    are dropped.

    Drops a provider on EITHER:
    - ``provider.owner_company`` is in the blocklist (decision 14: provider
      ownership), OR
    - ``model_family_to_company(provider.default_model)`` is in the
      blocklist (decision 11: catches Bedrock serving Anthropic where
      ``owner_company='aws'`` isn't banned but the model lineage IS).

    NOTE on ``requested_model``: it is accepted for API symmetry but is
    NOT used as a filter predicate per provider. The substitution path
    works by leaving the non-banned providers in place; the router's
    existing cross-family-fallback logic at messages.py / completions.py
    detects "asked model can't be served by any survivor, swap to the
    survivor's family" and emits the disclosure. Using ``requested_model``
    here would self-defeat: a caller asking for ``claude-haiku`` would
    cause every non-Anthropic provider to fail the family check too,
    raising ``ComplianceNoSubstituteError`` instead of letting the router
    substitute. (Caught during v5.0.0 router wiring.)
    """
    if not blocklist:
        return list(providers)
    out = []
    for p in providers:
        owner = getattr(p, "owner_company", None)
        if owner and owner in blocklist:
            continue
        provider_default_model = getattr(p, "default_model", None)
        # Multi-company match — a Bedrock-Anthropic model triggers BOTH
        # the ``anthropic`` and ``aws`` bans (decision 11 in spec).
        families = model_family_companies(provider_default_model)
        if families & blocklist:
            continue
        out.append(p)
    if not out and providers:
        raise ComplianceNoSubstituteError(
            f"All {len(providers)} candidates blocked by compliance policy; "
            f"banned companies: {sorted(blocklist)}",
            blocked_companies=blocklist,
            n_dropped=len(providers),
        )
    return out


def is_company_banned(company_id: Optional[str], blocklist: Set[str]) -> bool:
    """Convenience for callers (cache + memory layers) that have already
    resolved a ``source_company`` and want a single-line check.
    Decision 7 — unknown (NULL) source_company is treated as banned by any
    non-empty blocklist."""
    if not blocklist:
        return False
    if company_id is None:
        return True
    return company_id in blocklist


__all__ = [
    "ComplianceNoSubstituteError",
    "ComplianceUaBlockedError",
    "get_effective_blocklist",
    "get_custom_companies",
    "invalidate_blocklist_cache",
    "filter_providers",
    "is_company_banned",
]
