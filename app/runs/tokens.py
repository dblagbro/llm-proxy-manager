"""Token counting + context-length lookup for the Run runtime (R3).

We use ``litellm.token_counter`` (already a dep) rather than vendoring a
tokenizer. It dispatches to the right per-model BPE/SentencePiece library
and falls back to a heuristic when the model is unknown.

Context length per model is read from the existing ``model_capabilities``
table. Default fallback: 128k tokens — matches the spec's "approaching the
model's limit (≥80% of model.max_context_tokens)" wording where
``max_context_tokens`` is per-model not per-provider.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

import litellm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ModelCapability

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_LENGTH = 128_000
COMPACTION_THRESHOLD = 0.80              # spec B.10: "≥80% of model.max_context_tokens"
COMPACTION_TARGET_RATIO = 0.50           # bring usage back below this after compaction


def estimate_message_tokens(model: str, role: str, content) -> int:
    """Best-effort token count for a single conversation message.

    ``content`` may be a plain string or a list of Anthropic-format content
    blocks (text, tool_use, tool_result). litellm.token_counter consumes
    OpenAI-shape messages, so we serialise content blocks into a synthetic
    text string for the count — slightly conservative (overestimates by a
    handful of tokens on tool-heavy turns) which is what we want for a
    compaction trigger.
    """
    if isinstance(content, list):
        parts = []
        for blk in content:
            if not isinstance(blk, dict):
                parts.append(str(blk))
                continue
            t = blk.get("type")
            if t == "text":
                parts.append(blk.get("text", ""))
            elif t == "tool_use":
                # Tool name + JSON args — approximate
                parts.append(f"<tool_use name={blk.get('name','')} input={blk.get('input',{})!r}>")
            elif t == "tool_result":
                parts.append(f"<tool_result {blk.get('content','')!r}>")
            else:
                parts.append(str(blk))
        text = "\n".join(parts)
    else:
        text = str(content or "")

    try:
        return int(litellm.token_counter(
            model=model or "gpt-4o",
            messages=[{"role": role, "content": text}],
        ))
    except Exception as e:
        # litellm hiccups on some custom model names — fall back to a
        # 4-chars-per-token heuristic so compaction still has data to
        # decide with.
        logger.debug("token_counter fallback for model=%s: %s", model, e)
        return max(1, len(text) // 4)


def estimate_messages_tokens(model: str, messages: Iterable[dict]) -> int:
    """Sum per-message token estimates."""
    return sum(
        estimate_message_tokens(model, m.get("role", "user"), m.get("content", ""))
        for m in messages
    )


async def model_context_length(db: AsyncSession, model: str) -> int:
    """Return the model's max-context window.

    Reads the existing ``model_capabilities.context_length`` row. Falls
    back to ``DEFAULT_CONTEXT_LENGTH`` if the model isn't catalogued —
    operators add capabilities when they enable a new provider, so this
    fallback only fires for ad-hoc Runs against new models.
    """
    if not model:
        return DEFAULT_CONTEXT_LENGTH
    res = await db.execute(
        select(ModelCapability.context_length).where(
            ModelCapability.model_id == model,
        ).limit(1)
    )
    val = res.scalar()
    if val and val > 0:
        return int(val)
    return DEFAULT_CONTEXT_LENGTH


def should_compact(used_tokens: int, max_tokens: int) -> bool:
    """80% threshold per spec B.10."""
    if max_tokens <= 0:
        return False
    return used_tokens >= int(max_tokens * COMPACTION_THRESHOLD)


def compaction_target(max_tokens: int) -> int:
    """Number of tokens to aim for AFTER compaction (50%). Compaction
    keeps summarising oldest pairs until usage drops below this."""
    return int(max_tokens * COMPACTION_TARGET_RATIO)
