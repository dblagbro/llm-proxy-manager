"""Disclosure surfaces — HTTP headers + SSE prelude (v5.0.0).

Two disclosure paths:

- **HTTP headers** — always emitted on substitution responses (200 OK +
  ``X-Compliance-*`` fields, decision 8 + 23). The default and
  protocol-clean path.
- **SSE prelude** — opt-in via ``Accept-Compliance-Events: true`` (decision
  28, revised 2026-06-03 from "default-on opt-out" to "default-off
  opt-in"). When opted in, an Anthropic-shape ``event: compliance_substitution``
  block is emitted BEFORE ``message_start``; for OpenAI-shape streams the
  disclosure is injected as a top-level ``compliance_substitution`` key on
  the first ``data:`` frame (decision 15).
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from app.compliance.company_map import KNOWN_COMPANIES


def _company_display_name(company_id: str) -> str:
    info = KNOWN_COMPANIES.get(company_id, {})
    return info.get("display_name", company_id.title())


def compliance_headers(
    *,
    blocked_company: str,
    requested_model: str,
    served_model: str,
    served_company: Optional[str],
    served_provider_id: str,
    audit_id: str,
) -> Dict[str, str]:
    """The seven X-Compliance-* response headers for a substituted 200 OK
    (decision 8). Caller merges into ``resp_headers`` after route
    resolution returns ``compliance_substituted=True``.
    """
    served_display = (
        _company_display_name(served_company) if served_company else "alternate provider"
    )
    return {
        "X-Compliance-Substitution": "true",
        "X-Compliance-Substitution-Code": f"api-key-policy:blocked-company:{blocked_company}",
        "X-Compliance-Requested-Model": requested_model or "",
        "X-Compliance-Served-Model": served_model or "",
        "X-Compliance-Served-Provider": served_provider_id or "",
        "X-Compliance-Note": f"Answered using {served_model} by {served_display}",
        "X-Compliance-Audit-Id": audit_id,
    }


def refusal_headers_ua(
    *,
    matched_product: str,
    matched_company: str,
    audit_id: str,
) -> Dict[str, str]:
    """Headers for the HTTP 451 client-product-refusal response."""
    return {
        "X-Compliance-Refusal": "true",
        "X-Compliance-Refusal-Reason": "client-product-banned",
        "X-Compliance-Matched-Product": matched_product or "",
        "X-Compliance-Matched-Company": matched_company or "",
        "X-Compliance-Audit-Id": audit_id,
    }


def refusal_headers_no_substitute(*, audit_id: str) -> Dict[str, str]:
    """Headers for the HTTP 503 no-substitute response."""
    return {
        "X-Compliance-Refusal": "true",
        "X-Compliance-Refusal-Reason": "no-compliant-provider-available",
        "X-Compliance-Audit-Id": audit_id,
    }


def refusal_headers_no_local(*, audit_id: str) -> Dict[str, str]:
    """v5.0.4 — headers for the HTTP 503 coordinator-local-without-self-
    hosted-provider response. CADC §6.2 specifies
    ``no-compliant-local-provider``; v5.0.0–v5.0.3 omitted the header
    entirely (hub-team-flagged F anomaly), defaulting to a bare 503.
    """
    return {
        "X-Compliance-Refusal": "true",
        "X-Compliance-Refusal-Reason": "no-compliant-local-provider",
        "X-Compliance-Audit-Id": audit_id,
    }


def refusal_headers_path(*, audit_id: str) -> Dict[str, str]:
    """Headers for the HTTP 403 path-not-allowed response from the
    allowed_paths middleware."""
    return {
        "X-Compliance-Reason": "path-not-in-allowed_paths",
        "X-Compliance-Audit-Id": audit_id,
    }


def wants_sse_prelude(request_headers: Any) -> bool:
    """Decision 28 (revised 2026-06-03) — SSE prelude is OPT-IN.

    Default behavior is headers-only (protocol-clean SSE body). Caller
    sends ``Accept-Compliance-Events: true`` to opt in to the prelude.
    Accepts either a Starlette Headers object or a plain dict.
    """
    if request_headers is None:
        return False
    try:
        val = request_headers.get("accept-compliance-events", "")
    except AttributeError:
        return False
    if not val:
        return False
    return str(val).strip().lower() == "true"


def build_disclosure_payload(
    *,
    blocked_company: str,
    requested_model: str,
    served_model: str,
    served_company: Optional[str],
    served_provider_id: str,
    audit_id: str,
) -> Dict[str, Any]:
    """Compact disclosure dict shared between the HTTP-header path and
    the SSE prelude path. Both surfaces use the same field names so
    callers writing dual-surface clients can deserialize once.
    """
    served_display = (
        _company_display_name(served_company) if served_company else "alternate provider"
    )
    return {
        "substituted": True,
        "blocked_company": blocked_company,
        "requested_model": requested_model,
        "served_model": served_model,
        "served_company": served_company,
        "served_provider_id": served_provider_id,
        "note": f"Answered using {served_model} by {served_display}",
        "audit_id": audit_id,
    }


def sse_prelude_anthropic(disclosure: Dict[str, Any]) -> bytes:
    """Anthropic-shape SSE prelude. Emitted BEFORE ``event: message_start``
    (decision 15). Caller yields the returned bytes verbatim into the
    response stream.
    """
    body = json.dumps(disclosure, separators=(",", ":"))
    return f"event: compliance_substitution\ndata: {body}\n\n".encode("utf-8")


def sse_prelude_openai_inject(
    first_frame: Dict[str, Any],
    disclosure: Dict[str, Any],
) -> Dict[str, Any]:
    """OpenAI-shape — inject the disclosure as a top-level
    ``compliance_substitution`` key on the first ``data:`` frame
    (decision 15). Mutates and returns the dict; caller serializes.
    """
    first_frame["compliance_substitution"] = disclosure
    return first_frame


__all__ = [
    "compliance_headers",
    "refusal_headers_ua",
    "refusal_headers_no_substitute",
    "refusal_headers_path",
    "wants_sse_prelude",
    "build_disclosure_payload",
    "sse_prelude_anthropic",
    "sse_prelude_openai_inject",
]
