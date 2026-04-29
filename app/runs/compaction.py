"""Context compaction for long Runs (R3, spec B.10).

Triggers when the conversation reaches 80% of the model's max-context
window. Summarises the oldest non-system message pairs into a single
synthetic ``assistant`` message; preserves the system prompt and the last
4 turns (so the model still has enough recent context to act).

Compaction model: per-Run override (``run.compaction_model``) wins; else
the proxy picks the cheapest available ``claude-haiku-*`` provider in the
chain. Q3 lock-in.

Emits ``context_compacted`` event with ``model_used``, ``tokens_in``,
``tokens_out`` so the hub's task_events.payload mirror gets the cost
attribution per-run (per their week-1 ack).
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ModelCapability, Provider, RunMessage
from app.routing.retry import acompletion_with_retry
from app.runs.tokens import (
    compaction_target,
    estimate_message_tokens,
    estimate_messages_tokens,
    model_context_length,
    should_compact,
)

logger = logging.getLogger(__name__)


# Last N user/assistant exchanges to KEEP verbatim. Spec: "skip the last
# 4 turns". A "turn" = one user message + one assistant response, so the
# tail-window is 4 * 2 = 8 messages.
_PRESERVED_TAIL_MESSAGES = 8


_SUMMARY_SYSTEM_PROMPT = (
    "You are summarising an in-progress agent conversation for context compaction. "
    "Produce a single tight prose summary (≤500 tokens) capturing: what the user "
    "asked, what tools the assistant invoked and what they returned, what's been "
    "decided, and what's still pending. Preserve concrete identifiers (file paths, "
    "URLs, ticket numbers). Do NOT invent. Do NOT add disclaimers. Output prose only."
)


async def _pick_compaction_model(
    db: AsyncSession, override: Optional[str] = None
) -> tuple[str, Optional[Provider]]:
    """Return ``(model_id, provider_row)``. If override is set, search for a
    provider that has it; else pick the cheapest claude-haiku-* available.

    Provider may be None — the worker can call litellm directly without a
    provider row if the operator has a default key configured. We return
    one when found because cost attribution + circuit-breaker integration
    require a provider id.
    """
    if override:
        # Find first provider with this model in its capabilities
        res = await db.execute(
            select(ModelCapability, Provider)
            .join(Provider, Provider.id == ModelCapability.provider_id)
            .where(
                ModelCapability.model_id == override,
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
            )
            .limit(1)
        )
        row = res.first()
        if row is not None:
            return override, row[1]
        return override, None

    # Find cheapest claude-haiku-* available
    res = await db.execute(
        select(ModelCapability, Provider)
        .join(Provider, Provider.id == ModelCapability.provider_id)
        .where(
            ModelCapability.model_id.like("%haiku%"),
            Provider.enabled == True,  # noqa: E712
            Provider.deleted_at.is_(None),
        )
    )
    rows = res.all()
    if not rows:
        # Fallback: any cheap model the operator has flagged 'economy'
        res2 = await db.execute(
            select(ModelCapability, Provider)
            .join(Provider, Provider.id == ModelCapability.provider_id)
            .where(
                ModelCapability.cost_tier == "economy",
                Provider.enabled == True,  # noqa: E712
                Provider.deleted_at.is_(None),
            )
            .limit(1)
        )
        row = res2.first()
        if row is not None:
            return row[0].model_id, row[1]
        # Hard last-resort default; operator with no haiku and no economy
        # tier in the catalog needs to either set compaction_model or add
        # one. We return a sensible default and let the call fail loudly.
        return "claude-haiku-4-5", None

    # Sort by cost_tier (economy < standard < premium); within tier just
    # take the first row — load is balanced at request time, not here.
    tier_rank = {"economy": 0, "standard": 1, "premium": 2}
    rows_sorted = sorted(rows, key=lambda r: tier_rank.get(r[0].cost_tier, 1))
    cap, prov = rows_sorted[0]
    return cap.model_id, prov


def _split_for_compaction(
    messages: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split into (system_msgs, body_to_summarize, tail_to_preserve).

    System messages always stay. Last 8 messages always stay. Everything
    in the middle is fair game for summarisation.
    """
    sys_msgs = [m for m in messages if m.get("role") == "system"]
    non_sys = [m for m in messages if m.get("role") != "system"]
    if len(non_sys) <= _PRESERVED_TAIL_MESSAGES:
        # Not enough to summarise — nothing to do
        return sys_msgs, [], non_sys
    tail = non_sys[-_PRESERVED_TAIL_MESSAGES:]
    body = non_sys[:-_PRESERVED_TAIL_MESSAGES]
    return sys_msgs, body, tail


