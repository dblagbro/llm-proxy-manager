"""Unit tests for task-adaptive branch selection + Python code extraction
and the sandboxed Python runner (Wave 2 #11)."""
import sys
import types
import pytest

sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.cot.task_adaptive import (
    select_task_branch, extract_python, run_python_sandbox,
)


class TestSelectTaskBranch:
    def test_math_aliases(self):
        assert select_task_branch("math") == "math"
        assert select_task_branch("calculation") == "math"
        assert select_task_branch("arithmetic") == "math"

    def test_code_aliases(self):
        assert select_task_branch("code") == "code"
        assert select_task_branch("coding") == "code"
        assert select_task_branch("programming") == "code"

    def test_summarize(self):
        assert select_task_branch("summarize") == "summarize"

    def test_unknown_returns_none(self):
        assert select_task_branch("reasoning") is None
        assert select_task_branch("chat") is None
        assert select_task_branch("") is None
        assert select_task_branch(None) is None

    def test_case_insensitive(self):
        assert select_task_branch("MATH") == "math"
        assert select_task_branch("  Code  ") == "code"


class TestExtractPython:
    def test_fenced_python(self):
        assert extract_python("```python\nprint(1)\n```") == "print(1)"

    def test_fenced_py(self):
        assert extract_python("```py\nx=1\n```") == "x=1"

    def test_multiline(self):
        text = "Some prose\n```python\nx = 1\ny = 2\nprint(x+y)\n```\nafter"
        assert extract_python(text) == "x = 1\ny = 2\nprint(x+y)"

    def test_no_code_returns_none(self):
        assert extract_python("just text") is None
        assert extract_python("```bash\necho hi\n```") is None


class TestRunPythonSandbox:
    def test_simple_math(self):
        r = run_python_sandbox("print(6*7)")
        assert r.ok
        assert r.stdout.strip() == "42"

    def test_stderr_on_error(self):
        r = run_python_sandbox("1/0")
        assert not r.ok
        assert r.returncode != 0
        assert "ZeroDivisionError" in r.stderr

    def test_timeout(self):
        r = run_python_sandbox("import time; time.sleep(10)", timeout_sec=1.0)
        assert r.timed_out
        assert r.returncode == -1

    def test_stdlib_imports_work(self):
        r = run_python_sandbox("import math, statistics; print(math.sqrt(9))")
        assert r.ok
        assert "3.0" in r.stdout

    def test_output_truncation(self):
        # Generate far more than 4000 chars
        r = run_python_sandbox("print('x' * 10000)")
        assert r.ok
        assert len(r.stdout) <= 4100  # 4000 + a bit for newlines
