"""Integration — cross-family translation (v3.10.0 regression guard).

The v3.10.0 fix: an Anthropic-wire ``/v1/messages`` request carrying
``tool_use`` / ``tool_result`` content blocks, routed to a litellm-
dispatched provider (Gemini, OpenRouter, litellm-Anthropic, ...), must
be translated to OpenAI shape before dispatch. Before the fix it 400'd
with "Invalid user message at index N" — ~69% of all fleet failures in
the 2026-05-15 audit.

These run against the live deployment (small Gemini calls). The unit
side is covered by tests/unit/test_v3100_translation_gate.py; this
file proves the path end-to-end through the real router + dispatch.
"""
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL
from tests.integration.conftest import collect_sse


# A standard tool-using Anthropic conversation: assistant emits a
# tool_use, the next user turn carries the matching tool_result. This
# is the exact shape that produced "Invalid user message at index 2".
TOOL_CONVO = {
    "model": "gemini-2.5-flash",
    "max_tokens": 32,
    "messages": [
        {"role": "user", "content": "list the files"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Running ls."},
            {"type": "tool_use", "id": "toolu_int01", "name": "Bash",
             "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_int01",
             "content": ".bashrc\n.profile\nnotes.txt"},
        ]},
    ],
}


def _post(api_key: str, body: dict, stream: bool = False) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/v1/messages",
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        json={**body, "stream": stream},
        verify=False, timeout=60, stream=stream,
    )


def test_tool_conversation_to_gemini_not_400(test_api_key):
    """The headline regression: a tool conversation to a Gemini model
    must not 400 with the index-N translation error."""
    r = _post(test_api_key, TOOL_CONVO)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
    assert "Invalid user message at index" not in r.text
    body = r.json()
    assert body.get("content") is not None, f"no content in response: {body}"


def test_tool_conversation_streaming_completes_cleanly(test_api_key):
    """Streaming variant — the SSE stream must terminate with a
    ``message_stop`` event (the success terminal — see the BUG-001
    streaming-error contract)."""
    r = _post(test_api_key, TOOL_CONVO, stream=True)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
    events = collect_sse(r)
    types = [e.get("type") for e in events]
    assert "message_stop" in types, f"stream did not complete cleanly: {types}"


def test_orphan_tool_result_conversation_not_400(test_api_key):
    """A conversation window that begins mid-tool-exchange — first turn
    is a tool_result with no assistant tool_use to attach to. v3.10.0
    Fix B emits it as plain user text, never a dangling role:'tool'."""
    body = {
        "model": "gemini-2.5-flash",
        "max_tokens": 32,
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_gone",
                 "content": "[Bash] empty command"},
            ]},
            {"role": "user", "content": "ok, continue"},
        ],
    }
    r = _post(test_api_key, body)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
    assert "Invalid user message at index" not in r.text


def test_plain_text_conversation_still_works(test_api_key):
    """Control — a plain-text conversation (no content blocks) must be
    unaffected by the translation widening."""
    body = {
        "model": "gemini-2.5-flash",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "reply with the word ok"}],
    }
    r = _post(test_api_key, body)
    assert r.status_code == 200, f"got {r.status_code}: {r.text[:300]}"
