"""
CoT-E — Chain-of-Thought Emulation Pipeline
Adds a reasoning layer to non-native-thinking models.

Pipeline: Plan → Initial Draft → [Critique → Refine] × N → [Verify] → Stream Final

All intermediate passes are emitted as Anthropic-format thinking blocks so
the caller sees the same structure as Claude's native extended thinking.
"""
import asyncio
import logging
import re
from typing import AsyncIterator

import litellm

from app.config import settings
from app.routing.retry import acompletion_with_retry
from app.cot.session import get_session_analyses, save_session_analysis
from app.cot.sse import (
    sse_thinking_start, sse_thinking_delta, sse_thinking_stop,
    sse_text_start, sse_text_delta, sse_text_stop,
    sse_message_delta, sse_done,
)

logger = logging.getLogger(__name__)


def parse_cot_request_headers(
    x_cot_iterations: str | None,
    x_cot_verify: str | None,
    x_cot_samples: str | None = None,
    x_cot_mode: str | None = None,
) -> tuple[int | None, bool | None, int]:
    """Parse CoT request headers into typed (cot_max, force_verify, samples).

    X-Cot-Samples: integer N>1 activates Self-Consistency mode with N drafts.
    X-Cot-Mode: 'self-consistency' is an alias for X-Cot-Samples: 3 when
                 X-Cot-Samples wasn't explicitly set.
    """
    cot_max: int | None = None
    if x_cot_iterations is not None:
        try:
            cot_max = max(0, int(x_cot_iterations))
        except ValueError:
            pass
    force_verify: bool | None = None
    if x_cot_verify is not None:
        force_verify = x_cot_verify.lower() in ("1", "true", "yes")

    samples = 1
    if x_cot_samples is not None:
        try:
            samples = max(1, min(10, int(x_cot_samples)))  # clamp 1..10
        except ValueError:
            pass
    elif (x_cot_mode or "").lower() == "self-consistency":
        samples = 3  # sensible default for mode flag

    return cot_max, force_verify, samples


# ── Prompts ───────────────────────────────────────────────────────────────────

PLAN_SYSTEM_VERBOSE = (
    "You are a reasoning planner. Analyse the user's request and identify:\n"
    "1. The core task and goal\n"
    "2. Key constraints and edge cases\n"
    "3. Recommended approach and steps\n"
    "Be concise. This output will guide the main response."
)

# Chain-of-Draft (Xu et al. 2025, arXiv:2502.18600): constrain plan steps to
# ~5 words each. Reported ~78% token reduction + ~76% TTFT reduction with
# <5pp quality drop on GSM8K. Better economics for streaming UX.
PLAN_SYSTEM_COMPACT = (
    "Plan the reasoning as numbered mini-steps. "
    "Each line: 1-7 words, no prose. No preamble, no summary.\n"
    "Format:\n"
    "1. <mini-step>\n"
    "2. <mini-step>\n"
    "..."
)

CRITIQUE_SYSTEM = (
    "You are a quality evaluator. Evaluate the draft response against the user's question.\n"
    "Reply with ONLY a JSON object, no prose, no markdown fences. Use this exact schema:\n"
    '{\n'
    '  "factual_issues": ["short description per issue"],\n'
    '  "missing_coverage": ["what the answer failed to address"],\n'
    '  "sufficient_for_user": true|false\n'
    '}\n\n'
    "Rules:\n"
    "- factual_issues: only items the answer gets wrong (not stylistic nits).\n"
    "- missing_coverage: things the user asked for that the answer didn't address.\n"
    "- sufficient_for_user: true only if the answer would satisfy the user as-is.\n"
    "- Empty arrays are fine (and expected) when the answer is good.\n"
    "Max {max_tokens} tokens. Output MUST be valid JSON."
)

REFINE_SYSTEM = (
    "You are an expert assistant. A draft response has been critiqued. "
    "Produce an improved, complete answer addressing the identified gaps."
)

