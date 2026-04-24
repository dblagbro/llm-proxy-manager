"""Unit tests for SSE primitives, response builders, and usage extraction (app/cot/sse.py)."""
import sys
import types
import json
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.cot.sse import (
    sse_thinking_start, sse_thinking_delta, sse_thinking_stop,
    sse_text_start, sse_text_delta, sse_text_stop,
    sse_message_delta, sse_done,
    anthropic_tool_sse, anthropic_text_sse, anthropic_tools_sse,
    anthropic_tool_response, anthropic_tools_response, anthropic_text_response,
    openai_tool_sse, openai_text_sse, openai_tools_sse,
    openai_tool_response, openai_tools_response, openai_text_response,
    extract_cache_tokens, to_anthropic_response,
)


def _parse_sse(blob: bytes) -> list[dict]:
    """Extract JSON payloads from an SSE byte blob. Skips [DONE]."""
    out = []
    for line in blob.decode().split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        out.append(json.loads(payload))
    return out


async def _collect(agen):
    chunks = []
    async for c in agen:
        chunks.append(c)
    return b"".join(chunks)


# ── Anthropic primitives ─────────────────────────────────────────────────────


class TestAnthropicPrimitives:
    def test_thinking_start_parses(self):
        blob = sse_thinking_start(0)
        events = _parse_sse(blob)
        assert len(events) == 1
        assert events[0]["type"] == "content_block_start"
        assert events[0]["index"] == 0
        assert events[0]["content_block"]["type"] == "thinking"

    def test_thinking_delta_escapes_quotes(self):
        blob = sse_thinking_delta(1, 'he said "hi"')
        events = _parse_sse(blob)
        assert events[0]["delta"]["thinking"] == 'he said "hi"'

    def test_thinking_delta_escapes_newlines(self):
        blob = sse_thinking_delta(0, "line1\nline2")
        events = _parse_sse(blob)
        assert events[0]["delta"]["thinking"] == "line1\nline2"

    def test_text_delta_escapes_backslash(self):
        blob = sse_text_delta(0, "path\\to\\file")
        events = _parse_sse(blob)
        assert events[0]["delta"]["text"] == "path\\to\\file"

    def test_text_stop_has_index(self):
        blob = sse_text_stop(2)
        events = _parse_sse(blob)
        assert events[0]["type"] == "content_block_stop"
        assert events[0]["index"] == 2

    def test_message_delta_has_stop_and_usage(self):
        blob = sse_message_delta("end_turn", 10, 20)
        events = _parse_sse(blob)
        assert events[0]["delta"]["stop_reason"] == "end_turn"
        assert events[0]["usage"]["input_tokens"] == 10
        assert events[0]["usage"]["output_tokens"] == 20

    def test_done_emits_both_message_stop_and_DONE(self):
        blob = sse_done()
        assert b'"type":"message_stop"' in blob
        assert b"[DONE]" in blob


# ── Anthropic response builders ──────────────────────────────────────────────


class TestAnthropicResponseBuilders:
    def test_tool_response_structure(self):
        r = anthropic_tool_response("get_weather", {"city": "Paris"}, "claude-sonnet-4")
        assert r["type"] == "message"
        assert r["role"] == "assistant"
        assert r["stop_reason"] == "tool_use"
        assert r["model"] == "claude-sonnet-4"
        assert r["content"][0]["type"] == "tool_use"
        assert r["content"][0]["name"] == "get_weather"
        assert r["content"][0]["input"] == {"city": "Paris"}

    def test_tool_response_unique_ids(self):
        a = anthropic_tool_response("f", {}, "m")
        b = anthropic_tool_response("f", {}, "m")
        assert a["id"] != b["id"]

    def test_tools_response_multiple_blocks(self):
        tcs = [
            {"name": "tool_a", "input": {"x": 1}},
            {"name": "tool_b", "input": {"y": 2}},
            {"name": "tool_c", "input": {}},
        ]
        r = anthropic_tools_response(tcs, "claude-sonnet-4")
        assert len(r["content"]) == 3
        assert [c["name"] for c in r["content"]] == ["tool_a", "tool_b", "tool_c"]
        assert r["stop_reason"] == "tool_use"

    def test_text_response_structure(self):
        r = anthropic_text_response("hello world", "claude-haiku")
        assert r["content"][0]["type"] == "text"
        assert r["content"][0]["text"] == "hello world"
        assert r["stop_reason"] == "end_turn"


# ── Anthropic SSE generators ─────────────────────────────────────────────────


