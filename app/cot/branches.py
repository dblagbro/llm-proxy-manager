"""
Task-adaptive branches for the CoT-E pipeline (Wave 2 #11).

Each branch is an async generator yielding raw SSE bytes for its own
complete response. ``run_cot_pipeline`` in ``app/cot/pipeline.py``
delegates to one of these when a ``task_branch`` hint is present.

Extracted from ``pipeline.py`` in the 2026-04-23 refactor; the logic
and public callable names are unchanged.
"""
from __future__ import annotations

import asyncio
import re as _re
from typing import AsyncIterator

from app.routing.retry import acompletion_with_retry
from app.cot.sse import (
    sse_thinking_start, sse_thinking_delta, sse_thinking_stop,
    sse_text_start, sse_text_delta, sse_text_stop,
    sse_message_delta, sse_done,
)


# Refine system prompt duplicated here to keep branches.py self-contained;
# pipeline.py owns the authoritative copy. Keeping a local alias avoids
# a circular import when pipeline imports us.
_REFINE_SYSTEM = (
    "You are an expert assistant. A draft response has been critiqued. "
    "Produce an improved, complete answer addressing the identified gaps."
)


def _chunk_text_sse(text: str, block_index: int) -> list:
    """Unused helper (kept for future DRY'ing); callers currently inline the loop."""
    return []  # pragma: no cover


async def run_summarize_branch(
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
    yield sse_thinking_delta(
        0, "## Task: Summarize (single-pass)\nSkipping plan/critique/refine for summarization tasks."
    )
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


async def run_math_branch(
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


async def run_code_branch(
    model: str, messages: list[dict], user_text: str, extra_kwargs: dict,
) -> AsyncIterator[bytes]:
    """task=code — generate implementation + tests, run tests, refine on failure."""
    from app.cot.task_adaptive import (
        CODEGEN_TESTS_SYSTEM, extract_python, run_python_sandbox,
    )
    draft_kwargs = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system")}

    gen = await acompletion_with_retry(
        model=model,
        messages=[{"role": "system", "content": CODEGEN_TESTS_SYSTEM}] + messages,
        stream=False,
        **draft_kwargs,
    )
    gen_text = gen.choices[0].message.content or ""

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
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "impl.py"), "w") as f:
                f.write(impl_code)
            with open(os.path.join(tmpdir, "test_impl.py"), "w") as f:
                test_rewritten = _re.sub(r"from\s+\w+\s+import", "from impl import", test_code)
                f.write(test_rewritten)
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
            refined = await acompletion_with_retry(
                model=model,
                messages=[
                    {"role": "system", "content": _REFINE_SYSTEM},
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