RECONCILE_SYSTEM = (
    "You are a reconciler. Below are {n} independently generated candidate "
    "answers to the same user question. Identify the consensus across them, "
    "resolve any disagreements by weight of evidence, and produce a SINGLE "
    "final answer that reflects the majority reasoning.\n\n"
    "Do NOT explain your choice; do NOT reference the candidates; just emit "
    "the final answer the user should see."
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


# ── Core helpers ──────────────────────────────────────────────────────────────

async def _call(model: str, messages: list[dict], system: str, max_tokens: int, **kwargs) -> str:
    """Non-streaming litellm call, returns text.

    Strips max_tokens and system from kwargs to prevent duplicate-argument
    errors when extra_kwargs from the endpoint already contains those keys.
    """
    kwargs.pop("max_tokens", None)
    kwargs.pop("system", None)
    resp = await acompletion_with_retry(
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


def _parse_critique(critique: str) -> dict:
    """Parse the JSON rubric from a critique response.

    Returns a dict with keys: factual_issues (list), missing_coverage (list),
    sufficient_for_user (bool). Falls back to safe defaults if JSON parsing
    fails or the legacy SCORE:/GAPS: format is returned instead.
    """
    import json as _json
    # Strip markdown code fences if the model couldn't resist
    cleaned = critique.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    # Find the first { ... } block
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = _json.loads(cleaned[start:end + 1])
            return {
                "factual_issues": list(obj.get("factual_issues") or []),
                "missing_coverage": list(obj.get("missing_coverage") or []),
                "sufficient_for_user": bool(obj.get("sufficient_for_user", False)),
            }
        except (ValueError, TypeError):
            pass
    # Legacy fallback: parse SCORE:/GAPS: style
    score = _parse_score(critique)
    gaps_line = _parse_gaps(critique)
    gaps_empty = gaps_line.lower() in ("", "none", "n/a")
    return {
        "factual_issues": [],
        "missing_coverage": [] if gaps_empty else [gaps_line],
        "sufficient_for_user": score >= 8 or gaps_empty,
    }


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
    critique_model: str | None = None,
    critique_kwargs: dict | None = None,
    samples: int = 1,
    task_branch: str | None = None,
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
        critique_model: override model for the critique pass only. When set,
                        the critique runs on a different provider than the draft,
                        eliminating self-preference bias. Falls back to `model`
                        if None. (Wave 2 #8)
        critique_kwargs: litellm kwargs (api_key, api_base, timeout) for the
                         critique_model provider.
        samples:        Self-Consistency (Wave 2 #10). When >1, generate N
                        independent drafts in parallel at temperature=0.7 and
                        reconcile to consensus before entering the critique
                        loop. Published lift is +5-15pp on reasoning
                        benchmarks at ~N× cost. Default 1 (disabled).
    """
    block_index = 0
    # Strip keys that are passed explicitly to _call to avoid duplicate-arg errors
    cot_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system", "stream")}

    # ── Wave 2 #11 — task-adaptive branches ──────────────────────────────────
    if task_branch:
        user_text_early = _last_user_text(messages)
        if task_branch == "summarize":
            async for chunk in _run_summarize_branch(
                model, messages, user_text_early, extra_kwargs
            ):
                yield chunk
            return
        if task_branch == "math":
            async for chunk in _run_math_branch(
                model, messages, user_text_early, extra_kwargs
            ):
                yield chunk
            return
        if task_branch == "code":
            async for chunk in _run_code_branch(
                model, messages, user_text_early, extra_kwargs
            ):
                yield chunk
            return
        # unknown branch name — fall through to default pipeline

    # ── Pass 0: Plan ──────────────────────────────────────────────────────────
    prior_analyses = await get_session_analyses(session_id)
    plan_context = ""
    if prior_analyses:
        plan_context = "\n\nPrior reasoning context:\n" + "\n---\n".join(prior_analyses[-3:])

    user_text = _last_user_text(messages)
    # Wave 2 #12 — Chain-of-Draft compression: terse prompt + smaller budget
    plan_compact = getattr(settings, "cot_plan_compact", True)
    plan_system = PLAN_SYSTEM_COMPACT if plan_compact else PLAN_SYSTEM_VERBOSE
    plan_budget = min(settings.cot_plan_max_tokens, 120) if plan_compact else settings.cot_plan_max_tokens
    plan_text = await _call(
        model,
        [{"role": "user", "content": user_text + plan_context}],
        plan_system,
        plan_budget,
        **cot_kwargs,
    )
    await save_session_analysis(session_id, plan_text)

    yield sse_thinking_start(block_index)
    plan_label = "## Planning (compact)" if plan_compact else "## Planning"
    yield sse_thinking_delta(block_index, f"{plan_label}\n{plan_text}")
    yield sse_thinking_stop(block_index)
    block_index += 1

    # ── Pass 1: Initial draft (buffered, not streamed) ────────────────────────
    augmented_system = (
        f"<augmented_reasoning>\n{plan_text}\n</augmented_reasoning>\n\n"
        "Use the reasoning above to produce a high-quality response."
    )
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}

    if samples > 1:
        # Self-Consistency: N parallel drafts at T=0.7, reconcile to consensus
        import asyncio as _asyncio
        sc_kwargs = {**draft_kwargs, "temperature": 0.7}
        async def _one_draft():
            resp = await acompletion_with_retry(
                model=model,
                messages=[{"role": "system", "content": augmented_system}] + messages,
                stream=False,
                **sc_kwargs,
            )
            return resp.choices[0].message.content or ""
        drafts = await _asyncio.gather(*[_one_draft() for _ in range(samples)])

        yield sse_thinking_start(block_index)
        yield sse_thinking_delta(
            block_index,
            f"## Self-Consistency ({samples} parallel drafts)\n"
            + "\n".join(
                f"- Draft {i + 1}: {d[:200]}{'…' if len(d) > 200 else ''}"
                for i, d in enumerate(drafts)
            ),
        )
        yield sse_thinking_stop(block_index)
        block_index += 1

        # Reconcile to a single consensus answer
        candidates_text = "\n\n---\n\n".join(
            f"Candidate {i + 1}:\n{d}" for i, d in enumerate(drafts)
        )
        reconcile = await acompletion_with_retry(
            model=model,
            messages=[
                {"role": "system", "content": RECONCILE_SYSTEM.format(n=samples)},
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": candidates_text},
                {"role": "user", "content": "Produce the consensus answer."},
            ],
            stream=False,
            **draft_kwargs,
        )
        draft_text = reconcile.choices[0].message.content or drafts[0]
    else:
        draft = await acompletion_with_retry(
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

    # Resolve critique provider: use caller-supplied override, else fall back
    # to the draft model. Separate kwargs so the critique uses the correct
    # api_key / api_base for its provider.
    critique_model_effective = critique_model or model
    critique_call_kwargs = (
        {k: v for k, v in (critique_kwargs or {}).items() if k not in ("max_tokens", "system", "stream")}
        if critique_model else cot_kwargs
    )

    iterations_used = 0
    for iteration in range(1, iterations + 1):
        iterations_used = iteration
        critique_system = CRITIQUE_SYSTEM.format(
            max_tokens=settings.cot_critique_max_tokens,
        )
        critique_text = await _call(
            critique_model_effective,
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": current_answer},
                {"role": "user", "content": "Evaluate the above response."},
            ],
            critique_system,
            settings.cot_critique_max_tokens,
            **critique_call_kwargs,
        )

        rubric = _parse_critique(critique_text)
        # Render a human-readable thinking block (preserves original UX)
        issues = rubric["factual_issues"]
        missing = rubric["missing_coverage"]
        summary_parts: list[str] = []
        if issues:
            summary_parts.append("**Factual issues:**\n" + "\n".join(f"- {x}" for x in issues))
        if missing:
            summary_parts.append("**Missing coverage:**\n" + "\n".join(f"- {x}" for x in missing))
        if not (issues or missing):
            summary_parts.append("_No issues found._")
        summary_parts.append(f"**Sufficient for user:** {rubric['sufficient_for_user']}")
        critique_rendered = "\n\n".join(summary_parts)

        critique_label = (
            f"## Quality Check (iter {iteration}, via {critique_model_effective})"
            if critique_model and critique_model != model
            else f"## Quality Check (iter {iteration})"
        )
        yield sse_thinking_start(block_index)
        yield sse_thinking_delta(block_index, f"{critique_label}\n{critique_rendered}")
        yield sse_thinking_stop(block_index)
        block_index += 1

        # Stop when the answer is sufficient OR no concrete issues remain
        if rubric["sufficient_for_user"] or (not issues and not missing):
            break

        critique_for_refine = critique_rendered
        refined = await acompletion_with_retry(
            model=model,
            messages=[{"role": "system", "content": REFINE_SYSTEM}] + [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": current_answer},
                {"role": "user", "content": f"Critique:\n{critique_for_refine}\n\nPlease improve your answer."},
            ],
            stream=False,
            **draft_kwargs,
        )
        current_answer = refined.choices[0].message.content or current_answer

        yield sse_thinking_start(block_index)
        yield sse_thinking_delta(block_index, f"## Refinement (iter {iteration})\n[Refined answer produced]")
        yield sse_thinking_stop(block_index)
        block_index += 1

    try:
        from app.observability.prometheus import observe_cot_iterations
        observe_cot_iterations(model, iterations_used)
    except Exception:
        pass

    # ── Verification pass ─────────────────────────────────────────────────────
    run_verify = _resolve_verify(force_verify, current_answer)
    if run_verify:
        verify_text = await _run_verify_pass(model, user_text, current_answer, extra_kwargs)

        # Always parse into structured steps so agent frameworks can act on them
        from app.cot.verify_exec import (
            parse_verify_block, execute_step, render_executed_block,
            has_failures,
        )
        verify_steps = parse_verify_block(verify_text)

        # Optionally execute the network-safe subset (HTTP/DNS/TCP via stdlib)
        if settings.cot_verify_execute and verify_steps:
            try:
                from app.observability.prometheus import observe_verify_execution
            except Exception:
                observe_verify_execution = None
            for step in verify_steps:
                await execute_step(step, timeout_sec=settings.cot_verify_step_timeout_sec)
                if observe_verify_execution and step.status != "skipped":
                    try:
                        observe_verify_execution(step.status)
                    except Exception:
                        pass

            rendered = render_executed_block(verify_steps)
            yield sse_thinking_start(block_index)
            yield sse_thinking_delta(block_index, rendered)
            yield sse_thinking_stop(block_index)
            block_index += 1

            # Emit structured SSE event for agent frameworks
            import json as _json
            steps_json = _json.dumps([s.to_dict() for s in verify_steps])
            yield (
                f'event: verify_steps\ndata: {steps_json}\n\n'.encode()
            )

            # Reflexion: if any executed step failed, run ONE revision pass
            # with the actual failures surfaced back to the model.
            if has_failures(verify_steps):
                failures = [s for s in verify_steps if s.status in ("fail", "error")]
                failure_detail = "\n".join(
                    f"- `{s.command}` expected: {s.expected}; actual: {(s.actual or s.error)[:300]}"
                    for s in failures
                )
                reflexion = await acompletion_with_retry(
                    model=model,
                    messages=[
                        {"role": "system", "content": REFINE_SYSTEM},
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": current_answer},
                        {"role": "user", "content": (
                            "The following verification steps FAILED when executed. "
                            "Update your answer to be consistent with these actual outputs, "
                            "or explain why the expectation was wrong.\n\n" + failure_detail
                        )},
                    ],
                    stream=False,
                    **{k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system", "stream")},
                )
                current_answer = reflexion.choices[0].message.content or current_answer
                yield sse_thinking_start(block_index)
                yield sse_thinking_delta(
                    block_index,
                    f"## Refinement (post-verification)\nAnswer updated based on "
                    f"{len(failures)} failed verification step(s).",
                )
                yield sse_thinking_stop(block_index)
                block_index += 1
        else:
            yield sse_thinking_start(block_index)
            yield sse_thinking_delta(block_index, f"## Verification\n{verify_text}")
            yield sse_thinking_stop(block_index)
            block_index += 1
            # Still emit structured steps so clients can consume them
            if verify_steps:
                import json as _json
                steps_json = _json.dumps([s.to_dict() for s in verify_steps])
                yield (
                    f'event: verify_steps\ndata: {steps_json}\n\n'.encode()
                )

        logger.debug("cot_verify_pass_complete", tokens=len(verify_text.split()))

    # ── Stream final answer ───────────────────────────────────────────────────
    yield sse_text_start(block_index)
    chunk_size = 50
    for i in range(0, len(current_answer), chunk_size):
        yield sse_text_delta(block_index, current_answer[i:i + chunk_size])
        await asyncio.sleep(0)  # yield control to event loop
    yield sse_text_stop(block_index)

    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    output_tokens = len(current_answer) // 4
    yield sse_message_delta("end_turn", input_tokens, output_tokens)
    yield sse_done()


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


# ── Task-adaptive branches (Wave 2 #11) ──────────────────────────────────────

async def _run_summarize_branch(
    model: str, messages: list[dict], user_text: str, extra_kwargs: dict,
) -> AsyncIterator[bytes]:
    """task=summarize — single pass, no plan, no critique."""
    from app.cot.task_adaptive import SUMMARIZE_SYSTEM
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}
    resp = await acompletion_with_retry(
        model=model,
        messages=[{"role": "system", "content": SUMMARIZE_SYSTEM}] + messages,
        stream=False,
        **draft_kwargs,
    )
    answer = resp.choices[0].message.content or ""

    yield sse_thinking_start(0)
    yield sse_thinking_delta(0, "## Task: Summarize (single-pass)\nSkipping plan/critique/refine for summarization tasks.")
    yield sse_thinking_stop(0)

    yield sse_text_start(1)
    chunk_size = 50
    for i in range(0, len(answer), chunk_size):
        yield sse_text_delta(1, answer[i:i + chunk_size])
        await asyncio.sleep(0)
    yield sse_text_stop(1)
    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    yield sse_message_delta("end_turn", input_tokens, len(answer) // 4)
    yield sse_done()


async def _run_math_branch(
    model: str, messages: list[dict], user_text: str, extra_kwargs: dict,
) -> AsyncIterator[bytes]:
    """task=math — Program-of-Thought: generate Python, run it, report."""
    from app.cot.task_adaptive import (
        POT_SYSTEM, POT_REPORT_SYSTEM, extract_python, run_python_sandbox,
    )
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}

    gen = await acompletion_with_retry(
        model=model,
        messages=[{"role": "system", "content": POT_SYSTEM}] + messages,
        stream=False,
        **draft_kwargs,
    )
    gen_text = gen.choices[0].message.content or ""
    code = extract_python(gen_text)

    yield sse_thinking_start(0)
    yield sse_thinking_delta(
        0,
        "## Task: Math (Program-of-Thought)\n"
        + (f"```python\n{code}\n```" if code else f"(no code block found)\n{gen_text[:500]}"),
    )
    yield sse_thinking_stop(0)

    exec_result_text = ""
    if code:
        result = await asyncio.get_event_loop().run_in_executor(None, run_python_sandbox, code)
        status = "✓ success" if result.ok else ("⚠ timed out" if result.timed_out else f"✗ exit {result.returncode}")
        exec_result_text = f"```\n{result.stdout[:1000]}\n```"
        if result.stderr:
            exec_result_text += f"\n\nstderr:\n```\n{result.stderr[:500]}\n```"
        yield sse_thinking_start(1)
        yield sse_thinking_delta(1, f"## Execution ({status}, {result.duration_ms:.0f}ms)\n{exec_result_text}")
        yield sse_thinking_stop(1)

        report = await acompletion_with_retry(
            model=model,
            messages=[
                {"role": "system", "content": POT_REPORT_SYSTEM},
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": f"Computed output:\n{result.stdout}"},
                {"role": "user", "content": "Produce the final user-facing answer."},
            ],
            stream=False,
            **draft_kwargs,
        )
        answer = report.choices[0].message.content or gen_text
    else:
        # Couldn't extract code; just return what the model produced
        answer = gen_text

    yield sse_text_start(2)
    chunk_size = 50
    for i in range(0, len(answer), chunk_size):
        yield sse_text_delta(2, answer[i:i + chunk_size])
        await asyncio.sleep(0)
    yield sse_text_stop(2)
    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    yield sse_message_delta("end_turn", input_tokens, len(answer) // 4)
    yield sse_done()


async def _run_code_branch(
    model: str, messages: list[dict], user_text: str, extra_kwargs: dict,
) -> AsyncIterator[bytes]:
    """task=code — generate implementation + tests, run tests, refine on failure."""
    from app.cot.task_adaptive import (
        CODEGEN_TESTS_SYSTEM, extract_python, run_python_sandbox,
    )
    import re as _re
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}

    gen = await acompletion_with_retry(
        model=model,
        messages=[{"role": "system", "content": CODEGEN_TESTS_SYSTEM}] + messages,
        stream=False,
        **draft_kwargs,
    )
    gen_text = gen.choices[0].message.content or ""

    # Extract BOTH python blocks (implementation + tests)
    blocks = _re.findall(r"```(?:python|py)\s*\n(.*?)\n```", gen_text, flags=_re.DOTALL)
    impl_code = blocks[0] if len(blocks) >= 1 else None
    test_code = blocks[1] if len(blocks) >= 2 else None

    yield sse_thinking_start(0)
    yield sse_thinking_delta(
        0,
        "## Task: Code (generate + test)\n"
        + (f"Implementation found: {bool(impl_code)} · Tests found: {bool(test_code)}"),
    )
    yield sse_thinking_stop(0)

    if impl_code and test_code:
        # Drop both into same temp dir and run pytest
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "impl.py"), "w") as f:
                # Strip user-provided `from impl import` anchors; we name the file impl.py
                f.write(impl_code)
            with open(os.path.join(tmpdir, "test_impl.py"), "w") as f:
                # Replace "from <anything> import" with "from impl import" if model guessed
                test_rewritten = _re.sub(r"from\s+\w+\s+import", "from impl import", test_code)
                f.write(test_rewritten)
            # Combined runner
            runner = (
                f"import sys, subprocess\n"
                f"sys.path.insert(0, {tmpdir!r})\n"
                f"r = subprocess.run([sys.executable, '-m', 'pytest', '-x', '--tb=short', {os.path.join(tmpdir, 'test_impl.py')!r}],"
                f" capture_output=True, timeout=5)\n"
                f"print(r.stdout.decode()); print('---STDERR---'); print(r.stderr.decode())\n"
                f"sys.exit(r.returncode)\n"
            )
            result = await asyncio.get_event_loop().run_in_executor(None, run_python_sandbox, runner)

        status = "✓ tests pass" if result.ok else ("⚠ timed out" if result.timed_out else "✗ tests failed")
        yield sse_thinking_start(1)
        yield sse_thinking_delta(
            1,
            f"## Test Execution ({status}, {result.duration_ms:.0f}ms)\n"
            f"```\n{result.stdout[:800]}\n```"
            + (f"\nstderr:\n```\n{result.stderr[:400]}\n```" if result.stderr else ""),
        )
        yield sse_thinking_stop(1)

        if not result.ok and not result.timed_out:
            # One refinement pass with actual test output
            refined = await acompletion_with_retry(
                model=model,
                messages=[
                    {"role": "system", "content": REFINE_SYSTEM},
                    {"role": "user", "content": user_text},
                    {"role": "assistant", "content": gen_text},
                    {"role": "user", "content": (
                        f"The generated tests FAILED when executed. Output below — fix the "
                        f"implementation or the tests, whichever is wrong.\n\n{result.stdout[:1500]}\n"
                        f"{result.stderr[:500]}"
                    )},
                ],
                stream=False,
                **draft_kwargs,
            )
            answer = refined.choices[0].message.content or gen_text
            yield sse_thinking_start(2)
            yield sse_thinking_delta(2, "## Refinement (post-test-failure)\nAnswer revised based on actual pytest output.")
            yield sse_thinking_stop(2)
        else:
            answer = gen_text
    else:
        answer = gen_text

    yield sse_text_start(3)
    chunk_size = 50
    for i in range(0, len(answer), chunk_size):
        yield sse_text_delta(3, answer[i:i + chunk_size])
        await asyncio.sleep(0)
    yield sse_text_stop(3)
    input_tokens = sum(len(m.get("content", "")) for m in messages) // 4
    yield sse_message_delta("end_turn", input_tokens, len(answer) // 4)
    yield sse_done()
