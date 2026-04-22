"""
CoT-E — Chain-of-Thought Emulation Pipeline
Adds a reasoning layer to non-native-thinking models.

Pipeline: Plan → Initial Draft → [Critique → Refine] × N → [Verify] → Stream Final

All intermediate passes are emitted as Anthropic-format thinking blocks so
the caller sees the same structure as Claude's native extended thinking.
"""
import asyncio
import json
import logging
import re
from typing import AsyncIterator

import litellm

from app.config import settings
from app.cot.session import get_session_analyses, save_session_analysis

logger = logging.getLogger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

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

VERIFY_SYSTEM = (
    "You are a verification assistant for technical and infrastructure tasks.\n\n"
    "Given a question and a completed answer, produce concise verification steps "
    "that confirm the answer's steps were applied correctly and are working as expected.\n\n"
    "Reply in this EXACT format:\n"
    "## Verification Steps\n"
    "1. `<exact command or check>` → <what success looks like / key string to look for>\n"
    "2. `<exact command or check>` → <expected result>\n"
    "...\n\n"
    "Rules:\n"
    "- Only include steps that can be run immediately after applying the answer\n"
    "- Prefer read-only / non-destructive checks (status, logs, curl, grep)\n"
    "- Include the expected output or the key phrase that confirms success\n"
    "- Maximum 5 steps — be selective, not exhaustive\n"
    "- If the answer is conceptual (no actionable steps to verify), reply:\n"
    "  ## Verification Steps\n  (not applicable — no executable steps in answer)"
)

# Infrastructure CLI tools whose presence signals a verifiable answer
_INFRA_TOOLS: frozenset[str] = frozenset({
    "docker", "kubectl", "systemctl", "journalctl", "service ",
    "rabbitmqctl", "rabbitmq-plugins", "rabbitmq-diagnostics",
    "asterisk", "fs_cli", "opensips", "osipsctl",
    "nginx", "apache2ctl", "httpd",
    "certbot", "acme.sh",
    "iptables", "ufw", "firewall-cmd",
    "ip route", "ip addr", "ip link", "nmcli", "netstat", "ss -",
    "mysql", "mysqladmin", "psql", "redis-cli", "mongosh",
    "curl -", "wget ", "ssh ", "scp ", "rsync ",
    "supervisorctl", "pm2 ", "gunicorn", "uwsgi",
})

_SHELL_CODE_BLOCK = re.compile(r"```(?:bash|sh|shell|zsh|fish|powershell)", re.IGNORECASE)


# ── SSE helpers ───────────────────────────────────────────────────────────────

def _sse_thinking_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"thinking","thinking":""}}}}\n\n'.encode()


def _sse_thinking_delta(index: int, text: str) -> bytes:
    escaped = json.dumps(text)[1:-1]
    return f'data: {{"type":"content_block_delta","index":{index},"delta":{{"type":"thinking_delta","thinking":"{escaped}"}}}}\n\n'.encode()


def _sse_thinking_stop(index: int) -> bytes:
    return f'data: {{"type":"content_block_stop","index":{index}}}\n\n'.encode()


def _sse_text_start(index: int) -> bytes:
    return f'data: {{"type":"content_block_start","index":{index},"content_block":{{"type":"text","text":""}}}}\n\n'.encode()


def _sse_text_delta(index: int, text: str) -> bytes:
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


# ── Core helpers ──────────────────────────────────────────────────────────────

async def _call(model: str, messages: list[dict], system: str, max_tokens: int, **kwargs) -> str:
    """Non-streaming litellm call, returns text.

    Strips max_tokens and system from kwargs to prevent duplicate-argument
    errors when extra_kwargs from the endpoint already contains those keys.
    """
    kwargs.pop("max_tokens", None)
    kwargs.pop("system", None)
    resp = await litellm.acompletion(
        model=model,
        messages=[{"role": "system", "content": system}] + messages,
        max_tokens=max_tokens,
        stream=False,
        **kwargs,
    )
    return resp.choices[0].message.content or ""


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
    m = re.search(r"SCORE:\s*(\d+)", critique, re.IGNORECASE)
    return int(m.group(1)) if m else 5


