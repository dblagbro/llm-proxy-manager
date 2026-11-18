"""Model alias resolver — maps client model names to specific provider+model pairs.

v5.0.0 adds 4 hardcoded LOGICAL aliases (decision 29) layered ON TOP of
the existing DB-backed ``ModelAlias`` table. Logical aliases don't pin a
provider — instead they map to an LMRH hint string that the existing
hint resolver propagates, plus an optional ``self_hosted_only`` hard
filter that lives OUTSIDE the LMRH scorer (decision 29 correction
2026-06-03).
"""
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.db import ModelAlias


# v5.0.0 — hardcoded logical aliases. Each entry carries an LMRH hint
# string the caller layers into ``LLM-Hint``; the ``self_hosted_only``
# entry (coordinator-local only) enforces a hard self-hosted filter
# outside the LMRH scorer (decision 29, hub correction 2026-06-03 —
# "self-hosted/private regardless of runtime", no fallback to hosted).
LOGICAL_ALIASES: dict[str, dict[str, Any]] = {
    "coordinator-code": {
        "hint": "task=code, safety-min=3, cost=standard, exclude=anthropic;require",
    },
    "coordinator-fast": {
        "hint": "task=code, latency=low, cost=economy, exclude=anthropic;require",
    },
    "coordinator-reasoning": {
        "hint": "task=reasoning, safety-min=3, context-length=100000, exclude=anthropic;require",
    },
    "coordinator-local": {
        "hint": "task=code, exclude=anthropic;require",
        "self_hosted_only": True,
    },
}


def is_logical_alias(model: Optional[str]) -> bool:
    """True when ``model`` is one of the four CADC logical aliases.

    v5.3.8 — logical aliases are ROUTING DIRECTIVES, not model names.
    They must never reach the router's family/capability filters as a
    ``model_override`` (no provider has a capability row matching
    "coordinator-code", so the filter excludes every SCANNED provider
    and leaves only never-scanned ones — the exact misroute that sent
    the whole GCP alias workload to an unscanned cursor-oauth provider,
    which then 200-wrapped Cursor's ERROR_BAD_MODEL_NAME as an empty
    completion). They must also never be dispatched verbatim upstream.
    """
    return bool(model) and model in LOGICAL_ALIASES


def logical_alias_hint(alias: str) -> Optional[str]:
    """Return the LMRH hint string for a logical alias, or None."""
    entry = LOGICAL_ALIASES.get(alias)
    return entry["hint"] if entry else None


# Provider types that are inherently self-hosted. The
# ``is_self_hosted_provider`` predicate also accepts opt-in marker fields
# on the generic ``openai-compatible`` / ``custom`` types and on the
# operator-set ``owner_company`` label.
SELF_HOSTED_PROVIDER_TYPES = {
    "ollama", "vllm", "llamacpp", "lmstudio", "localai",
}

# v5.23.0 — self-hosted cold-load timeout. Provider.timeout_sec and the
# create-provider API historically defaulted to 30s (ProviderForm empty
# form: 60s). A 30–90s GGUF load of a local 30B trips that deadline,
# litellm raises, and the circuit breaker opens the only local route.
# See docs/5.23-local-accelerator-orchestration-backpressure-design.md §1.2.
HOSTED_DEFAULT_TIMEOUT_SEC = 30
SELF_HOSTED_DEFAULT_TIMEOUT_SEC = 240
# Values that mean "operator never thought about this" — the SQLAlchemy
# column / API schema default (30) and the ProviderForm empty-form default (60).
_UNSET_SELF_HOSTED_TIMEOUTS = frozenset({None, 30, 60})


def default_timeout_sec_for_type(provider_type: str) -> int:
    """Create-time default: 240s for inherent self-hosted runtimes, 30s otherwise."""
    if provider_type in SELF_HOSTED_PROVIDER_TYPES:
        return SELF_HOSTED_DEFAULT_TIMEOUT_SEC
    return HOSTED_DEFAULT_TIMEOUT_SEC


def coerce_self_hosted_timeout(provider_type: str, timeout_sec: int | None) -> int:
    """Persist an honest default when creating/updating a self-hosted row.

    Legacy 30/60 values are treated as unset for self-hosted types so a
    form that still ships the historical default does not write a trap.
    Explicit values outside that set (including a deliberate tight
    timeout such as 15, or a longer 300) are kept.
    """
    if provider_type in SELF_HOSTED_PROVIDER_TYPES and timeout_sec in _UNSET_SELF_HOSTED_TIMEOUTS:
        return SELF_HOSTED_DEFAULT_TIMEOUT_SEC
    if timeout_sec is None or int(timeout_sec) <= 0:
        return default_timeout_sec_for_type(provider_type)
    return int(timeout_sec)


def effective_timeout_sec(provider: Any) -> int:
    """Timeout handed to litellm / the runs worker.

    Existing Ollama rows in the DB still carry timeout_sec=30. Lifting
    only those unset/legacy values keeps an operator-chosen 90 or 300
    intact while unblocking a cold 30B load on a default row.
    """
    raw = getattr(provider, "timeout_sec", None)
    if is_self_hosted_provider(provider) and raw in _UNSET_SELF_HOSTED_TIMEOUTS:
        return SELF_HOSTED_DEFAULT_TIMEOUT_SEC
    if raw is None or int(raw) <= 0:
        return HOSTED_DEFAULT_TIMEOUT_SEC
    return int(raw)


def is_self_hosted_provider(p: Any) -> bool:
    """Hard filter for ``coordinator-local`` (decision 29 correction
    2026-06-03 — wider than Ollama-only).

    True when EITHER:
    - provider_type is one of the inherent self-hosted runtimes, OR
    - provider_type is a generic compatibility shim AND extra_config
      carries ``self_hosted: True`` (operator opt-in), OR
    - owner_company is an internal/local sentinel (operator override).
    """
    if getattr(p, "provider_type", None) in SELF_HOSTED_PROVIDER_TYPES:
        return True
    ptype = getattr(p, "provider_type", None)
    if ptype in ("openai-compatible", "compatible", "custom"):
        extra = getattr(p, "extra_config", None) or {}
        if extra.get("self_hosted") is True:
            return True
    if getattr(p, "owner_company", None) in ("internal", "local", "self-hosted"):
        return True
    return False


async def resolve_logical_alias(
    db: AsyncSession, alias: str, api_key_id: str
) -> Optional[str]:
    """Return the alias string itself when it is a known logical alias,
    or ``None`` otherwise.

    The caller's existing alias→hint propagation path (see
    ``_request_pipeline.build_hint_with_auto_task`` + LMRH hint
    resolution) layers ``LOGICAL_ALIASES[alias]["hint"]`` into the
    request's LMRH hint string. ``coordinator-local`` carries the extra
    ``self_hosted_only`` flag which the dispatch layer enforces with
    ``is_self_hosted_provider`` outside the LMRH scorer.

    Marker pass-through — does NOT resolve to a concrete provider+model
    here (that's the LMRH ranker's job once the hint is in place). Keeps
    the existing alias mechanism (``resolve_alias`` below, DB-backed)
    the source of truth for concrete pins; the logical aliases live
    alongside as a parallel namespace.
    """
    if alias in LOGICAL_ALIASES:
        return alias
    return None


async def resolve_alias(db: AsyncSession, model: Optional[str]) -> Optional[ModelAlias]:
    """Return the ModelAlias row for this model name, or None if no alias exists."""
    if not model:
        return None
    result = await db.execute(select(ModelAlias).where(ModelAlias.alias == model))
    return result.scalar_one_or_none()
