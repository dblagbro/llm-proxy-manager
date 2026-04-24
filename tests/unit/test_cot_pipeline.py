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
_litellm_stub.RateLimitError = type("RateLimitError", (Exception,), {})  # type: ignore
sys.modules.setdefault("litellm", _litellm_stub)

_session_stub = types.ModuleType("app.cot.session")
_session_stub.get_session_analyses = None  # type: ignore
_session_stub.save_session_analysis = None  # type: ignore
sys.modules.setdefault("app.cot.session", _session_stub)

# Stub app.routing.retry (pipeline imports it; CB not available in unit test context)
_retry_stub = types.ModuleType("app.routing.retry")
async def _acompletion_with_retry(model, messages, **kwargs):
    import litellm
    return await litellm.acompletion(model=model, messages=messages, **kwargs)
_retry_stub.acompletion_with_retry = _acompletion_with_retry  # type: ignore
sys.modules.setdefault("app.routing.retry", _retry_stub)

from app.cot.pipeline import (  # noqa: E402 — must come after stubs
    _should_verify,
    _resolve_verify,
    _parse_score,
    _parse_gaps,
    _parse_critique,
    _last_user_text,
    parse_cot_request_headers,
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
        with patch("app.cot.verify.settings") as s:
            s.cot_verify_enabled = False
            s.cot_verify_auto_detect = True
            assert _resolve_verify(True, "any text") is True

    def test_force_false_never_verifies(self):
        with patch("app.cot.verify.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = False
            assert _resolve_verify(False, "```bash\nsystemctl status\n```") is False

    def test_none_global_disabled_never_verifies(self):
        with patch("app.cot.verify.settings") as s:
            s.cot_verify_enabled = False
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "```bash\nsystemctl status\n```") is False

    def test_none_global_enabled_auto_detect_on_shell_answer(self):
        with patch("app.cot.verify.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "```bash\nsystemctl restart nginx\n```") is True

    def test_none_global_enabled_auto_detect_on_plain_answer(self):
        with patch("app.cot.verify.settings") as s:
            s.cot_verify_enabled = True
            s.cot_verify_auto_detect = True
            assert _resolve_verify(None, "The answer is 42.") is False

    def test_none_global_enabled_auto_detect_off_always_verifies(self):
        with patch("app.cot.verify.settings") as s:
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


# ── _parse_critique (Wave 2 #7 — structured JSON rubric) ─────────────────────

class TestParseCritique:
    def test_clean_json(self):
        r = _parse_critique('{"factual_issues":[],"missing_coverage":[],"sufficient_for_user":true}')
        assert r == {"factual_issues": [], "missing_coverage": [], "sufficient_for_user": True}

    def test_json_with_issues(self):
        r = _parse_critique(
            '{"factual_issues":["wrong port","bad command"],'
            '"missing_coverage":["docker compose"],'
            '"sufficient_for_user":false}'
        )
        assert r["factual_issues"] == ["wrong port", "bad command"]
        assert r["missing_coverage"] == ["docker compose"]
        assert r["sufficient_for_user"] is False

    def test_json_with_markdown_fences(self):
        r = _parse_critique('```json\n{"factual_issues":[],"missing_coverage":[],"sufficient_for_user":true}\n```')
        assert r["sufficient_for_user"] is True

    def test_json_with_surrounding_prose(self):
        r = _parse_critique('Here is my review:\n{"factual_issues":["x"],"missing_coverage":[],"sufficient_for_user":false}\nHope that helps!')
        assert r["factual_issues"] == ["x"]
        assert r["sufficient_for_user"] is False

    def test_fallback_legacy_score_high_means_sufficient(self):
        r = _parse_critique("SCORE: 9\nGAPS: none")
        assert r["sufficient_for_user"] is True
        assert r["factual_issues"] == []
        assert r["missing_coverage"] == []

    def test_fallback_legacy_score_low_not_sufficient(self):
        r = _parse_critique("SCORE: 4\nGAPS: missing steps")
        assert r["sufficient_for_user"] is False
        assert r["missing_coverage"] == ["missing steps"]

    def test_empty_string(self):
        r = _parse_critique("")
        assert r["sufficient_for_user"] in (True, False)
        assert isinstance(r["factual_issues"], list)

    def test_malformed_json_falls_back_safely(self):
        r = _parse_critique('{"factual_issues": [oops broken')
        # Should fall back to legacy parser path, which yields defaults for pure garbage
        assert isinstance(r["sufficient_for_user"], bool)
        assert isinstance(r["factual_issues"], list)


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


# ── parse_cot_request_headers (Wave 2 #10 — self-consistency) ────────────────

class TestParseCotHeaders:
    def test_defaults(self):
        cot_max, force, samples = parse_cot_request_headers(None, None)
        assert cot_max is None
        assert force is None
        assert samples == 1

    def test_iterations_and_verify(self):
        cot_max, force, samples = parse_cot_request_headers("2", "true")
        assert cot_max == 2
        assert force is True
        assert samples == 1

    def test_samples_header(self):
        cot_max, force, samples = parse_cot_request_headers(None, None, "5", None)
        assert samples == 5

    def test_samples_clamp_max(self):
        _, _, samples = parse_cot_request_headers(None, None, "99", None)
        assert samples == 10

    def test_samples_clamp_min(self):
        _, _, samples = parse_cot_request_headers(None, None, "0", None)
        assert samples == 1

    def test_mode_alias(self):
        _, _, samples = parse_cot_request_headers(None, None, None, "self-consistency")
        assert samples == 3

    def test_explicit_samples_overrides_mode(self):
        _, _, samples = parse_cot_request_headers(None, None, "7", "self-consistency")
        assert samples == 7

    def test_invalid_samples_falls_back(self):
        _, _, samples = parse_cot_request_headers(None, None, "nope", None)
        assert samples == 1

    def test_iterations_zero_allowed(self):
        cot_max, _, _ = parse_cot_request_headers("0", None)
        assert cot_max == 0
