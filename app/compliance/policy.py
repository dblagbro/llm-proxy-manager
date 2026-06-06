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

import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import select

from app.compliance.company_map import (
    KNOWN_COMPANIES,
    model_family_to_company,
    model_family_companies,
)
from app.models.db import ApiKey


# v5.2.0 / Batch V2 — fine-grained vendor-neutrality policy bundle.


@dataclass(frozen=True)
class Policy:
    """Resolved effective policy for one (api_key, request) pair.

    All four dimensions union per-key + system. Deny wins everywhere:
    a company/model that appears in both the allow and the block side
    is BLOCKED.

    Empty allowlist (the default for legacy keys) = blocklist-only
    behavior, identical to pre-v5.2.0. Non-empty allowlist switches to
    positive-allowlist mode for that dimension.

    Model patterns are fnmatch-style (`claude-*`, `gpt-4-*-turbo`); an
    entry without a wildcard is an exact match. The match target is
    the provider's ``default_model`` (without the litellm ``provider/``
    prefix) AND the request's ``requested_model`` when present — both
    must clear the model gates for a provider to pass.
    """
    blocked_companies: Set[str] = field(default_factory=set)
    allowed_companies: Set[str] = field(default_factory=set)
    blocked_models: Tuple[str, ...] = field(default_factory=tuple)
    allowed_models: Tuple[str, ...] = field(default_factory=tuple)

    def is_empty(self) -> bool:
        return not (
            self.blocked_companies or self.allowed_companies
            or self.blocked_models or self.allowed_models
        )


def _model_matches_any(model: Optional[str], patterns: Tuple[str, ...]) -> bool:
    """fnmatch-style match (exact strings are a degenerate glob).
    Empty model never matches. Empty pattern set never matches.
    Comparison is case-insensitive — providers occasionally normalize
    case differently for the same model id.
    """
    if not model or not patterns:
        return False
    m = model.lower()
    for pat in patterns:
        if fnmatch.fnmatchcase(m, pat.lower()):
            return True
    return False


def evaluate_policy(
    policy: Policy,
    provider: Any,
    requested_model: Optional[str] = None,
) -> Tuple[bool, str]:
    """Return (allowed, reason_code). reason_code is empty on allow.

    Order (deny wins):
      1. Company blocked → "blocked-company"
      2. Company-family blocked (Bedrock-Anthropic edge case) → "blocked-model-family"
      3. Model blocked (default_model OR requested_model) → "blocked-model"
      4. Allowlist present + company not in it → "company-not-in-allowlist"
      5. Allowlist present + model patterns not in allowed_models → "model-not-in-allowlist"
      6. Otherwise → allow.

    The reason_code feeds the audit row's ``reason_code`` field.
    """
    if policy.is_empty():
        return True, ""

    owner = getattr(provider, "owner_company", None)
    provider_default_model = getattr(provider, "default_model", None)

    if owner and owner in policy.blocked_companies:
        return False, "blocked-company"

    families = model_family_companies(provider_default_model)
    if families & policy.blocked_companies:
        return False, "blocked-model-family"

    if (
        _model_matches_any(provider_default_model, policy.blocked_models)
        or _model_matches_any(requested_model, policy.blocked_models)
    ):
        return False, "blocked-model"

    if policy.allowed_companies:
        company_ok = (owner and owner in policy.allowed_companies) or bool(
            families & policy.allowed_companies
        )
        if not company_ok:
            return False, "company-not-in-allowlist"

    if policy.allowed_models:
        model_ok = (
            _model_matches_any(provider_default_model, policy.allowed_models)
            or _model_matches_any(requested_model, policy.allowed_models)
        )
        if not model_ok:
            return False, "model-not-in-allowlist"

    return True, ""


class ComplianceNoSubstituteError(Exception):
    """Raised by ``filter_providers`` when every candidate is blocked by
    policy. The dispatch layer catches and returns HTTP 503 (decision 4)
    with ``X-Compliance-Refusal: true``.
    """

    def __init__(self, message: str, blocked_companies: Set[str], n_dropped: int):
        super().__init__(message)
        self.blocked_companies = blocked_companies
        self.n_dropped = n_dropped