class TestAnthropicSSEGenerators:
    @pytest.mark.asyncio
    async def test_tool_sse_contains_tool_use(self):
        blob = await _collect(anthropic_tool_sse("f", {"a": 1}))
        events = _parse_sse(blob)
        types_ = [e["type"] for e in events]
        assert "content_block_start" in types_
        assert "content_block_delta" in types_
        assert "content_block_stop" in types_
        # Final events include message_delta and message_stop
        assert any(e["type"] == "message_delta" and e["delta"]["stop_reason"] == "tool_use" for e in events)
        assert any(e["type"] == "message_stop" for e in events)

    @pytest.mark.asyncio
    async def test_text_sse_chunks_long_text(self):
        long_text = "x" * 250  # will be chunked at 80
        blob = await _collect(anthropic_text_sse(long_text))
        events = _parse_sse(blob)
        deltas = [e for e in events if e["type"] == "content_block_delta"]
        # 250 / 80 = 4 chunks (80, 80, 80, 10)
        assert len(deltas) == 4
        joined = "".join(d["delta"]["text"] for d in deltas)
        assert joined == long_text

    @pytest.mark.asyncio
    async def test_tools_sse_emits_one_block_per_tool(self):
        tcs = [{"name": "a", "input": {}}, {"name": "b", "input": {}}]
        blob = await _collect(anthropic_tools_sse(tcs))
        events = _parse_sse(blob)
        starts = [e for e in events if e["type"] == "content_block_start"]
        assert len(starts) == 2
        assert starts[0]["index"] == 0
        assert starts[1]["index"] == 1


# ── OpenAI response builders ─────────────────────────────────────────────────


class TestOpenAIResponseBuilders:
    def test_tool_response_has_function_call(self):
        r = openai_tool_response("get_weather", {"city": "Paris"}, "gpt-4o")
        assert r["object"] == "chat.completion"
        assert r["model"] == "gpt-4o"
        choice = r["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        tc = choice["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "get_weather"
        assert json.loads(tc["function"]["arguments"]) == {"city": "Paris"}

    def test_tools_response_multiple_calls(self):
        tcs = [
            {"name": "a", "input": {"x": 1}},
            {"name": "b", "input": {"y": 2}},
        ]
        r = openai_tools_response(tcs, "gpt-4o")
        calls = r["choices"][0]["message"]["tool_calls"]
        assert len(calls) == 2
        assert calls[0]["function"]["name"] == "a"
        assert calls[1]["function"]["name"] == "b"

    def test_text_response_structure(self):
        r = openai_text_response("hello", "gpt-4o-mini")
        choice = r["choices"][0]
        assert choice["finish_reason"] == "stop"
        assert choice["message"]["content"] == "hello"
        assert choice["message"]["role"] == "assistant"


# ── OpenAI SSE generators ────────────────────────────────────────────────────


class TestOpenAISSEGenerators:
    @pytest.mark.asyncio
    async def test_tool_sse_ends_with_done(self):
        blob = await _collect(openai_tool_sse("f", {"a": 1}))
        assert blob.endswith(b"data: [DONE]\n\n")

    @pytest.mark.asyncio
    async def test_tool_sse_finish_reason_tool_calls(self):
        blob = await _collect(openai_tool_sse("f", {}))
        events = _parse_sse(blob)
        final = [e for e in events if e["choices"][0].get("finish_reason") == "tool_calls"]
        assert len(final) == 1

    @pytest.mark.asyncio
    async def test_text_sse_emits_role_then_content(self):
        blob = await _collect(openai_text_sse("hello"))
        events = _parse_sse(blob)
        # First delta sets role
        assert events[0]["choices"][0]["delta"].get("role") == "assistant"
        # Last real chunk has finish_reason=stop
        assert events[-1]["choices"][0]["finish_reason"] == "stop"

    @pytest.mark.asyncio
    async def test_tools_sse_first_chunk_has_call_headers(self):
        tcs = [
            {"name": "a", "input": {"x": 1}},
            {"name": "b", "input": {"y": 2}},
        ]
        blob = await _collect(openai_tools_sse(tcs))
        events = _parse_sse(blob)
        first = events[0]
        headers = first["choices"][0]["delta"]["tool_calls"]
        assert len(headers) == 2
        assert headers[0]["function"]["name"] == "a"
        assert headers[1]["function"]["name"] == "b"


# ── extract_cache_tokens ─────────────────────────────────────────────────────


class _UsageAnthropicStyle:
    def __init__(self, creation=0, read=0):
        self.cache_creation_input_tokens = creation
        self.cache_read_input_tokens = read


class _UsageOpenAIStyle:
    class _Details:
        def __init__(self, cached):
            self.cached_tokens = cached

    def __init__(self, cached):
        self.prompt_tokens_details = _UsageOpenAIStyle._Details(cached)


class TestExtractCacheTokens:
    def test_none_returns_zero(self):
        assert extract_cache_tokens(None) == (0, 0)

    def test_anthropic_style(self):
        u = _UsageAnthropicStyle(creation=100, read=50)
        assert extract_cache_tokens(u) == (100, 50)

    def test_openai_style_reads_cached(self):
        u = _UsageOpenAIStyle(cached=75)
        assert extract_cache_tokens(u) == (0, 75)

    def test_anthropic_read_takes_precedence_over_openai(self):
        """If both styles present on same obj, anthropic read wins."""
        u = _UsageAnthropicStyle(creation=10, read=20)
        u.prompt_tokens_details = _UsageOpenAIStyle._Details(99)
        assert extract_cache_tokens(u) == (10, 20)


# ── to_anthropic_response ────────────────────────────────────────────────────


class _FakeUsage:
    prompt_tokens = 10
    completion_tokens = 20
    cache_creation_input_tokens = 0
    cache_read_input_tokens = 0


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeChoice:
    def __init__(self, message, finish_reason="stop"):
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, choice, model="gpt-4o", id="chatcmpl-1", usage=None):
        self.choices = [choice]
        self.model = model
        self.id = id
        self.usage = usage or _FakeUsage()


