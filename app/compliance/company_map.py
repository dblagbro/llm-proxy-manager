"""Company taxonomy + lineage maps (v5.0.0).

The hardcoded 10-company taxonomy that ships with v5.0.0's compliance
enforcement. Each company defines:

- ``display_name`` — for headers + admin UI
- ``model_prefixes`` — for model-family lineage check (decision 11). A
  request for ``claude-haiku`` resolves to company ``anthropic`` regardless
  of which provider serves it (a Bedrock-hosted Anthropic model still
  triggers the Anthropic ban).
- ``provider_types`` — for ``Provider.owner_company`` auto-derivation
  (decision 17). A provider with ``provider_type='anthropic-oauth'`` gets
  ``owner_company='anthropic'`` automatically.
- ``ua_patterns`` — for client-product UA detection (decision 16). Each
  pattern has a ``type`` (prefix/contains/regex/exact) and ``value``.
  Matching is case-insensitive.

Pattern philosophy: narrow > broad. Each pattern is anchored so that
documentation strings, compatibility libraries, and historical names
don't false-positive. See ``docs/compliance-taxonomy-v5.0.0.md`` for the
operator-facing checklist + review guidance.

Operators can add custom companies via
``SystemSetting.compliance_custom_companies`` (JSON list); they merge with
``KNOWN_COMPANIES`` at lookup time (see ``policy.get_merged_companies``).
"""
from __future__ import annotations

from typing import Any, Dict, Optional


KNOWN_COMPANIES: Dict[str, Dict[str, Any]] = {
    "anthropic": {
        "display_name": "Anthropic",
        # Bedrock-style ``anthropic.claude-*`` is intentionally included here
        # so banning ``anthropic`` catches Bedrock-served Claude models.
        # The model-family resolver also tags such requests as ``aws``
        # (see ``aws.model_prefixes``), so banning either company drops the
        # provider. Decision 11.
        "model_prefixes": ["claude-", "claude/", "anthropic.claude-"],
        "provider_types": [
            "anthropic",
            "anthropic-direct",
            "anthropic-oauth",
            "claude-oauth",
        ],
        "ua_patterns": [
            {"type": "prefix", "value": "claude-cli/"},
            {"type": "contains", "value": "@anthropic-ai/claude-code"},
            {"type": "prefix", "value": "claude-code/"},
            {"type": "prefix", "value": "anthropic-sdk-python/"},
            {"type": "prefix", "value": "anthropic-sdk-typescript/"},
            {"type": "prefix", "value": "anthropic-sdk-go/"},
            {"type": "prefix", "value": "anthropic-sdk-java/"},
            {"type": "prefix", "value": "anthropic-sdk-ruby/"},
            {"type": "prefix", "value": "anthropic-sdk-rust/"},
            {"type": "regex", "value": r"^claude/[0-9]"},
            {"type": "regex", "value": r"(^|[ ;])@anthropic-ai/"},
        ],
    },
    "openai": {
        "display_name": "OpenAI",
        "model_prefixes": [
            "gpt-", "o1-", "o3-", "o4-", "codex-",
            "text-embedding-", "whisper-", "dall-e-",
        ],
        "provider_types": ["openai", "ChatGPT-oauth-plan"],
        "ua_patterns": [
            {"type": "prefix", "value": "openai-python/"},
            {"type": "prefix", "value": "openai-node/"},
            {"type": "prefix", "value": "openai/"},
            {"type": "prefix", "value": "chatgpt/"},
            {"type": "prefix", "value": "codex-cli/"},
            {"type": "regex", "value": r"^@openai/"},
        ],
    },
    "google": {
        "display_name": "Google",
        "model_prefixes": [
            "gemini-", "bison", "palm-",
            "text-bison", "chat-bison",
        ],
        "provider_types": ["google", "vertex"],
        "ua_patterns": [
            {"type": "prefix", "value": "google-genai/"},
            {"type": "prefix", "value": "google-cloud-aiplatform/"},
            {"type": "prefix", "value": "gemini-cli/"},
            {"type": "regex", "value": r"(^|[ ;])vertex-ai-"},
        ],
    },
    "xai": {
        "display_name": "xAI",
        "model_prefixes": ["grok-", "x-ai/"],
        "provider_types": ["grok", "grok-web"],
        "ua_patterns": [
            {"type": "prefix", "value": "xai-sdk-"},
            {"type": "prefix", "value": "grok-cli/"},
            {"type": "prefix", "value": "xai-grok/"},
        ],
    },
    "cohere": {
        "display_name": "Cohere",
        "model_prefixes": ["embed-", "command-", "rerank-"],
        "provider_types": ["cohere"],
        "ua_patterns": [
            {"type": "prefix", "value": "cohere-python/"},
            {"type": "prefix", "value": "cohere-go/"},
            {"type": "prefix", "value": "cohere-typescript/"},
        ],
    },
    "meta": {
        "display_name": "Meta",
        "model_prefixes": ["llama-", "llama2-", "llama3-", "code-llama-"],
        "provider_types": [],  # Meta has no direct hosted provider
        "ua_patterns": [
            {"type": "prefix", "value": "meta-llama/"},
            {"type": "regex", "value": r"^@meta-llama/"},
        ],
    },
    "mistral": {
        "display_name": "Mistral",
        "model_prefixes": [
            "mistral-", "mixtral-", "codestral-", "magistral-",
        ],
        "provider_types": [],  # Mistral via OpenRouter / self-hosted
        "ua_patterns": [
            {"type": "prefix", "value": "mistralai-"},
            {"type": "prefix", "value": "mistral-cli/"},
            {"type": "regex", "value": r"^@mistralai/"},
        ],
    },
    "aws": {
        "display_name": "AWS",
        "model_prefixes": [
            "anthropic.claude-", "ai21.j2-", "cohere.command-",
            "meta.llama-", "mistral.mistral-",
            "amazon.titan-", "stability.sd-",
        ],
        "provider_types": ["bedrock"],
        "ua_patterns": [
            {"type": "prefix", "value": "aws-sdk-"},
            {"type": "prefix", "value": "boto3/"},
            {"type": "prefix", "value": "aws-cli/"},
        ],
    },
    "microsoft": {
        "display_name": "Microsoft (Azure)",
        "model_prefixes": ["phi-", "orca-"],
        "provider_types": ["azure"],
        "ua_patterns": [
            {"type": "prefix", "value": "azure-openai/"},
            {"type": "prefix", "value": "azure-ai-"},
            {"type": "prefix", "value": "microsoft-azuresdk/"},
        ],
    },
    "amazon": {
        "display_name": "Amazon",
        "model_prefixes": ["amazon.titan-", "titan-", "nova-", "nova."],
        "provider_types": [],  # Overlaps with AWS
        "ua_patterns": [
            {"type": "prefix", "value": "amazon-bedrock-"},
        ],
    },
}


