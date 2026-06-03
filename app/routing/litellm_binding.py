"""litellm wire-binding helpers — extracted from ``router.py`` 2026-06-02.

This module owns the "given a Provider row, how do we call litellm?"
question. It's a sibling of ``router.py``, which owns "given a request,
which Provider do we pick?".

The split exists because the binding layer (these helpers) has grown
to ~7 distinct concerns:

- ``PROVIDER_TYPE_TO_LITELLM`` — litellm prefix per provider_type
- ``PROVIDER_DEFAULT_MODELS`` — fallback default model per provider_type
- ``build_litellm_model`` — combines prefix + (override / row default / fallback)
- ``build_litellm_kwargs`` — api_key, api_base allowlist, timeout
- ``resolve_chat_model_for_provider`` — embedding-default → chat fallback
- ``_is_embedding_model`` — guard against routing embed-* to chat surfaces
- ``_model_family_provider_types`` — model-name → eligible provider-type set

Each new subscription-provider type (claude-oauth, codex-oauth,
grok-web, cursor-oauth) adds ~3 lines here. Keeping them in their own
file means a future fifth subscription provider doesn't touch the
select_provider / scoring strategy code in router.py.

All public names are re-exported from ``app.routing.router`` so existing
callers (every `from app.routing.router import …` site in the tree)
keep working without changes — this is a behavior-preserving move."""
from __future__ import annotations

import re
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.db import Provider


# ── helpers ─────────────────────────────────────────────────────────────────


_O_SERIES = re.compile(r"^o[0-9]")


def _is_embedding_model(model: str) -> bool:
    """v3.0.27: detect embedding-only model names. Used to keep embedding
    models off chat dispatch paths — Cohere's chat API explicitly rejects
    `embed-*` slugs with HTTP 400, OpenAI's chat API does the same with
    `text-embedding-*`. Caller's job to route those to /v1/embeddings."""
    if not model:
        return False
    m = model.lower()
    return (m.startswith("embed-") or m.startswith("text-embedding-")
            or m.startswith("embedding-"))


async def resolve_chat_model_for_provider(
    db: AsyncSession, provider: Provider
) -> tuple[Optional[str], Optional[str]]:
    """v3.0.32: shared helper for "what chat-capable model should we dispatch
    to this provider with?".

    Returns ``(chat_model, skip_reason)``:
      - ``(model, None)`` when a chat-capable model is found. Either the
        provider's ``default_model`` is already a chat slug, or one was
        picked from scanned ``ModelCapability`` rows (preferring
        ``command-*`` for cohere or ``gpt-*`` for OpenAI-shape).
      - ``(None, reason)`` when the provider has only embedding-only
        scanned models. Caller should skip the chat-style call.

    Replaces three duplicate copies that landed across v3.0.27/30/31:
      - ``scanner.test_provider`` (UI Test button)
      - ``keepalive._probe_one`` (synthetic 5-min probe)
      - the entry-time guard in ``completions.py`` + ``messages.py``
        (this one stays — it rejects embedding-named caller requests
        with a 400 before they reach select_provider; doesn't need the
        cap-fallback logic).

    The bug class this prevents: ``provider.default_model`` may be an
    embedding-only slug (e.g. Cohere's ``embed-english-v3.0`` is the
    recommended general-purpose default). Dispatching that to a chat
    surface upstream returns 400 and trips the breaker. v3.0.27, 30, 31
    each fixed one of the call sites in isolation; this helper makes
    the next call site impossible to get wrong.
    """
    default = provider.default_model or ""
    if not _is_embedding_model(default):
        return (default or None, None)

    from app.models.db import ModelCapability
    caps = (await db.execute(
        select(ModelCapability.model_id).where(
            ModelCapability.provider_id == provider.id
        )
    )).scalars().all()
    chat_candidates = [c for c in caps if not _is_embedding_model(c)]
    if not chat_candidates:
        return (None, f"provider {provider.id!r} has no chat-capable scanned models")
    # Prefer `command-*` (Cohere chat) or `gpt-*` over the alphabetical
    # default — those are the most reliable chat surfaces for a probe.
    preferred = [c for c in chat_candidates
                 if c.startswith("command-") or c.startswith("gpt-")]
    return ((preferred or sorted(chat_candidates))[0], None)


