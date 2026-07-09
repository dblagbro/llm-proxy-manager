"""v5.14.0 — Built-in ``X-Compliance-Substitution`` header hook.

Migrated from the inline emission code at
``app/api/_compliance_handler.py:_disposition_only_headers`` (v5.9.3)
and the substitution-active emission at
``app/compliance/disclosure.py``. Behavior is byte-identical to the
pre-v5.14.0 emission so the v5.9.3 contract held by hub team's
``_scan_anthropic_response_model`` doesn't move.

Header value contract:

- ``true``           — proxy substituted the requested model with a
                       served one (compliance policy enforcement).
- ``false``          — policy was evaluated AND the served model
                       passed (no substitution needed).
- ``pass-through``   — no per-key compliance policy applies, and no
                       cluster-default gate is active. The caller
                       bypassed the substitution path by design.

Always emitted on 2xx relay responses. Hub-side scanners treat
absence as a hard error (per 2026-06-22 contract reply memo).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.api._response_hook_runner import HookContext

logger = logging.getLogger(__name__)


def _key_has_compliance_policy(key_record: Any) -> bool:
    """Mirror of the v5.9.3 helper that lived in _compliance_handler.py.

    Returns True iff the API key has ANY MCP / banned-vendor / mode
    policy on it — anything that would trigger the substitution gate
    at routing time.
    """
    if key_record is None:
        return False
    for attr in (
        "mcp_tools_allow",
        "mcp_tools_deny",
        "banned_companies",
        "compliance_mode",
    ):
        v = getattr(key_record, attr, None)
        if v is None:
            continue
        if isinstance(v, list) and len(v) > 0:
            return True
        if isinstance(v, str) and v.strip() not in ("", "[]", "null"):
            return True
    return False


async def compliance_substitution_header_hook(
    *,
    handler_id: str,
    resp_headers: dict,
    context: HookContext,
) -> Optional[dict]:
    """Set ``X-Compliance-Substitution`` based on the routing outcome.

    Idempotent: if a caller (e.g. a future hub-side hook) has already
    set the header to ``true`` to mirror its own substitution
    decision, leave it alone. This hook owns the false / pass-through
    decision; the substitution-active case is set upstream by the
    routing layer's policy-rejection path before this hook fires.
    """
    if "X-Compliance-Substitution" in resp_headers:
        return None

    if context.substituted:
        return {"X-Compliance-Substitution": "true"}

    value = (
        "false" if _key_has_compliance_policy(context.key_record)
        else "pass-through"
    )
    return {"X-Compliance-Substitution": value}
