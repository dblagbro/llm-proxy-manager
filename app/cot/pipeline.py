"""
CoT-E — Chain-of-Thought Emulation Pipeline
Adds a reasoning layer to non-native-thinking models.

Pipeline: Plan → Initial Draft → [Critique → Refine] × N → Stream Final

All intermediate passes are emitted as Anthropic-format thinking blocks so
the caller sees the same structure as Claude's native extended thinking.
"""
import asyncio
import logging
import time
from typing import AsyncIterator, Any

import litellm

from app.config import settings
from app.cot.session import get_session_analyses, save_session_analysis

logger = logging.getLogger(__name__)

PLAN_SYSTEM = (
    "You are a reasoning planner. Analyse the user's request and identify:\n"
    "1. The core task and goal\n"
    "2. Key constraints and edge cases\n"
    "3. Recommended approach and steps\n"
    "Be concise. This output will guide the main response."
)

CRITIQUE_SYSTEM = (
    "You are a quality evaluator. Review the draft response and reply in this exact format:\n"
    "SCORE: <1-10>\n"
    "GAPS: <brief description of gaps, or 'none' if score >= {threshold}>\n"
    "Be concise — max {max_tokens} tokens."
)

REFINE_SYSTEM = (
    "You are an expert assistant. A draft response has been critiqued. "
    "Produce an improved, complete answer addressing the identified gaps."
)


async def _call(model: str, messages: list[dict], system: str, max_tokens: int, **kwargs) -> str:
    """Non-streaming litellm call, returns text."""
    resp = await litellm.acompletion(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=max_tokens,
        stream=False,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


def _sse_thinking_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"thinking","thinking":""}}}}\n\n'.encode()


def _sse_thinking_delta(index: int, text: str) -> bytes:
    import json
    escaped = json.dumps(text)[1:-1]
    return f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"thinking_delta","thinking":"{escaped}"}}}}\n\n'.encode()


def _sse_thinking_stop(index: int) -> bytes:
    return f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()


def _sse_text_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()


def _sse_text_delta(index: int, text: str) -> bytes:
    import json
    escaped = json.dumps(text)[1:-1]
    return f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"text_delta","text":"{escaped}"}}}}\n\n'.encode()


def _sse_text_stop(index: int) -> bytes:
    return f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()


def _sse_message_delta(stop_reason: str, input_tokens: int, output_tokens: int) -> bytes:
    return (
        f'data: {{"type":"message_delta","delta":{{"stop_reason":"{stop_reason}","stop_sequence":null}},'
        f'"usage":{{"input_tokens":{input_tokens},"output_tokens":{output_tokens}}}}}\n\n'
    ).encode()


def _sse_done() -> bytes:
    return b'data: {"type":"message_stop"}\n\ndata: [DONE]\n\n'


async def run_cot_pipeline(
    model: str,
    messages: list[dict],
    session_id: str | None,
    extra_kwargs: dict,
    max_iterations: int | None = None,
) -> AsyncIterator[bytes]:
    """
    Full CoT-E pipeline. Yields SSE bytes.
    Thinking blocks precede the final text block.
    """
    block_index = 0

    # ── Pass 0: Plan ─────────────────────────────────────────────────────────
    prior_analyses = await get_session_analyses(session_id)
    plan_context = ""
    if prior_analyses:
        plan_context = "\n\nPrior reasoning context:\n" + "\n---\n".join(prior_analyses[-3:])

    user_text = _last_user_text(messages)
    plan_messages = [{"role": "user", "content": user_text + plan_context}]

    plan_text = await _call(
        model, plan_messages, PLAN_SYSTEM, settings.cot_plan_max_tokens, **extra_kwargs
    )
    await save_session_analysis(session_id, plan_text)

    # Emit planning thinking block
    yield _sse_thinking_start(block_index)
    yield _sse_thinking_delta(block_index, f"## Planning\n{plan_text}")
    yield _sse_thinking_stop(block_index)
    block_index += 1

    # ── Pass 1: Initial draft (not streamed to client) ────────────────────────
    augmented_system = (
        f"<augmented_reasoning>\n{plan_text}\n</augmented_reasoning>\n\n"
        "Use the reasoning above to produce a high-quality response."
    )
    draft_messages = [{"role": "system", "content": augmented_system}] + messages
    draft = await litellm.acompletion(
        model=model,
        messages=draft_messages,
        stream=False,
        **extra_kwargs,
    )
    draft_text = draft.choices[0].message.content or ""

    # ── Critique + Refinement loop ────────────────────────────────────────────
    current_answer = draft_text
    iterations = max_iterations if max_iterations is not None else settings.cot_max_iterations

    # Skip refinement when the draft is already long (high token count signals
    # the model produced a thorough answer; critique would rarely improve it).
    draft_tokens = len(draft_text.split()) * 4 // 3  # rough word→token estimate
    if settings.cot_min_tokens_skip > 0 and draft_tokens >= settings.cot_min_tokens_skip:
        iterations = 0

    for iteration in range(1, iterations + 1):
        critique_system = CRITIQUE_SYSTEM.format(
            threshold=settings.cot_quality_threshold,
            max_tokens=settings.cot_critique_max_tokens,
        )
        critique_messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": current_answer},
            {"role": "user", "content": "Evaluate the above response."},
        ]
        critique_text = await _call(
            model, critique_messages, critique_system,
            settings.cot_critique_max_tokens, **extra_kwargs
        )

        # Emit critique thinking block
        yield _sse_thinking_start(block_index)
        yield _sse_thinking_delta(block_index, f"## Quality Check (iter {iteration})\n{critique_text}")
        yield _sse_thinking_stop(block_index)
        block_index += 1

        # Parse score
        score = _parse_score(critique_text)
        gaps_line = _parse_gaps(critique_text)
        if score >= settings.cot_quality_threshold or gaps_line.lower() == "none":
            break

        # Refinement pass
        refine_messages = [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": current_answer},
            {"role": "user", "content": f"Critique:\n{critique_text}\n\nPlease improve your answer."},
        ]
        refined = await litellm.acompletion(
            model=model,
            messages=[{"role": "system", "content": REFINE_SYSTEM}] + refine_messages,
            stream=False,
            **extra_kwargs,
        )
        current_answer = refined.choices[0].message.content or current_answer

        # Emit refinement thinking block
        yield _sse_thinking_start(block_index)
        yield _sse_thinking_delta(block_index, f"## Refinement (iter {iteration})\n[Refined answer produced]")
        yield _sse_thinking_stop(block_index)
        block_index += 1

    # ── Stream final answer ───────────────────────────────────────────────────
    yield _sse_text_start(block_index)
    chunk_size = 50
    for i in range(0, len(current_answer), chunk_size):
        yield _sse_text_delta(block_index, current_answer[i:i + chunk_size])
        await asyncio.sleep(0)  # yield control
    yield _sse_text_stop(block_index)

    # Approximate token counts
    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    output_tokens = len(current_answer) // 4
    yield _sse_message_delta("end_turn", input_tokens, output_tokens)
    yield _sse_done()


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return block.get("text", "")
    return ""


def _parse_score(critique: str) -> int:
    import re
    m = re.search(r"SCORE:\s*(\d+)", critique, re.IGNORECASE)
    return int(m.group(1)) if m else 5


def _parse_gaps(critique: str) -> str:
    import re
    m = re.search(r"GAPS:\s*(.+)", critique, re.IGNORECASE)
    return m.group(1).strip() if m else ""
