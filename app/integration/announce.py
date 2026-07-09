"""v5.8.0 — public ``/announce`` payload assembly.

The announce document describes the proxy's external surface (endpoints,
wire formats, routing semantics, MCP catalog, integration protocol)
WITHOUT revealing any operational secret (no provider list with
credentials, no per-key policy details, no audit chain state). The
goal is to give an integrating AI enough context to negotiate a key
on its first ``/api/integration/chat`` turn.
"""
from __future__ import annotations

from typing import Any, Dict


async def build_announce_payload() -> Dict[str, Any]:
    """Assemble the announce payload. Safe to call from a public
    endpoint — no secrets returned."""
    from app.__version__ import __version__
    from app.config import settings

    # Pull the live MCP tool list if MCP is mounted; degrade silently
    # if not (the integrate-side AI just sees an empty list).
    mcp_tools: list[dict] = []
    try:
        from app.mcp_server.server import current_mcp_policy  # noqa: F401
        from app.proxy_tools import get_registry_async
        registry = await get_registry_async()
        for t in registry:
            mcp_tools.append({
                "name": t.name,
                "description": (
                    t.anthropic_schema.get("description", "")[:200]
                    if isinstance(t.anthropic_schema, dict) else ""
                ),
            })
    except Exception:
        pass

    return {
        "name": "llm-proxy v2",
        "version": __version__,
        "description": (
            "Multi-provider LLM gateway. Accepts Anthropic, OpenAI, and "
            "OpenAI Responses wire formats; routes to the best available "
            "upstream via litellm with cross-family failover, circuit "
            "breakers, capability-aware routing, and per-key MCP policy. "
            "Audit-grade compliance enforcement on every request."
        ),
        "endpoints": {
            "messages": {
                "path": "/v1/messages",
                "wire_format": "Anthropic Messages",
                "supports_streaming": True,
                "supports_tools": True,
                "supports_images": True,
            },
            "chat_completions": {
                "path": "/v1/chat/completions",
                "wire_format": "OpenAI Chat Completions",
                "supports_streaming": True,
                "supports_tools": True,
                "supports_images": True,
            },
            "responses": {
                "path": "/v1/responses",
                "wire_format": "OpenAI Responses",
                "supports_streaming": True,
                "supports_tools": True,
            },
            "embeddings": {
                "path": "/v1/embeddings",
                "wire_format": "OpenAI Embeddings",
            },
            "mcp_aggregator": {
                "path": "/mcp/",
                "wire_format": "MCP (streamable_http)",
                "auth": "Bearer token (any valid API key for this proxy)",
                "description": (
                    "Aggregate all proxy-side tools (Excel reader, "
                    "URL fetcher, document-to-markdown, etc.) for any "
                    "MCP-compatible client (Claude Code, Cursor, "
                    "Continue, Cline)."
                ),
            },
        },
        "routing_features": [
            {
                "name": "model_aliases",
                "description": (
                    "Pass ``model: 'auto'`` (or ``'llmp-auto'``) to let "
                    "the LMRH ranking pick provider + model based on "
                    "request context."
                ),
            },
            {
                "name": "routing_hints",
                "description": (
                    "Header `llm-hint: speed|depth|cheap|exacto` "
                    "biases routing. Suffix on model "
                    "`:floor|:nitro|:exacto` does the same per-call."
                ),
            },
            {
                "name": "cross_family_fallback",
                "description": (
                    "If the requested provider family is unavailable, "
                    "the router substitutes the same capability tier "
                    "from a different family (e.g. Anthropic → Gemini)."
                ),
            },
            {
                "name": "circuit_breakers_per_provider",
                "description": (
                    "Automatic provider-pool exclusion on consecutive "
                    "failures; hold-down + adaptive half-open probes."
                ),
            },
            {
                "name": "empty_success_failover",
                "description": (
                    "Detects streaming responses that complete with "
                    "zero content (Gemini hiccup, Anthropic 5xx silent "
                    "drop) and fails over to a different provider on "
                    "the same call."
                ),
            },
            {
                "name": "per_key_mcp_policy",
                "description": (
                    "Each API key can restrict (allow-list) or expand "
                    "(deny-list) which MCP tools the model can call. "
                    "Token-budget cap also per-key."
                ),
            },
        ],
        "mcp_tools_available": mcp_tools,
        "auth_header_options": [
            "x-api-key: <key> (Anthropic style)",
            "Authorization: Bearer <key> (OpenAI style)",
        ],
        "integration": {
            "endpoint": "/api/integration/chat",
            "method": "POST",
            "auth_mechanism": "shared passphrase in request body",
            "enabled": bool(settings.integration_enabled),
            "purpose": (
                "Negotiate API key configuration via AI-to-AI chat. "
                "The integrating AI describes its project + use case; "
                "the management AI asks clarifying questions if needed, "
                "then mints an API key with the appropriate scope, "
                "MCP policy, and daily budget."
            ),
            # v5.20.2 — self-update surface. Docs the caller's AI can
            # read to know it can adjust its own key AFTER negotiation
            # without going through the operator.
            "self_update": {
                "endpoint": "/api/integration/self-update",
                "method": "POST",
                "auth_mechanism": "the API key itself (x-api-key or Bearer header)",
                "purpose": (
                    "After initial negotiation, the caller's AI can "
                    "update its own settings — bounded by the "
                    "self_edit_permissions granted on the key at mint "
                    "time. Fields not in that permission list return "
                    "as 'denied' in the response, not an HTTP error, "
                    "so the caller can propose changes and see which "
                    "the operator pre-authorized."
                ),
                "eligible_fields": [
                    "mcp_tools_allow", "mcp_tools_deny",
                    "mcp_schema_token_budget",
                    "system_prompt_mcp_augmentation",
                    "refusal_detection_enabled",
                    "refusal_prompt_hardening",
                    "refusal_retry_enabled",
                    "refusal_retry_max_attempts",
                    "semantic_cache_enabled",
                ],
                "never_editable_fields": [
                    "key_type", "enabled", "spending_cap_usd",
                    "daily_hard_cap_usd", "blocked_companies",
                    "self_edit_permissions",
                ],
                "protocol_proposal_channel": (
                    "The self-update payload includes an optional "
                    "``protocol_proposal`` free-form text field. When "
                    "set, the proposal is queued as an activity_log "
                    "event (event_type=integration.protocol_proposal) "
                    "for the operator to review — a structured way to "
                    "ask for new features or protocols without going "
                    "through email/memo channels."
                ),
            },
            # v5.20.2 — Refusal detection surface (self-editable per
            # v5.20.0). Documented in announce so the caller's AI
            # knows the flags exist and can request them via
            # self-update if permission is granted.
            "refusal_detection": {
                "enabled_per_key": True,
                "detection_module": "app/refusal_detection.py",
                "detection_response_header": "X-Refusal-Detected",
                "category_header": "X-Refusal-Category",
                "categories": [
                    "task_substitution",
                    "capability_deny",
                    "explicit_refused",
                ],
                "prompt_hardening_flag": "refusal_prompt_hardening",
                "cascade_flag": "refusal_retry_enabled",
                "cascade_headers": [
                    "X-Refusal-Retry-Attempted",
                    "X-Refusal-Retry-Provider",
                    "X-Refusal-Chain-Exhausted",
                ],
                "notes": (
                    "The caller can enable detection alone (header + "
                    "activity_log), detection + prompt hardening "
                    "(makes refusals machine-detectable as "
                    "REFUSED: <reason>), or full proxy-side cascade "
                    "(non-streaming only in v5.20.1)."
                ),
            },
            "request_shape": {
                "passphrase": "<shared secret>",
                "conversation_id": "<optional; nullable on first turn>",
                "project_name": "<name of integrating project>",
                "message": "<your description / response>",
            },
            "response_shape": {
                "conversation_id": "<echoed; use it on subsequent turns>",
                "response": "<management AI's reply>",
                "provisioned": (
                    "<null OR {api_key, name, daily_budget_usd, "
                    "mcp_tools_allow, notes, usage_instructions}>"
                ),
            },
            "first_turn_guidance": (
                "Front-load your first message with: (1) project name + "
                "purpose, (2) AI use case (chat, agent, embedding, "
                "tool-use), (3) capability needs (image input, "
                "long-context, code, document tools), (4) cost "
                "sensitivity. The management AI aims to mint your key "
                "within 1-3 turns."
            ),
            "limits": {
                "default_daily_budget_usd": settings.integration_default_daily_budget_usd,
                "max_daily_budget_usd": settings.integration_max_daily_budget_usd,
                "max_messages_per_session": settings.integration_max_messages_per_session,
            },
        },
    }
