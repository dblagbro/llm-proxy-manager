"""Task-adaptive CoT branches (Wave 2 #11).

Three non-default pipelines, selected by the LMRH task= hint dimension:

    task=math       → Program-of-Thought (generate Python → execute → report)
    task=code       → generate + tests + run → refine on failures
    task=summarize  → single-pass (skip plan + critique loop)

All code execution uses a restricted Python subprocess with:
- --isolated Python flag (no site-packages, no user customizations)
- resource.setrlimit for CPU (3s) + AS memory (256 MB) before exec
- subprocess.run with timeout=5
- stdin closed, stdout/stderr captured, size-limited

Since the proxy container only has python3 + stdlib, generated code can
use math/statistics/decimal/fractions/datetime/re/json/etc — no numpy,
no pandas. This matches what math-word-problem benchmarks need.
"""
from __future__ import annotations

import logging
import os
import re
import resource
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

_PYCODE_RE = re.compile(r"```(?:python|py)\s*\n(?P<code>.*?)\n```", re.DOTALL)


@dataclass
class PyExecResult:
    stdout: str
    stderr: str
    returncode: int
    timed_out: bool
    duration_ms: float

    @property
    def ok(self) -> bool:
        return not self.timed_out and self.returncode == 0


def extract_python(text: str) -> Optional[str]:
    """Pull the first ```python …``` fenced block out of a response."""
    m = _PYCODE_RE.search(text)
    return m.group("code") if m else None


def run_python_sandbox(code: str, timeout_sec: float = 5.0, max_memory_mb: int = 256) -> PyExecResult:
    """Execute `code` in an isolated python3 -I subprocess with resource caps."""
    import time

    def _set_limits():
        cpu = int(timeout_sec) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        mem = max_memory_mb * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        except (ValueError, OSError):
            pass  # some container configs reject AS limits

    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write(code)
        path = f.name

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-B", path],
            input=b"",
            capture_output=True,
            timeout=timeout_sec,
            preexec_fn=_set_limits if hasattr(os, "fork") else None,
            env={"PATH": "/usr/bin:/usr/local/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )
        duration_ms = (time.monotonic() - t0) * 1000
        return PyExecResult(
            stdout=proc.stdout.decode("utf-8", errors="replace")[:4000],
            stderr=proc.stderr.decode("utf-8", errors="replace")[:2000],
            returncode=proc.returncode,
            timed_out=False,
            duration_ms=duration_ms,
        )
    except subprocess.TimeoutExpired as e:
        duration_ms = (time.monotonic() - t0) * 1000
        return PyExecResult(
            stdout=(e.stdout or b"").decode("utf-8", errors="replace")[:4000] if e.stdout else "",
            stderr=(e.stderr or b"").decode("utf-8", errors="replace")[:2000] if e.stderr else "timeout",
            returncode=-1,
            timed_out=True,
            duration_ms=duration_ms,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# ── Prompts ──────────────────────────────────────────────────────────────────

POT_SYSTEM = textwrap.dedent("""
    You are a reasoning assistant. The user's question is quantitative — solve it
    by writing a SHORT Python program that computes the answer, not by reasoning
    verbally. Your response MUST contain exactly one ```python ... ``` fenced
    block that:

    - Uses only the Python stdlib (math, statistics, decimal, fractions, datetime).
    - Prints ONLY the final answer to stdout on the last line.
    - Is runnable as-is with `python3 program.py`.

    After the code block, add ONE short line of natural-language explanation.
""").strip()

POT_REPORT_SYSTEM = textwrap.dedent("""
    You computed a Python program to answer the user's question. Its execution
    output is below. Produce the final user-facing answer, incorporating the
    computed result naturally. Do NOT print the code unless the user asked for it.
""").strip()

CODEGEN_TESTS_SYSTEM = textwrap.dedent("""
    You are a careful software engineer. Produce an implementation for the user's
    request AND a minimal pytest test file that verifies the implementation.
    Output:

    ```python
    # implementation
    <code>
    ```

    ```python
    # tests (pytest)
    <code that imports the implementation and asserts behavior>
    ```

    The test must be runnable as `python -m pytest <path>` — stick to Python stdlib.
""").strip()

SUMMARIZE_SYSTEM = textwrap.dedent("""
    You are a precise summarization assistant. Produce the summary the user asked
    for in one pass. Do not add preamble, do not explain your reasoning.
""").strip()


def select_task_branch(lmrh_task: Optional[str]) -> Optional[str]:
    """Map a raw LMRH task hint to a known branch name, else None."""
    if not lmrh_task:
        return None
    t = lmrh_task.lower().strip()
    if t in ("math", "calculation", "arithmetic"):
        return "math"
    if t in ("code", "coding", "programming"):
        return "code"
    if t == "summarize":
        return "summarize"
    return None
