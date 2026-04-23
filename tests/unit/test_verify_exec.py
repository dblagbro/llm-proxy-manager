"""Unit tests for app.cot.verify_exec.parse + grade (no network calls)."""
import sys
import types
import pytest

# Stub litellm so pipeline imports aren't required
sys.modules.setdefault("litellm", types.ModuleType("litellm"))

from app.cot.verify_exec import (
    parse_verify_block, _grade, VerifyStep, render_executed_block, has_failures,
)


class TestParse:
    def test_basic_line(self):
        text = (
            "## Verification Steps\n"
            "1. `docker ps` → container running\n"
            "2. `curl -sI https://example.com` → HTTP/1.1 200 OK\n"
        )
        steps = parse_verify_block(text)
        assert len(steps) == 2
        assert steps[0].command == "docker ps"
        assert steps[0].expected == "container running"
        assert steps[1].command == "curl -sI https://example.com"

    def test_arrow_variants(self):
        text = (
            "1. `a` → x\n"
            "2. `b` -> y\n"
            "3. `c` — z\n"
        )
        steps = parse_verify_block(text)
        assert len(steps) == 3
        assert [s.expected for s in steps] == ["x", "y", "z"]

    def test_empty_text(self):
        assert parse_verify_block("") == []

    def test_not_applicable_no_steps(self):
        text = (
            "## Verification Steps\n"
            "(not applicable — no executable steps in answer)\n"
        )
        assert parse_verify_block(text) == []

    def test_skips_non_matching_lines(self):
        text = "some preamble\n1. `x` → y\nmore prose"
        steps = parse_verify_block(text)
        assert len(steps) == 1


class TestGrade:
    def test_status_code_match_passes(self):
        s = VerifyStep(number=1, command="curl -sI url", expected="HTTP/1.1 200 OK",
                       actual="HTTP 200 OK\nContent-Type: text/html", error="")
        _grade(s)
        assert s.status == "pass"

    def test_quoted_token_match_passes(self):
        s = VerifyStep(number=1, command="docker ps", expected='container "rabbitmq" in output',
                       actual="rabbitmq  3 days up", error="")
        _grade(s)
        assert s.status == "pass"

    def test_mismatch_fails(self):
        s = VerifyStep(number=1, command="curl url", expected="HTTP/1.1 200 OK",
                       actual="HTTP 503 Service Unavailable", error="")
        _grade(s)
        assert s.status == "fail"

    def test_error_only_no_output(self):
        s = VerifyStep(number=1, command="curl url", expected="anything",
                       actual="", error="timed out")
        _grade(s)
        assert s.status == "error"

    def test_substring_fallback_passes(self):
        s = VerifyStep(number=1, command="hostname", expected="tmrwww01",
                       actual="tmrwww01.voipguru.org", error="")
        _grade(s)
        assert s.status == "pass"


class TestRender:
    def test_renders_all_statuses(self):
        steps = [
            VerifyStep(number=1, command="ok", expected="e", actual="e", status="pass", duration_ms=10),
            VerifyStep(number=2, command="fail", expected="e", actual="nope", status="fail", duration_ms=20),
            VerifyStep(number=3, command="docker ps", expected="e", actual="", status="skipped"),
            VerifyStep(number=4, command="err", expected="e", actual="", status="error", error="bad"),
        ]
        out = render_executed_block(steps)
        assert "✓" in out and "✗" in out and "…" in out and "⚠" in out
        assert "not executable in proxy sandbox" in out

    def test_empty(self):
        assert "no verification steps" in render_executed_block([])


class TestFailures:
    def test_detects_failures(self):
        steps = [
            VerifyStep(number=1, command="ok", expected="e", actual="e", status="pass"),
            VerifyStep(number=2, command="f", expected="e", actual="x", status="fail"),
        ]
        assert has_failures(steps) is True

    def test_no_failures(self):
        steps = [
            VerifyStep(number=1, command="ok", expected="e", actual="e", status="pass"),
            VerifyStep(number=2, command="sk", expected="e", status="skipped"),
        ]
        assert has_failures(steps) is False