# Aggregator/relay provider types that don't map to one of the 10 KNOWN
# companies but should still get a stable ``owner_company`` label so
# operators can ban an aggregator wholesale if they need to.
_AGGREGATOR_COMPANIES: Dict[str, str] = {
    "openrouter": "openrouter",
    "cursor-oauth": "cursor",
    "openai-compatible": "compatible",
    "compatible": "compatible",
    "custom": "custom",
}


def provider_type_to_company(provider_type: Optional[str]) -> Optional[str]:
    """Decision 17 — derive ``Provider.owner_company`` from ``provider_type``.

    Called at create/update time in ``app/api/providers.py``. The result is
    persisted; subsequent compliance checks read the stored ``owner_company``
    rather than re-deriving (so operator overrides stick).

    Returns ``None`` if the type is unknown — the operator can fill in
    ``owner_company`` manually for one-off cases.
    """
    if not provider_type:
        return None
    for company_id, info in KNOWN_COMPANIES.items():
        if provider_type in info["provider_types"]:
            return company_id
    if provider_type in _AGGREGATOR_COMPANIES:
        return _AGGREGATOR_COMPANIES[provider_type]
    return None


def model_family_to_company(model: Optional[str]) -> Optional[str]:
    """Return the FIRST company whose ``model_prefix`` matches ``model``.

    For policy decisions that care about a single label (disclosure
    headers, audit row's ``blocked_company`` field), this is the right
    shape — pick the primary attribution.

    For the policy FILTER decision (does this model touch a banned
    company?), use ``model_family_companies(...)`` instead — Bedrock-
    served Anthropic models match BOTH ``anthropic`` and ``aws`` and the
    filter needs to honor either ban.
    """
    if not model:
        return None
    m = model.lower()
    for company_id, info in KNOWN_COMPANIES.items():
        for prefix in info["model_prefixes"]:
            if m.startswith(prefix.lower()):
                return company_id
    return None


def model_family_companies(model: Optional[str]) -> set:
    """Decision 11 — return EVERY company whose ``model_prefix`` matches.

    Bedrock-served Anthropic models like ``anthropic.claude-3-haiku-v1:0``
    are tagged as BOTH ``anthropic`` (via the ``anthropic.claude-`` prefix
    in Anthropic's list) AND ``aws`` (via the same prefix in AWS's list).
    The router pre-filter drops the provider if EITHER company is in the
    request's effective blocklist.
    """
    if not model:
        return set()
    m = model.lower()
    out = set()
    for company_id, info in KNOWN_COMPANIES.items():
        for prefix in info["model_prefixes"]:
            if m.startswith(prefix.lower()):
                out.add(company_id)
                break
    return out


__all__ = [
    "KNOWN_COMPANIES",
    "provider_type_to_company",
    "model_family_to_company",
    "model_family_companies",
]