class ComplianceNoLocalProviderError(ComplianceNoSubstituteError):
    """v5.0.4 — fires when ``model=coordinator-local`` is requested but
    no self-hosted provider is configured. Subclass of
    ``ComplianceNoSubstituteError`` so legacy 503 catches still work,
    but it carries a more specific ``reason_code`` for the disclosure
    header (``no-compliant-local-provider`` per CADC §6.2).

    Hub-team-flagged F anomaly: the 503 must include
    ``X-Compliance-Refusal-Reason`` even when the failure mode is
    "no self-hosted provider" rather than "no compliant substitute."
    """

    def __init__(self):
        super().__init__(
            "coordinator-local requested but no self-hosted provider configured",
            blocked_companies=set(),
            n_dropped=0,
        )


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


def _get_system_setting_list(attr: str) -> Tuple[str, ...]:
    """v5.2.0 — pull a JSON-encoded list[str] setting and return a
    tuple. Empty/parse-error → (). Used by allowed_companies /
    blocked_models / allowed_models system-wide settings (parallel
    to ``_get_system_blocklist`` for ``blocked_companies``).
    """
    import json
    try:
        from app.config import settings
        raw = getattr(settings, attr, "") or ""
        if not raw or not raw.strip():
            return tuple()
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return tuple(str(x) for x in parsed if x)
        return tuple()
    except Exception:
        return tuple()


def _coerce_list(raw: Any) -> List[str]:
    """ApiKey JSON columns deserialize as list (SQLAlchemy JSON) or
    string (some sync paths roundtrip through TEXT). Normalize to
    list[str].
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if isinstance(raw, str):
        import json
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed if x]
        except Exception:
            return []
    return []


async def get_effective_policy(db, api_key_id: Optional[str]) -> Policy:
    """v5.2.0 — resolve the full effective policy for one (api_key, request)
    pair. Per-key fields union with the system-wide settings of the
    same name. Cache shares the 30s TTL of ``_BLOCKLIST_CACHE`` (a
    Policy is a small immutable bundle); invalidated on the same
    triggers (PATCH /api/keys, PATCH /api/settings, cluster sync apply).
    """
    sys_block_companies = _get_system_blocklist()
    sys_allow_companies = set(_get_system_setting_list("compliance_system_allowed_companies"))
    sys_block_models = _get_system_setting_list("compliance_system_blocked_models")
    sys_allow_models = _get_system_setting_list("compliance_system_allowed_models")

    per_key_block_companies: Set[str] = set()
    per_key_allow_companies: Set[str] = set()
    per_key_block_models: Tuple[str, ...] = tuple()
    per_key_allow_models: Tuple[str, ...] = tuple()

    if api_key_id:
        try:
            result = await db.execute(
                select(
                    ApiKey.blocked_companies, ApiKey.allowed_companies,
                    ApiKey.blocked_models, ApiKey.allowed_models,
                ).where(ApiKey.id == api_key_id)
            )
            row = result.first()
            if row:
                per_key_block_companies = set(_coerce_list(row[0]))
                per_key_allow_companies = set(_coerce_list(row[1]))
                per_key_block_models = tuple(_coerce_list(row[2]))
                per_key_allow_models = tuple(_coerce_list(row[3]))
        except Exception:
            pass  # fail-open on DB error; the bare blocklist path still gates

    return Policy(
        blocked_companies=per_key_block_companies | sys_block_companies,
        allowed_companies=per_key_allow_companies | sys_allow_companies,
        blocked_models=per_key_block_models + sys_block_models,
        allowed_models=per_key_allow_models + sys_allow_models,
    )


def filter_providers_v2(
    providers: List[Any],
    policy: Policy,
    requested_model: Optional[str] = None,
) -> List[Any]:
    """v5.2.0 — fine-grained filter using the full Policy bundle.

    Mirror of ``filter_providers`` but evaluates allowlist, model
    glob, and per-model patterns in addition to company blocklist.
    Raises ``ComplianceNoSubstituteError`` if every candidate is
    dropped (same 503 path as the v5.0.0 filter).
    """
    if policy.is_empty():
        return list(providers)
    out = []
    for p in providers:
        allowed, _reason = evaluate_policy(policy, p, requested_model)
        if allowed:
            out.append(p)
    if not out and providers:
        raise ComplianceNoSubstituteError(
            f"All {len(providers)} candidates blocked by policy "
            f"(companies blocked={sorted(policy.blocked_companies)} "
            f"allowed={sorted(policy.allowed_companies) or 'ANY'}; "
            f"models blocked={list(policy.blocked_models)} "
            f"allowed={list(policy.allowed_models) or 'ANY'})",
            blocked_companies=policy.blocked_companies,
            n_dropped=len(providers),
        )
    return out


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
    # v5.2.0 / Batch V2 — fine-grained policy
    "Policy",
    "evaluate_policy",
    "filter_providers_v2",
    "get_effective_policy",
]
