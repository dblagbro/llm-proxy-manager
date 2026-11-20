"""Compliance enforcement subsystem (v5.0.0).

See ``docs/5.0-compliance-design.md`` for the full spec.

Public surface (re-exported here for ``from app.compliance import ...``):

- Company map: ``KNOWN_COMPANIES``, ``provider_type_to_company``,
  ``model_family_to_company``
- Policy: ``get_effective_blocklist``, ``filter_providers``,
  ``is_company_banned``, ``invalidate_blocklist_cache``,
  ``ComplianceNoSubstituteError``, ``ComplianceUaBlockedError``
- UA detect: ``detect_client_company``
- Disclosure: ``compliance_headers``, ``refusal_headers_ua``,
  ``refusal_headers_no_substitute``, ``refusal_headers_path``,
  ``wants_sse_prelude``, ``build_disclosure_payload``,
  ``sse_prelude_anthropic``, ``sse_prelude_openai_inject``
- Audit: ``generate_audit_id``, ``generate_policy_change_id``,
  ``emit_event``, ``emit_policy_change``,
  ``compute_daily_integrity_hash``, ``purge_expired_events``
"""

from app.compliance.audit import (
    compute_daily_integrity_hash,
    emit_event,
    emit_policy_change,
    generate_audit_id,
    generate_policy_change_id,
    purge_expired_events,
)
from app.compliance.company_map import (
    KNOWN_COMPANIES,
    model_family_to_company,
    provider_type_to_company,
)
from app.compliance.disclosure import (
    build_disclosure_payload,
    compliance_headers,
    refusal_headers_no_local,
    refusal_headers_no_substitute,
    refusal_headers_path,
    refusal_headers_ua,
    sse_prelude_anthropic,
    sse_prelude_openai_inject,
    wants_sse_prelude,
)
from app.compliance.policy import (
    ComplianceNoLocalProviderError,
    ComplianceNoSubstituteError,
    ComplianceUaBlockedError,
    # v5.2.0 / Batch V2 — fine-grained policy
    Policy,
    evaluate_policy,
    filter_providers,
    filter_providers_v2,
    get_custom_companies,
    get_effective_blocklist,
    get_effective_policy,
    invalidate_blocklist_cache,
    is_company_banned,
)
from app.compliance.ua_detect import detect_client_company

__all__ = [
    # Company map
    "KNOWN_COMPANIES",
    "provider_type_to_company",
    "model_family_to_company",
    # Policy
    "ComplianceNoSubstituteError",
    "ComplianceNoLocalProviderError",
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
    # UA detection
    "detect_client_company",
    # Disclosure
    "compliance_headers",
    "refusal_headers_ua",
    "refusal_headers_no_substitute",
    "refusal_headers_no_local",
    "refusal_headers_path",
    "wants_sse_prelude",
    "build_disclosure_payload",
    "sse_prelude_anthropic",
    "sse_prelude_openai_inject",
    # Audit
    "generate_audit_id",
    "generate_policy_change_id",
    "emit_event",
    "emit_policy_change",
    "compute_daily_integrity_hash",
    "purge_expired_events",
]