def _model_family_provider_types(model: str) -> Optional[set[str]]:
    """v3.0.26: map a requested model name to the set of provider types that
    can physically serve it. Returns None when no family is detected (caller
    falls through to the existing capability/scoring path).

    The mapping is intentionally narrow — only well-known prefixes that
    correspond to known SDK shapes. Unknown prefixes (custom finetunes, new
    families) return None and skip the family filter entirely so we don't
    over-restrict legitimate routes.

    DevinGPT report 2026-05-01: claude-sonnet-4-6 was being routed to
    codex-oauth via a v3.0.22 fall-through. This filter is the hard backstop.

    v4.4.40 BUG-086 fix: ``cursor-oauth`` was missing from both the Claude
    AND the OpenAI families since its v4.4.31 introduction. Cursor's relay
    serves claude-* (claude-4-sonnet, claude-4.5-haiku, claude-opus-4-8-*,
    etc.) and gpt-* (gpt-4o, gpt-5, gpt-5-codex, …) — the operator filed
    a high-priority bug on 2026-06-03 noting that claude-haiku requests
    were skipping their priority-4 Cursor provider in favor of higher-
    priority-number Anthropic-OAuth providers, because the family filter
    eliminated Cursor before priority ordering even applied.
    """
    if not model:
        return None
    m = model.lower()
    # Anthropic family — claude-* + their variants. Vertex's claude-on-bedrock
    # would also live here but we don't currently support that wire format.
    # cursor-oauth's relay serves the full Claude catalog (including the
    # tiered -low/-medium/-high/-max and -thinking variants).
    if m.startswith("claude-") or m.startswith("claude/"):
        return {"anthropic", "anthropic-direct", "anthropic-oauth", "claude-oauth", "cursor-oauth"}
    # OpenAI family — gpt-*, o1/o3/o4 reasoning series, text-embedding-*,
    # whisper-*, dall-e-*, codex-*. codex-oauth speaks the same wire format
    # but only for its 6 Plus-tier slugs (handled by the v3.0.22 cap filter
    # which runs after this). cursor-oauth's relay also serves the gpt-*
    # family (gpt-4o, gpt-5, gpt-5-codex, …); the same downstream cap
    # filter eliminates models the operator's Cursor account can't reach.
    if (m.startswith("gpt-") or m.startswith("o1-") or m.startswith("o3-")
            or m.startswith("o4-") or m.startswith("text-embedding-")
            or m.startswith("whisper-") or m.startswith("dall-e-")
            or m.startswith("codex-")):
        return {"openai", "ChatGPT-oauth-plan", "cursor-oauth"}
    # Google / Gemini family.
    if m.startswith("gemini-") or m.startswith("text-bison") or m.startswith("chat-bison"):
        return {"google", "vertex", "vertex_ai"}
    # Cohere family — their embed-* + command-* slugs.
    if m.startswith("embed-") or m.startswith("command-"):
        return {"cohere"}
    # Grok / xAI family — covers paid xAI API (``grok``), web-subscription
    # surface (``grok-web``), and OpenRouter's xAI passthrough.
    if m.startswith("grok-") or m.startswith("x-ai/"):
        return {"grok", "grok-web", "openrouter"}
    # Unknown family — don't constrain.
    return None


# ── provider_type → litellm prefix + default model tables ───────────────────


PROVIDER_TYPE_TO_LITELLM = {
    "anthropic": "anthropic",
    "openai": "openai",
    "google": "gemini",
    "vertex": "vertex_ai",
    "ollama": "ollama",
    "grok": "xai",
    "compatible": "openai",     # OpenAI-compatible uses openai provider with custom base_url
    # v2.7.0: claude-oauth never routes through litellm — messages.py
    # dispatches a direct httpx call to platform.claude.com. The "anthropic"
    # prefix here is only used for the `X-Resolved-Model` response header.
    "claude-oauth": "anthropic",
    # v3.0.15: codex-oauth never routes through litellm either —
    # _codex_oauth_dispatch handles it via direct httpx to chatgpt.com.
    # The "openai" prefix is only used for the X-Resolved-Model header.
    "ChatGPT-oauth-plan": "openai",
    # v3.0.23 (Q2): Cohere via litellm (cohere/embed-english-v3.0 etc).
    "cohere": "cohere",
    # v3.0.66: Microsoft Azure OpenAI Service. Caller's base_url should
    # carry the Azure resource endpoint (e.g.
    # https://my-resource.openai.azure.com); api_key carries the key.
    # litellm uses the ``azure/`` prefix and reads the deployment name
    # from the model field (``azure/<deployment-name>``).
    "azure": "azure",
    # v3.1.3: OpenRouter — multi-vendor LLM marketplace. litellm has
    # native support via the ``openrouter/`` prefix. Models are namespaced
    # vendor/model (e.g. ``openrouter/anthropic/claude-sonnet-4-6``,
    # ``openrouter/openai/gpt-4o``, ``openrouter/google/gemini-2.5-flash``).
    # base_url is fixed at https://openrouter.ai/api/v1 (litellm handles
    # internally — operator doesn't need to set it on the Provider row).
    "openrouter": "openrouter",
    # v3.2.0: grok-web — replays grok.com browser session against the
    # operator's web subscription (no xAI API key needed). Like
    # claude-oauth and codex-oauth, never goes through litellm; messages.py
    # / completions.py dispatch directly via app.providers.grok_web.
    # The "xai" prefix here is only used by the X-Resolved-Model header.
    "grok-web": "xai",
    # v4.4.31: cursor-oauth dispatches through the Cursor-To-OpenAI
    # sidecar, which speaks the OpenAI Chat Completions wire format.
    # litellm therefore uses the openai/ prefix and the sidecar's
    # base_url (auto-pinned by _do_exchange_create / _do_poll_create).
    # build_litellm_kwargs adds it to the api_base allowlist below so
    # litellm actually honors that base_url instead of falling through
    # to api.openai.com (which rejected the user_<id>::<JWT> token with
    # "Incorrect API key provided" — the v4.4.31..v4.4.34 Test-failure
    # mystery resolved by v4.4.35).
    "cursor-oauth": "openai",
}


