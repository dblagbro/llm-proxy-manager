"""Wave 5 #26 — Long-context map-reduce.

When the input exceeds the selected provider's effective context window,
chunk the history into overlapping spans, summarise each span against the
user's current question, then concatenate summaries with the original
question (and the tail of the conversation preserved verbatim — the
"anchor retention" pattern).

Opt-in via `X-Context-Strategy: truncate|mapreduce|error` request header:
  truncate  — (default) just drop the oldest messages until we fit
  mapreduce — summarise-then-merge, slower but preserves signal
  error     — return 413 Payload Too Large with X-Context-Tokens: N
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Approx tokens per character — deliberately pessimistic (3 chars/token)
# to avoid submitting over the limit.
_CHARS_PER_TOKEN = 3


def _content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", ""))
        return "\n".join(parts)
    return ""


def estimate_tokens(messages: list[dict], system: Optional[str] = None) -> int:
    chars = len(system or "")
    for m in messages:
        chars += len(_content_to_text(m.get("content", "")))
    return chars // _CHARS_PER_TOKEN


def effective_window(context_length: int, fraction: float = 0.75) -> int:
    """Leave ~25% headroom for the assistant's response and overhead."""
    return int(context_length * fraction)


def needs_compression(messages: list[dict], context_length: int, system: Optional[str] = None) -> bool:
    return estimate_tokens(messages, system) > effective_window(context_length)


def resolve_strategy(header_value: Optional[str]) -> str:
    v = (header_value or "").lower().strip()
    if v in ("truncate", "mapreduce", "map-reduce", "error"):
        return "mapreduce" if v == "map-reduce" else v
    return "truncate"


def truncate_to_window(
    messages: list[dict], context_length: int, system: Optional[str] = None,
    keep_last: int = 6,
) -> tuple[list[dict], int]:
    """Default strategy — drop the oldest messages until we fit, preserving
    the last `keep_last` messages verbatim. Returns (new_messages, dropped)."""
    window = effective_window(context_length)
    if estimate_tokens(messages, system) <= window:
        return messages, 0

    # Always keep the tail
    tail = messages[-keep_last:] if len(messages) > keep_last else messages[:]
    head_budget = window - estimate_tokens(tail, system)
    head_budget = max(0, head_budget)

    kept_head: list[dict] = []
    running = 0
    for m in messages[:-keep_last] if len(messages) > keep_last else []:
        mt = estimate_tokens([m])
        if running + mt > head_budget:
            break
        kept_head.append(m)
        running += mt

    out = kept_head + tail
    dropped = len(messages) - len(out)
    return out, dropped


async def mapreduce_compress(
    messages: list[dict],
    *,
    model: str,
    extra: dict,
    context_length: int,
    user_question: str,
    keep_last: int = 4,
) -> tuple[list[dict], int, int]:
    """Summarise older messages in chunks, keep the last N verbatim.

    Returns (new_messages, chunks_summarised, original_tokens).
    """
    from app.routing.retry import acompletion_with_retry
    import asyncio as _asyncio

    orig_tokens = estimate_tokens(messages)
    tail = messages[-keep_last:] if len(messages) > keep_last else messages[:]
    head = messages[:-keep_last] if len(messages) > keep_last else []

    if not head:
        return messages, 0, orig_tokens

    # Chunk `head` so each chunk is ~15% of the window (room for 6-ish summaries)
    chunk_size_tokens = max(1000, int(context_length * 0.15))
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for m in head:
        mt = estimate_tokens([m])
        if current and current_tokens + mt > chunk_size_tokens:
            chunks.append(current)
            # 20% overlap — carry the last message into the next chunk
            current = [current[-1]] if current else []
            current_tokens = estimate_tokens(current)
        current.append(m)
        current_tokens += mt
    if current:
        chunks.append(current)

    if not chunks:
        return messages, 0, orig_tokens

    system_prompt = (
        "You are a summariser. Below is a chunk of conversation history. "
        "Extract facts that are likely relevant to this upcoming question:\n"
        f"    {user_question[:500]}\n\n"
        "Output concise bullet points — 150 words max. Preserve names, numbers, "
        "and identifiers verbatim. Do not answer the question itself."
    )

    async def _summarise_one(chunk: list[dict]) -> str:
        chunk_text = "\n\n".join(
            f"[{m.get('role')}]: {_content_to_text(m.get('content'))[:2000]}"
            for m in chunk
        )
        call_kwargs = {k: v for k, v in extra.items() if k not in ("max_tokens", "system", "stream", "tools", "tool_choice")}
        try:
            resp = await acompletion_with_retry(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": chunk_text},
                ],
                max_tokens=250,
                stream=False,
                **call_kwargs,
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("long_context.chunk_summary_failed %s", exc)
            return "(summary unavailable for this chunk)"

    summaries = await _asyncio.gather(*[_summarise_one(c) for c in chunks])
    summary_text = "## Earlier conversation — compressed\n\n" + "\n\n---\n\n".join(
        f"**Chunk {i + 1}:**\n{s}" for i, s in enumerate(summaries)
    )

    # Replace head with a single synthetic assistant summary message
    out_messages = [{"role": "user", "content": summary_text}] + tail
    return out_messages, len(chunks), orig_tokens