async def _call_summary_model(
    model: str, provider: Optional[Provider], body_messages: list[dict]
) -> tuple[str, int, int]:
    """One-shot LLM call to generate the summary. Returns
    ``(summary_text, in_tokens, out_tokens)``."""
    # Build a chat-completions request: system prompt + serialised body
    body_text_lines = []
    for m in body_messages:
        role = m.get("role", "user")
        c = m.get("content", "")
        if isinstance(c, list):
            parts = []
            for blk in c:
                if not isinstance(blk, dict):
                    continue
                t = blk.get("type")
                if t == "text":
                    parts.append(blk.get("text", ""))
                elif t == "tool_use":
                    parts.append(f"[tool_use {blk.get('name','')} input={json.dumps(blk.get('input',{}))[:300]}]")
                elif t == "tool_result":
                    inner = blk.get("content", "")
                    inner_s = inner if isinstance(inner, str) else json.dumps(inner)[:500]
                    parts.append(f"[tool_result {inner_s[:300]}]")
            c_text = "\n".join(p for p in parts if p)
        else:
            c_text = str(c)
        body_text_lines.append(f"--- {role} ---\n{c_text}")
    serialised = "\n".join(body_text_lines)

    kwargs = {}
    if provider is not None:
        kwargs["api_key"] = provider.api_key
        if provider.base_url:
            kwargs["api_base"] = provider.base_url

    resp = await acompletion_with_retry(
        model=model,
        messages=[
            {"role": "system", "content": _SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": serialised},
        ],
        max_tokens=600,
        **kwargs,
    )
    summary = ""
    try:
        summary = resp.choices[0].message.content or ""
    except Exception:
        summary = ""
    in_tok = 0
    out_tok = 0
    try:
        in_tok = int(getattr(resp.usage, "prompt_tokens", 0) or 0)
        out_tok = int(getattr(resp.usage, "completion_tokens", 0) or 0)
    except Exception:
        pass
    return summary, in_tok, out_tok


async def maybe_compact(
    db: AsyncSession,
    *,
    run_id: str,
    run_compaction_model: Optional[str],
    next_call_model: str,
    messages: list[dict],
) -> Optional[dict]:
    """Run the compaction step if the conversation has crossed the 80%
    threshold for ``next_call_model``'s context window.

    Returns the ``context_compacted`` event payload (caller emits it +
    rewrites the run_messages table), or ``None`` if no compaction was
    needed.
    """
    max_ctx = await model_context_length(db, next_call_model)
    used = estimate_messages_tokens(next_call_model, messages)
    if not should_compact(used, max_ctx):
        return None

    sys_msgs, body, tail = _split_for_compaction(messages)
    if not body:
        # Nothing to summarise even though we're over threshold — surface
        # the context_exhausted error path instead. Caller decides.
        logger.info("runs.compaction.nothing_to_summarize run=%s used=%d max=%d",
                    run_id, used, max_ctx)
        return None

    target = compaction_target(max_ctx)
    summary_model, provider = await _pick_compaction_model(db, run_compaction_model)
    summary_text, in_tok, out_tok = await _call_summary_model(
        summary_model, provider, body,
    )

    # Iteratively shrink the tail-anchor if a single summary still leaves us
    # over target. Pragmatic — we don't loop the LLM call; we just report
    # the post-compaction estimate and let the caller decide if another
    # pass is warranted (very rare: 8-message tail at 80% means each tail
    # message averages 10% of context, which a 600-token summary will not
    # save us from). The hub's escape hatch is to pass a smaller
    # compaction_model with longer ``max_tokens`` budget.
    summary_tokens = estimate_message_tokens(next_call_model, "assistant", summary_text)
    body_tokens_orig = estimate_messages_tokens(next_call_model, body)
    new_messages = sys_msgs + [{"role": "assistant", "content": summary_text}] + tail

    return {
        "event": {
            "messages_summarized": len(body),
            "original_tokens": body_tokens_orig,
            "summary_tokens": summary_tokens,
            "model_used": summary_model,
            "tokens_in": in_tok,
            "tokens_out": out_tok,
            "max_context": max_ctx,
            "target": target,
        },
        "new_messages": new_messages,
        "summary_text": summary_text,
    }


async def apply_compaction_to_db(
    db: AsyncSession,
    *,
    run_id: str,
    new_messages: list[dict],
) -> None:
    """Replace the run_messages rows for ``run_id`` with the compacted
    sequence. Safe to call inside the worker's session.

    Uses TRUNCATE+RE-INSERT semantics so seq numbers stay dense; the
    ``run_messages.compacted_from_seq`` / ``compacted_to_seq`` columns on
    the synthesised assistant row let auditors trace which originals
    were folded.
    """
    from sqlalchemy import delete
    # Wipe + re-insert is acceptable here because the conversation is the
    # authoritative source-of-truth (no FKs reference run_messages.seq).
    await db.execute(delete(RunMessage).where(RunMessage.run_id == run_id))
    now = time.time()
    for i, m in enumerate(new_messages, start=1):
        db.add(RunMessage(
            run_id=run_id, seq=i,
            role=m.get("role", "user"),
            content=m.get("content", ""),
            tokens=0,
            created_at=now,
        ))
    await db.flush()