PROVIDER_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "google":    "gemini-2.0-flash",
    "vertex":    "gemini-2.0-flash-002",
    "openai":    "gpt-4o",
    "grok":      "grok-2",
    "ollama":    "llama3",
    "compatible": "gpt-4o",
    # v3.0.66: Azure deployments are caller-named — admin sets the
    # deployment name as default_model on the provider row.
    "azure":     "gpt-4o",
    # Claude Pro Max subscription — caller chooses model at request time.
    "claude-oauth": "claude-sonnet-4-6",
    # ChatGPT Plus/Team/Enterprise subscription via Codex CLI.
    # gpt-5.5 is the Plus default; Pro/Team see different slugs.
    "ChatGPT-oauth-plan": "gpt-5.5",
    # v3.0.23 (Q2): Cohere — primarily an embeddings provider but also
    # has rerank/chat surfaces. embed-english-v3.0 is the recommended
    # general-purpose default.
    "cohere": "embed-english-v3.0",
    # v3.1.3: OpenRouter — vendor-prefixed model names. ``openai/gpt-4o``
    # is OpenRouter's default and a sane fallback when cross-family
    # substitution lands here.
    "openrouter": "openai/gpt-4o",
    # v3.2.0: grok-web — operator's web subscription. Lite plan default is
    # grok-3 (modeId=fast); Premium can override default_model to grok-4.
    "grok-web": "grok-3",
    # v4.4.37: cursor-oauth — Cursor renamed catalog (no claude-3-7-sonnet)
    # so the default is the live model name. v4.4.34 fixed the same default
    # in CURSOR_OAUTH_SPEC + ProviderForm OAUTH_FLAVORS; this dict was
    # missed and the test_all_known_types_have_default invariant caught it
    # in v4.4.37. Used as the build_litellm_model fallback if a provider
    # row has no default_model set.
    "cursor-oauth": "claude-4-sonnet",
}


# ── builders ────────────────────────────────────────────────────────────────


def build_litellm_model(provider: Provider, model_override: Optional[str] = None) -> str:
    prefix = PROVIDER_TYPE_TO_LITELLM.get(provider.provider_type, "openai")
    default = PROVIDER_DEFAULT_MODELS.get(provider.provider_type, "gpt-4o")
    model = model_override or provider.default_model or default
    return f"{prefix}/{model}"


def build_litellm_kwargs(provider: Provider) -> dict:
    kwargs: dict[str, Any] = {}
    if provider.api_key:
        kwargs["api_key"] = provider.api_key
    # v4.4.35: cursor-oauth needs api_base too, or litellm routes the
    # request to api.openai.com (defaulting from the ``openai/`` prefix)
    # and the user_<id>::<JWT> token gets rejected as "Incorrect API
    # key provided" — the exact symptom that confused v4.4.31..v4.4.34.
    if provider.base_url and provider.provider_type in ("ollama", "compatible", "cursor-oauth"):
        kwargs["api_base"] = provider.base_url
    kwargs["timeout"] = provider.timeout_sec
    return kwargs


def _native_thinking_params(provider_type: str, model_id: str) -> dict:
    """Return provider-specific reasoning kwargs to inject when native_reasoning=True.

    Lives here (not in router.py) because the decision is keyed off
    provider_type + model name — the same axis as PROVIDER_TYPE_TO_LITELLM.
    Settings (native_thinking_budget_tokens, native_reasoning_effort) come
    from ``app.config.settings``."""
    m = model_id.lower()
    if provider_type in ("google", "vertex") and "2.5" in m:
        return {"thinking": {"type": "enabled", "budget_tokens": settings.native_thinking_budget_tokens}}
    if provider_type == "openai" and _O_SERIES.match(m):
        return {"reasoning_effort": settings.native_reasoning_effort}
    return {}
