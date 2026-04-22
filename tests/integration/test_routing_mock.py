"""
Layer 1 — Mock-based routing tests.
All tests force-trip real provider CBs so only the mock is reachable.
This validates proxy mechanics (format conversion, streaming, tool use,
tool emulation, CoT-E) without spending real API credits.
"""
import json
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL
from tests.integration.conftest import collect_sse

# ── helpers ──────────────────────────────────────────────────────────────────

SIMPLE_ANTHROPIC = {
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Say exactly: hello world"}],
}

SIMPLE_OPENAI = {
    "model": "gpt-4o",
    "max_tokens": 50,
    "messages": [{"role": "user", "content": "Say exactly: hello world"}],
}

TOOL_DEF = {
    "name": "read_file",
    "description": "Read a file from disk",
    "input_schema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path"}},
        "required": ["path"],
    },
}

TOOL_DEF_OPENAI = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "Read a file from disk",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
}


def post_messages(headers, body, stream=False):
    return requests.post(
        f"{BASE_URL}/v1/messages",
        headers=headers,
        json={**body, "stream": stream},
        stream=stream,
        verify=False,
        timeout=30,
    )


def post_completions(headers, body, stream=False):
    return requests.post(
        f"{BASE_URL}/v1/chat/completions",
        headers=headers,
        json={**body, "stream": stream},
        stream=stream,
        verify=False,
        timeout=30,
    )


# ── Anthropic /v1/messages — non-streaming ────────────────────────────────────

class TestAnthropicNonStream:
    def test_basic_text_response_shape(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC)
        assert r.status_code == 200
        d = r.json()
        assert d["type"] == "message"
        assert d["role"] == "assistant"
        assert isinstance(d["content"], list)
        assert any(b["type"] == "text" and b["text"] for b in d["content"])
        assert d["stop_reason"] in ("end_turn", "max_tokens", "stop_sequence", "tool_use")
        assert "usage" in d
        assert d["usage"]["output_tokens"] > 0

    def test_response_contains_mock_text(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC)
        text = " ".join(b["text"] for b in r.json()["content"] if b["type"] == "text")
        assert "hello" in text.lower()

    def test_llm_capability_header_present(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="OK")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC)
        assert r.status_code == 200
        assert "LLM-Capability" in r.headers or "llm-capability" in r.headers


# ── Anthropic /v1/messages — streaming ───────────────────────────────────────

class TestAnthropicStream:
    def test_sse_event_sequence(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC, stream=True)
        assert r.status_code == 200
        events = collect_sse(r)
        types = [e["type"] for e in events]
        assert "message_start" in types
        assert "content_block_start" in types
        assert "content_block_delta" in types
        assert "content_block_stop" in types
        assert "message_delta" in types
        assert "message_stop" in types

    def test_streamed_text_matches_content(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC, stream=True)
        events = collect_sse(r)
        deltas = [e["delta"]["text"] for e in events
                  if e.get("type") == "content_block_delta"
                  and e.get("delta", {}).get("type") == "text_delta"]
        full_text = "".join(deltas)
        assert "hello" in full_text.lower()

    def test_usage_in_message_delta(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="test")
        r = post_messages(llm_headers, SIMPLE_ANTHROPIC, stream=True)
        events = collect_sse(r)
        msg_delta = next((e for e in events if e.get("type") == "message_delta"), None)
        assert msg_delta is not None
        assert "usage" in msg_delta
        assert msg_delta["usage"]["output_tokens"] > 0


# ── OpenAI /v1/chat/completions — non-streaming ──────────────────────────────