class _FakeFn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, name, args_json, id="call_1"):
        self.function = _FakeFn(name, args_json)
        self.id = id


class TestToAnthropicResponse:
    def test_simple_text(self):
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="hello"), "stop"))
        out = to_anthropic_response(resp)
        assert out["type"] == "message"
        assert out["role"] == "assistant"
        assert out["content"][0]["text"] == "hello"
        assert out["stop_reason"] == "end_turn"
        assert out["usage"]["input_tokens"] == 10
        assert out["usage"]["output_tokens"] == 20

    def test_length_finish_maps_to_max_tokens(self):
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="x"), "length"))
        out = to_anthropic_response(resp)
        assert out["stop_reason"] == "max_tokens"

    def test_tool_call_finish_maps_to_tool_use(self):
        tc = _FakeToolCall("f", '{"a": 1}')
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content=None, tool_calls=[tc]), "tool_calls"))
        out = to_anthropic_response(resp)
        assert out["stop_reason"] == "tool_use"
        tu = [c for c in out["content"] if c["type"] == "tool_use"][0]
        assert tu["name"] == "f"
        assert tu["input"] == {"a": 1}

    def test_malformed_tool_args_become_empty_dict(self):
        tc = _FakeToolCall("f", "not valid json {")
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content=None, tool_calls=[tc]), "tool_calls"))
        out = to_anthropic_response(resp)
        tu = [c for c in out["content"] if c["type"] == "tool_use"][0]
        assert tu["input"] == {}

    def test_empty_content_becomes_empty_text_block(self):
        """If a response has no content AND no tool_calls, emit an empty text block."""
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content=None), "stop"))
        out = to_anthropic_response(resp)
        assert out["content"] == [{"type": "text", "text": ""}]

    def test_text_plus_tool_call_both_present(self):
        tc = _FakeToolCall("f", '{}')
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="thinking...", tool_calls=[tc]), "tool_calls"))
        out = to_anthropic_response(resp)
        types_ = [c["type"] for c in out["content"]]
        assert "text" in types_
        assert "tool_use" in types_

    def test_unknown_finish_defaults_to_end_turn(self):
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="x"), "mystery_reason"))
        out = to_anthropic_response(resp)
        assert out["stop_reason"] == "end_turn"

    def test_cache_tokens_flow_into_usage(self):
        u = _FakeUsage()
        u.cache_creation_input_tokens = 5
        u.cache_read_input_tokens = 7
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="x"), "stop"), usage=u)
        out = to_anthropic_response(resp)
        assert out["usage"]["cache_creation_input_tokens"] == 5
        assert out["usage"]["cache_read_input_tokens"] == 7

    def test_zero_cache_tokens_omitted_from_usage(self):
        resp = _FakeResponse(_FakeChoice(_FakeMessage(content="x"), "stop"))
        out = to_anthropic_response(resp)
        assert "cache_creation_input_tokens" not in out["usage"]
        assert "cache_read_input_tokens" not in out["usage"]