def _parse_gaps(critique: str) -> str:
    m = re.search(r"GAPS:\s*(.+)", critique, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _should_verify(answer: str) -> bool:
    """
    Heuristic: return True if the answer likely contains infrastructure
    commands worth verifying.

    Two independent signals — either is sufficient:
      1. A fenced code block with a shell language marker (bash/sh/shell/zsh/…)
      2. Two or more distinct infra CLI tool names present in the answer
    """
    if _SHELL_CODE_BLOCK.search(answer):
        return True
    text_lower = answer.lower()
    hits = sum(1 for tool in _INFRA_TOOLS if tool in text_lower)
    return hits >= 2


# ── Pipeline ──────────────────────────────────────────────────────────────────

async def run_cot_pipeline(
    model: str,
    messages: list[dict],
    session_id: str | None,
    extra_kwargs: dict,
    max_iterations: int | None = None,
    force_verify: bool | None = None,
) -> AsyncIterator[bytes]:
    """
    Full CoT-E pipeline. Yields Anthropic-format SSE bytes.

    Thinking blocks (Plan, Quality Check, Refinement, Verification) precede
    the final text block. All intermediate LLM calls are non-streaming and
    buffered; only the final answer is chunked to the client.

    Args:
        max_iterations: override settings.cot_max_iterations for this request.
        force_verify:   True  → always run verification pass
                        False → never run verification pass
                        None  → use settings.cot_verify_enabled + auto-detection
    """
    block_index = 0
    # Strip keys that are passed explicitly to _call to avoid duplicate-arg errors
    cot_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system", "stream")}

    # ── Pass 0: Plan ──────────────────────────────────────────────────────────
    prior_analyses = await get_session_analyses(session_id)
    plan_context = ""
    if prior_analyses:
        plan_context = "\n\nPrior reasoning context:\n" + "\n---\n".join(prior_analyses[-3:])

    user_text = _last_user_text(messages)
    plan_text = await _call(
        model,
        [{"role": "user", "content": user_text + plan_context}],
        PLAN_SYSTEM,
        settings.cot_plan_max_tokens,
        **cot_kwargs,
    )
    await save_session_analysis(session_id, plan_text)

    yield _sse_thinking_start(block_index)
    yield _sse_thinking_delta(block_index, f"## Planning\n{plan_text}")
    yield _sse_thinking_stop(block_index)
    block_index += 1

    # ── Pass 1: Initial draft (buffered, not streamed) ────────────────────────
    augmented_system = (
        f"<augmented_reasoning>\n{plan_text}\n</augmented_reasoning>\n\n"
        "Use the reasoning above to produce a high-quality response."
    )
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}
    draft = await litellm.acompletion(
        model=model,
        messages=[{"role": "system", "content": augmented_system}] + messages,
        stream=False,
        **draft_kwargs,
    )
    draft_text = draft.choices[0].message.content or ""

    # ── Critique + Refinement loop ────────────────────────────────────────────
    current_answer = draft_text
    iterations = max_iterations if max_iterations is not None else settings.cot_max_iterations

    # Skip refinement for already-thorough long drafts
    draft_tokens = len(draft_text.split()) * 4 // 3  # rough word→token estimate
    if settings.cot_min_tokens_skip > 0 and draft_tokens >= settings.cot_min_tokens_skip:
        iterations = 0

    for iteration in range(1, iterations + 1):
        critique_system = CRITIQUE_SYSTEM.format(
            threshold=settings.cot_quality_threshold,
            max_tokens=settings.cot_critique_max_tokens,
        )
        critique_text = await _call(
            model,
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": current_answer},
                {"role": "user", "content": "Evaluate the above response."},
            ],
            critique_system,
            settings.cot_critique_max_tokens,
            **cot_kwargs,
        )

        yield _sse_thinking_start(block_index)
        yield _sse_thinking_delta(block_index, f"## Quality Check (iter {iteration})\n{critique_text}")
        yield _sse_thinking_stop(block_index)
        block_index += 1

        score = _parse_score(critique_text)
        gaps_line = _parse_gaps(critique_text)
        if score >= settings.cot_quality_threshold or gaps_line.lower() == "none":
            break

        refined = await litellm.acompletion(
            model=model,
            messages=[{"role": "system", "content": REFINE_SYSTEM}] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": current_answer},
                {"role": "user", "content": f"Critique:\n{critique_text}\n\nPlease improve your answer."},
            ],
            stream=False,
            **draft_kwargs,
        )
        current_answer = refined.choices[0].message.content or current_answer

        yield _sse_thinking_start(block_index)
        yield _sse_thinking_delta(block_index, f"## Refinement (iter {iteration})\n[Refined answer produced]")
        yield _sse_thinking_stop(block_index)
        block_index += 1

    # ── Verification pass ─────────────────────────────────────────────────────
    run_verify = _resolve_verify(force_verify, current_answer)
    if run_verify:
        verify_text = await _run_verify_pass(model, user_text, current_answer, extra_kwargs)
        yield _sse_thinking_start(block_index)
        yield _sse_thinking_delta(block_index, f"## Verification\n{verify_text}")
        yield _sse_thinking_stop(block_index)
        block_index += 1
        logger.debug("cot_verify_pass_complete", tokens=len(verify_text.split()))

    # ── Stream final answer ───────────────────────────────────────────────────
    yield _sse_text_start(block_index)
    chunk_size = 50
    for i in range(0, len(current_answer), chunk_size):
        yield _sse_text_delta(block_index, current_answer[i:i + chunk_size])
        await asyncio.sleep(0)  # yield control to event loop
    yield _sse_text_stop(block_index)

    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    output_tokens = len(current_answer) // 4
    yield _sse_message_delta("end_turn", input_tokens, output_tokens)
    yield _sse_done()


def _resolve_verify(force_verify: bool | None, answer: str) -> bool:
    """Decide whether to run the verification pass for this response."""
    if force_verify is True:
        return True
    if force_verify is False:
        return False
    # None → consult global settings + optional auto-detection
    if not settings.cot_verify_enabled:
        return False
    if settings.cot_verify_auto_detect:
        return _should_verify(answer)
    return True  # enabled globally with auto-detect off → always verify


async def _run_verify_pass(
    model: str,
    user_text: str,
    answer: str,
    extra_kwargs: dict,
) -> str:
    """Call the model to generate verification steps for the given answer."""
    verify_messages = [
        {"role": "user", "content": f"Question:\n{user_text}\n\nAnswer:\n{answer}"},
    ]
    try:
        cot_kw = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system", "stream")}
        return await _call(
            model,
            verify_messages,
            VERIFY_SYSTEM,
            settings.cot_verify_max_tokens,
            **cot_kw,
        )
    except Exception as e:
        logger.warning("cot_verify_pass_failed error=%s", str(e))
        return f"(verification pass failed: {e})"