class TestOpenAINonStream:
    def test_basic_text_response_shape(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_completions(llm_headers, SIMPLE_OPENAI)
        assert r.status_code == 200
        d = r.json()
        assert d["object"] == "chat.completion"
        assert len(d["choices"]) > 0
        msg = d["choices"][0]["message"]
        assert msg["role"] == "assistant"
        assert msg["content"]
        assert d["choices"][0]["finish_reason"] in ("stop", "length", "tool_calls")
        assert "usage" in d

    def test_response_contains_mock_text(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_completions(llm_headers, SIMPLE_OPENAI)
        assert "hello" in r.json()["choices"][0]["message"]["content"].lower()


# ── OpenAI /v1/chat/completions — streaming ───────────────────────────────────

class TestOpenAIStream:
    def test_sse_chunk_sequence(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_completions(llm_headers, SIMPLE_OPENAI, stream=True)
        assert r.status_code == 200
        chunks = collect_sse(r)
        assert len(chunks) > 0
        for chunk in chunks:
            assert chunk["object"] == "chat.completion.chunk"
            assert len(chunk["choices"]) > 0
        # Final chunk must have finish_reason
        last = chunks[-1]
        assert last["choices"][0]["finish_reason"] is not None

    def test_reassembled_text_coherent(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello world")
        r = post_completions(llm_headers, SIMPLE_OPENAI, stream=True)
        chunks = collect_sse(r)
        text = "".join(
            c["choices"][0]["delta"].get("content", "")
            for c in chunks
            if c["choices"][0]["delta"].get("content")
        )
        assert "hello" in text.lower()


# ── Native tool call (mock returns proper tool_calls JSON) ────────────────────

class TestNativeToolUse:
    """
    The mock is a 'compatible' provider (native_tools=False) so the proxy uses
    tool emulation for all tool requests. Queue 'tool_emulation' type so the mock
    returns <tool_call> XML which the emulation layer parses into a tool_use block.
    """
    def test_anthropic_tool_use_block_returned(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file", tool_input={"path": "/etc/hosts"})
        r = post_messages(llm_headers, {**SIMPLE_ANTHROPIC, "tools": [TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
        assert len(tool_blocks) > 0
        tb = tool_blocks[0]
        assert tb["name"] == "read_file"
        assert "path" in tb["input"]

    def test_openai_tool_call_in_message(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file", tool_input={"path": "/etc/hosts"})
        r = post_completions(llm_headers, {**SIMPLE_OPENAI, "tools": [TOOL_DEF_OPENAI]})
        assert r.status_code == 200
        d = r.json()
        # Tool emulation on completions endpoint: proxy synthesizes OpenAI tool_calls
        msg = d["choices"][0]["message"]
        assert msg.get("tool_calls") or d["choices"][0]["finish_reason"] == "tool_calls"
        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]
            assert tc["function"]["name"] == "read_file"

    def test_anthropic_tool_call_streaming(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file", tool_input={"path": "/tmp/test"})
        r = post_messages(llm_headers, {**SIMPLE_ANTHROPIC, "tools": [TOOL_DEF]}, stream=True)
        assert r.status_code == 200
        events = collect_sse(r)
        # Streaming tool use: content_block_start with type=tool_use (from emulation)
        tool_starts = [e for e in events if e.get("type") == "content_block_start"
                       and e.get("content_block", {}).get("type") == "tool_use"]
        assert len(tool_starts) > 0 or "tool_calls" in str(events)


# ── Tool emulation (mock returns <tool_call> text; proxy synthesizes tool_use) ─

class TestToolEmulation:
    """
    The mock provider is `compatible` type — defaults to native_tools=False.
    Proxy injects tool schema as system prompt and parses <tool_call> from response.
    """
    def test_anthropic_emulated_tool_use_block(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file",
                       tool_input={"path": "/etc/hosts"})
        r = post_messages(llm_headers, {**SIMPLE_ANTHROPIC, "tools": [TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        # Proxy must synthesize a tool_use block from the <tool_call> text
        tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
        assert len(tool_blocks) > 0, f"Expected tool_use block, got: {d.get('content')}"
        assert tool_blocks[0]["name"] == "read_file"

    def test_mock_received_tool_schema_in_system(self, only_mock_routing, mock_ctl, llm_headers):
        """System prompt injected by proxy must contain the tool definition."""
        mock_ctl.queue(type="tool_emulation", tool_name="read_file",
                       tool_input={"path": "/etc/hosts"})
        post_messages(llm_headers, {**SIMPLE_ANTHROPIC, "tools": [TOOL_DEF]})
        req = mock_ctl.last()
        # Proxy strips 'tools' and injects schema into system message
        assert "tools" not in req or req.get("tools") is None or req.get("tools") == []
        messages = req.get("messages", [])
        sys_texts = [m["content"] for m in messages if m.get("role") == "system"]
        all_sys = " ".join(str(t) for t in sys_texts)
        assert "read_file" in all_sys, "Tool schema must appear in system prompt for emulation"

    def test_openai_emulated_tool_call(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file",
                       tool_input={"path": "/etc/hosts"})
        r = post_completions(llm_headers, {**SIMPLE_OPENAI, "tools": [TOOL_DEF_OPENAI]})
        assert r.status_code == 200
        d = r.json()
        msg = d["choices"][0]["message"]
        assert msg.get("tool_calls"), f"Expected tool_calls in response, got: {msg}"
        assert msg["tool_calls"][0]["function"]["name"] == "read_file"

    def test_plain_text_when_no_tool_call_in_response(self, only_mock_routing, mock_ctl, llm_headers):
        """If model doesn't call a tool, proxy returns plain text — not tool_use."""
        mock_ctl.queue(type="text", content="I cannot read files directly.")
        r = post_messages(llm_headers, {**SIMPLE_ANTHROPIC, "tools": [TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        text_blocks = [b for b in d.get("content", []) if b.get("type") == "text"]
        assert len(text_blocks) > 0


# ── CoT-E engagement ──────────────────────────────────────────────────────────

class TestCoTEmulation:
    """
    CoT-E is triggered by `claude-code` key type (not via LLM-Hint, since that
    requires native_reasoning=False on the provider which the mock satisfies).
    The pipeline makes 2-3 calls to the mock: plan, draft, critique.
    We queue enough responses and verify thinking blocks appear in the SSE stream.
    """
    def test_cot_produces_thinking_blocks(self, only_mock_routing, mock_ctl, cot_headers,
                                          admin_session, settings_snapshot):
        # Ensure CoT is enabled globally
        admin_session.put(f"{BASE_URL}/api/settings",
                          json={"cot_enabled": True, "cot_max_iterations": 1})

        # Queue responses for each CoT pass:
        # 1. Plan pass
        mock_ctl.queue(type="text", content="Plan: answer the user's coding question step by step.")
        # 2. Draft pass
        mock_ctl.queue(type="text", content="def sum_list(lst): return sum(lst)")
        # 3. Critique pass (score above threshold → skip refine)
        mock_ctl.queue(type="text", content="SCORE: 8\nGAPS: none")

        r = post_messages(cot_headers, {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 200,
            "messages": [{"role": "user", "content": "Write a Python function to sum a list."}],
            "stream": True,
        }, stream=True)

        assert r.status_code == 200, f"CoT request failed: {r.text}"
        events = collect_sse(r)
        types = [e.get("type") for e in events]
        thinking_deltas = [e for e in events
                           if e.get("type") == "content_block_delta"
                           and e.get("delta", {}).get("type") == "thinking_delta"]
        assert len(thinking_deltas) > 0, (
            f"Expected thinking blocks in SSE stream, got event types: {types}"
        )

    def test_cot_disabled_produces_no_thinking(self, only_mock_routing, mock_ctl, cot_headers,
                                                admin_session, settings_snapshot):
        admin_session.put(f"{BASE_URL}/api/settings", json={"cot_enabled": False})
        mock_ctl.queue(type="text", content="def sum_list(lst): return sum(lst)")

        r = post_messages(cot_headers, {
            "model": "claude-3-5-sonnet-20241022",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "Write a Python function to sum a list."}],
            "stream": True,
        }, stream=True)

        assert r.status_code == 200
        events = collect_sse(r)
        thinking_deltas = [e for e in events
                           if e.get("type") == "content_block_delta"
                           and e.get("delta", {}).get("type") == "thinking_delta"]
        assert len(thinking_deltas) == 0, "CoT is disabled — no thinking blocks expected"
        # Restore
        admin_session.put(f"{BASE_URL}/api/settings", json={"cot_enabled": True})
