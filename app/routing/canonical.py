"""
v3.4.1 — canonical model identity helpers.

Solves the duplication that surfaces in ``GET /v1/models`` when a single
upstream model is registered under multiple spellings (e.g. ``grok-3``
and ``x-ai/grok-3`` both representing the same physical Grok-3 model
served by grok.com).

Convention (v3.4.1):

  Canonical model_id   = OpenRouter-style ``<provider>/<model>`` slug
                         when the provider has one (most cloud models),
                         else the bare upstream name (Anthropic /
                         OpenAI direct names like ``claude-sonnet-4-6``,
                         ``gpt-4o`` are themselves canonical when there
                         is no widely-adopted prefixed spelling).
  Aliases              = alternate spellings the operator knows callers
                         send. Stored on the ``ModelCapability.aliases``
                         JSON column. Empty list = no aliases.

Pre-v3.4.1 the router matched ``model_id`` exactly, so to accept both
spellings the operator had to register two ``ModelCapability`` rows for
the same model. That's the root cause of the /v1/models duplication.

The new helpers:

- ``matches_capability(model, capability)`` — does this requested model
  name route to this capability row? True when ``model == capability.model_id``
  OR when ``model`` is in ``capability.aliases``.
- ``canonical_for(model, db)`` — given a model name as sent by the
  caller, return the canonical ``model_id`` it resolves to.
- ``derive_family(model_id)`` — strip provider prefix when present
  (e.g. ``x-ai/grok-3`` → ``grok-3``). Used by LMRHv2.1 family
  grouping when the operator hasn't classified ``model_family``
  explicitly.

The matcher is case-insensitive (operators paste cURL strings; case
varies across vendor docs).
"""
from __future__ import annotations

from typing import Iterable, Optional


def _norm(s: Optional[str]) -> str:
    """Normalize a model string for case-insensitive comparison.

    We don't normalize separators (``x-ai`` vs ``x_ai``) — operators
    intentionally pick one or the other and the proxy honours that
    choice. Only case is folded.
    """
    return (s or "").strip().lower()


def matches_capability(requested: str, model_id: str, aliases: Optional[Iterable[str]]) -> bool:
    """True if ``requested`` resolves to the capability identified by
    ``model_id`` + ``aliases``.

    Used by the router during candidate selection. Pre-v3.4.1 the
    equivalent test was ``requested == capability.model_id``; v3.4.1
    extends that to also accept any alias spelling.
    """
    if not requested:
        return False
    n = _norm(requested)
    if n == _norm(model_id):
        return True
    if aliases:
        for a in aliases:
            if _norm(a) == n:
                return True
    return False


# v3.6.0 — known family enum for operator-driven model_family edits.
# Soft-warn (200 + X-Warning) on values outside this set, NOT a hard
# reject — operators may legitimately classify novel architectures
# under non-standard names. Update this set when a new family becomes
# canonical (e.g. when a major OSS model line emerges).
#
# Hub team consumes this constant via the OpenAPI spec for client-side
# pre-validation of the family field in their model-identity edit UI.
KNOWN_FAMILIES = frozenset({
    "claude",
    "gpt",
    "gemini",
    "grok",
    "llama",
    "mistral",
    "cohere",
    "deepseek",
})


def derive_family(model_id: str) -> str:
    """Strip a single ``provider/`` prefix to produce the bare family
    name. ``x-ai/grok-3`` → ``grok-3``; ``openai/gpt-4o`` → ``gpt-4o``;
    ``claude-sonnet-4-6`` → ``claude-sonnet-4-6`` (no prefix to strip).

    Used as a fallback when the operator hasn't set ``model_family``
    explicitly on a ``ModelCapability`` row. The split is single-pass
    so paths with multiple slashes (``vendor/family/variant``) keep
    everything after the first slash — that's by design; a vendor that
    nests slashes can override by setting ``model_family`` explicitly.
    """
    if not model_id:
        return ""
    if "/" in model_id:
        return model_id.split("/", 1)[1]
    return model_id


def collect_canonical_aliases(model_id: str, aliases: Optional[Iterable[str]]) -> list[str]:
    """Return the deduplicated list of all spellings the proxy will
    accept for this capability — canonical + aliases, in that order.

    Order matters for the ``/v1/models`` ``aliases`` field: callers
    who pick the first entry get the canonical name. Case-insensitive
    de-dupe so we don't emit ``x-ai/grok-3`` AND ``X-AI/grok-3``.
    """
    out: list[str] = []
    seen: set[str] = set()
    for s in [model_id, *(aliases or [])]:
        if not s:
            continue
        key = _norm(s)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
