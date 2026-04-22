"""
Unit tests for CoT-E pipeline helpers.

These cover the pure functions — no LLM calls are made.
"""
import sys
import types
import pytest
from unittest.mock import patch, MagicMock

# Stub litellm and app.cot.session so the module imports without installed deps
_litellm_stub = types.ModuleType("litellm")
_litellm_stub.acompletion = None  # type: ignore
sys.modules.setdefault("litellm", _litellm_stub)

_session_stub = types.ModuleType("app.cot.session")
_session_stub.get_session_analyses = None  # type: ignore
_session_stub.save_session_analysis = None  # type: ignore
sys.modules.setdefault("app.cot.session", _session_stub)

from app.cot.pipeline import (  # noqa: E402 — must come after stubs
    _should_verify,
    _resolve_verify,
    _parse_score,
    _parse_gaps,
    _last_user_text,
)


# ── _should_verify ────────────────────────────────────────────────────────────

class TestShouldVerify:
    def test_shell_code_block_bash(self):
        answer = "Run this:\n```bash\nsystemctl restart nginx\n```"
        assert _should_verify(answer) is True

    def test_shell_code_block_sh(self):
        answer = "```sh\necho hello\n```"
        assert _should_verify(answer) is True

    def test_shell_code_block_case_insensitive(self):
        assert _should_verify("```BASH\nls\n```") is True
        assert _should_verify("```Shell\nls\n```") is True

    def test_two_infra_tools_triggers(self):
        answer = "Use docker to run the container, then check with systemctl."
        assert _should_verify(answer) is True

    def test_one_infra_tool_does_not_trigger(self):
        answer = "Use docker to run the container."
        assert _should_verify(answer) is False

    def test_zero_infra_tools_no_code_block(self):
        answer = "The answer is to use a list comprehension in Python."
        assert _should_verify(answer) is False

    def test_rabbitmq_and_journalctl(self):
        answer = "Run rabbitmqctl list_queues and check journalctl for errors."
        assert _should_verify(answer) is True

    def test_nginx_and_certbot(self):
        answer = "Configure nginx and then run certbot to get an SSL cert."
        assert _should_verify(answer) is True

    def test_python_code_block_does_not_trigger(self):
        # Python code block, no shell tools — should NOT verify
        answer = "```python\ndef hello():\n    print('hi')\n```"
        assert _should_verify(answer) is False

    def test_curl_counts_as_infra_tool(self):
        # curl - is in the tool set; needs one more to trigger
        answer = "Run curl - to test and also docker ps to check."
        assert _should_verify(answer) is True

    def test_empty_answer(self):
        assert _should_verify("") is False

    def test_conceptual_answer(self):
        answer = (
            "The CAP theorem states that a distributed system can only guarantee "
            "two of the three properties: Consistency, Availability, Partition tolerance."
        )
        assert _should_verify(answer) is False


# ── _resolve_verify ───────────────────────────────────────────────────────────

class TestResolveVerify:
    def _settings(self, enabled=False, auto_detect=True):
        mock = MagicMock()
        mock.cot_verify_enabled = enabled
        mock.cot_verify_auto_detect = auto_detect
        return mock

    def test_force_true_always_verifies(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = False
            s.cot_verify_auto_detect = True
            assert _resolve_verify(True, "any text") is True

    def test_force_false_never_verifies(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = False
            assert _resolve_verify(False, "```bash\nsystemctl status\n```") is False

    def test_none_global_disabled_never_verifies(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = False
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "```bash\nsystemctl status\n```") is False

    def test_none_global_enabled_auto_detect_on_shell_answer(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "```bash\nsystemctl restart nginx\n```") is True

    def test_none_global_enabled_auto_detect_on_plain_answer(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "The answer is 42.") is False

    def test_none_global_enabled_auto_detect_off_always_verifies(self):
        with patch("app.cot.pipeline.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = False
            assert _resolve_verify(None, "The answer is 42.") is True


# ── _parse_score / _parse_gaps ────────────────────────────────────────────────

class TestParsers:
    def test_parse_score_found(self):
        assert _parse_score("SCORE: 7\nGAPS: minor wording") == 7

    def test_parse_score_missing_defaults_five(self):
        assert _parse_score("no score here") == 5

    def test_parse_score_case_insensitive(self):
        assert _parse_score("score: 3") == 3

    def test_parse_gaps_found(self):
        assert _parse_gaps("SCORE: 6\nGAPS: missing error handling") == "missing error handling"

    def test_parse_gaps_none(self):
        assert _parse_gaps("SCORE: 8\nGAPS: none") == "none"

    def test_parse_gaps_missing_returns_empty(self):
        assert _parse_gaps("SCORE: 5") == ""


# ── _last_user_text ───────────────────────────────────────────────────────────

class TestLastUserText:
    def test_simple_string_content(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "what is docker?"},
        ]
        assert _last_user_text(msgs) == "what is docker?"

    def test_multipart_content(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": "explain this"},
            {"type": "image", "source": {}},
        ]}]
        assert _last_user_text(msgs) == "explain this"

    def test_empty_messages(self):
        assert _last_user_text([]) == ""

    def test_no_user_message(self):
        assert _last_user_text([{"role": "assistant", "content": "hi"}]) == ""
