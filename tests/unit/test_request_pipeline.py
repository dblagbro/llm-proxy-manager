"""Unit tests for the shared request-pipeline helpers."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from fastapi import HTTPException

from app.api._request_pipeline import (
    apply_privacy_filters,
    build_hint_with_auto_task,
    build_base_response_headers,
    _extract_last_user_text,
)


# ── _extract_last_user_text ──────────────────────────────────────────────────


class TestExtractLastUserText:
    def test_empty(self):
        assert _extract_last_user_text([]) == ""

    def test_no_user_messages(self):
        msgs = [{"role": "assistant", "content": "hi"}]
        assert _extract_last_user_text(msgs) == ""

    def test_string_content(self):
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ack"},
            {"role": "user", "content": "second"},
        ]
        assert _extract_last_user_text(msgs) == "second"

    def test_list_content_text_blocks_joined(self):
        msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "line1"},
                {"type": "image", "source": {"data": "..."}},
                {"type": "text", "text": "line2"},
            ],
        }]
        assert _extract_last_user_text(msgs) == "line1\nline2"

    def test_scans_backward_to_most_recent_user(self):
        msgs = [
            {"role": "user", "content": "oldest"},
            {"role": "user", "content": "newer"},
            {"role": "user", "content": "newest"},
        ]
        assert _extract_last_user_text(msgs) == "newest"


# ── apply_privacy_filters ────────────────────────────────────────────────────


class TestApplyPrivacyFilters:
    def test_both_disabled_is_pass_through(self, monkeypatch):
        import app.privacy.prompt_guard as g
        import app.privacy.pii as p
        monkeypatch.setattr(g, "is_enabled", lambda: False)
        monkeypatch.setattr(p, "is_enabled", lambda: False)

        body = {"messages": [{"role": "user", "content": "hello"}]}
        out_msgs, count = apply_privacy_filters(body["messages"], body)
        assert count == 0
        assert out_msgs == [{"role": "user", "content": "hello"}]

    def test_guard_match_raises_400(self, monkeypatch):
        import app.privacy.prompt_guard as g
        import app.privacy.pii as p
        monkeypatch.setattr(g, "is_enabled", lambda: True)
        monkeypatch.setattr(g, "check_messages", lambda m, denylist=None: "banned")
        monkeypatch.setattr(p, "is_enabled", lambda: False)

        body = {"messages": [{"role": "user", "content": "banned"}]}
        with pytest.raises(HTTPException) as exc_info:
            apply_privacy_filters(body["messages"], body)
        assert exc_info.value.status_code == 400
        assert "banned" in exc_info.value.detail

    def test_pii_rewrites_body_messages(self, monkeypatch):
        import app.privacy.prompt_guard as g
        import app.privacy.pii as p
        monkeypatch.setattr(g, "is_enabled", lambda: False)
        monkeypatch.setattr(p, "is_enabled", lambda: True)
        monkeypatch.setattr(p, "mask_messages", lambda m: ([{"role": "user", "content": "[masked]"}], 3))

        body = {"messages": [{"role": "user", "content": "email@x.com"}]}
        out_msgs, count = apply_privacy_filters(body["messages"], body)
        assert count == 3
        assert body["messages"] == out_msgs  # in-place body update
        assert out_msgs[0]["content"] == "[masked]"

    def test_guard_runs_before_pii(self, monkeypatch):
        """Guard must match original text, not the PII-redacted version."""
        import app.privacy.prompt_guard as g
        import app.privacy.pii as p

        calls = []

        def _guard_check(msgs, denylist=None):
            calls.append("guard")
            return None

        def _pii_mask(msgs):
            calls.append("pii")
            return msgs, 0

        monkeypatch.setattr(g, "is_enabled", lambda: True)
        monkeypatch.setattr(g, "check_messages", _guard_check)
        monkeypatch.setattr(p, "is_enabled", lambda: True)
        monkeypatch.setattr(p, "mask_messages", _pii_mask)

        apply_privacy_filters([{"role": "user", "content": "x"}], {})
        assert calls == ["guard", "pii"]


# ── build_hint_with_auto_task ────────────────────────────────────────────────


class TestBuildHintWithAutoTask:
    @pytest.mark.asyncio
    async def test_passthrough_when_classifier_disabled(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "task_auto_detect_enabled", False)
        hint, auto_task = await build_hint_with_auto_task("cost=economy", [])
        assert auto_task is None
        assert hint is not None
        assert hint.get("cost").value == "economy"

    @pytest.mark.asyncio
    async def test_no_override_when_task_already_set(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "task_auto_detect_enabled", True)
        hint, auto_task = await build_hint_with_auto_task("task=code", [])
        assert auto_task is None

    @pytest.mark.asyncio
    async def test_classifier_appends_task_dim(self, monkeypatch):
        from app.config import settings
        import app.routing.classifier as cls_mod
        monkeypatch.setattr(settings, "task_auto_detect_enabled", True)

        async def _classify(text, model, dims):
            return ("reasoning", 0.88)

        monkeypatch.setattr(cls_mod, "classify", _classify)

        msgs = [{"role": "user", "content": "think deeply"}]
        hint, auto_task = await build_hint_with_auto_task(None, msgs)
        assert auto_task == "reasoning"
        assert hint is not None
        assert any(d.key == "task" and d.value == "reasoning" for d in hint.dimensions)

    @pytest.mark.asyncio
    async def test_classifier_noop_when_no_user_text(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "task_auto_detect_enabled", True)
        hint, auto_task = await build_hint_with_auto_task(None, [])
        assert auto_task is None
        assert hint is None


# ── build_base_response_headers ──────────────────────────────────────────────


class _FakeRoute:
    class _Provider:
        name = "MyProv"
        provider_type = "anthropic"

    class _Profile:
        context_length = 200_000
        provider_type = "anthropic"

    def __init__(self, cot=False, tool_emu=False, vis=False):
        self.provider = _FakeRoute._Provider()
        self.profile = _FakeRoute._Profile()
        self.capability_header = "v=1, provider=p, model=m"
        self.litellm_model = "anthropic/claude-sonnet-4"
        self.cot_engaged = cot
        self.tool_emulation_engaged = tool_emu
        self.vision_stripped = vis
        self.unmet_hints = []


class TestBuildBaseResponseHeaders:
    def test_minimal_emulation_level(self):
        r = _FakeRoute()
        h = build_base_response_headers(
            route=r, auto_task=None, vision_routed_count=0,
            context_strategy_applied=None, pii_masked_count=0, hint=None,
        )
        assert h["X-Emulation-Level"] == "minimal"
        assert h["X-Provider"] == "MyProv"
        assert h["X-Resolved-Provider"] == "anthropic"
        assert h["X-Resolved-Model"] == "anthropic/claude-sonnet-4"
        assert "X-Token-Budget-Remaining" not in h

    def test_standard_emulation_on_tool_emu(self):
        r = _FakeRoute(tool_emu=True)
        h = build_base_response_headers(
            route=r, auto_task=None, vision_routed_count=0,
            context_strategy_applied=None, pii_masked_count=0, hint=None,
        )
        assert h["X-Emulation-Level"] == "standard"

    def test_enhanced_emulation_on_cot(self):
        r = _FakeRoute(cot=True)
        h = build_base_response_headers(
            route=r, auto_task=None, vision_routed_count=0,
            context_strategy_applied=None, pii_masked_count=0, hint=None,
        )
        assert h["X-Emulation-Level"] == "enhanced"

    def test_optional_headers_omitted_when_zero(self):
        r = _FakeRoute()
        h = build_base_response_headers(
            route=r, auto_task=None, vision_routed_count=0,
            context_strategy_applied=None, pii_masked_count=0, hint=None,
        )
        assert "X-Task-Auto-Detected" not in h
        assert "X-Vision-Routed" not in h
        assert "X-Context-Strategy-Applied" not in h
        assert "X-PII-Masked" not in h
        assert "LLM-Hint-Set" not in h

    def test_optional_headers_set_when_provided(self):
        r = _FakeRoute()
        h = build_base_response_headers(
            route=r, auto_task="code",
            vision_routed_count=2, context_strategy_applied="truncate:4dropped",
            pii_masked_count=5, hint=None,
        )
        assert h["X-Task-Auto-Detected"] == "code"
        assert h["X-Vision-Routed"] == "2"
        assert h["X-Context-Strategy-Applied"] == "truncate:4dropped"
        assert h["X-PII-Masked"] == "5"

    def test_max_tokens_emits_budget_header(self):
        r = _FakeRoute()
        h = build_base_response_headers(
            route=r, auto_task=None, vision_routed_count=0,
            context_strategy_applied=None, pii_masked_count=0, hint=None,
            max_tokens=1024,
        )
        assert h["X-Token-Budget-Remaining"] == "1024"
