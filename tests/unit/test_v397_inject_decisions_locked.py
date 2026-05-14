"""v3.9.7 (#267) — lock the 3 Phase-4 design decisions before Phase 10.

Q1 (scope): inject fires only when X-Conversation-Id is supplied.
Q2 (Anthropic): inject as system-prompt prefix, NOT memory_blocks, NOT
   first user message.
Q3 (OpenAI): inject as system-prompt prefix on msg-0 or synthesized at
   index 0, NOT into other roles' content.

These tests are regression guards that catch accidental drift. Most of
the underlying behavior is exercised in test_v389_memory_injection.py
already; this file exists specifically to document the Phase-10
pre-flip decisions in test form so a future contributor can't
reintroduce one of the rejected approaches without first deleting a
named test.
"""
from __future__ import annotations

from unittest.mock import patch

from app.memory.inject import _inject_anthropic, _inject_openai


PREFIX = "[Persistent caller memory — applies across providers]\nfoo\n"


# ── Q1: scope (X-Conversation-Id gating) ────────────────────────────


async def test_q1_no_conversation_id_means_no_inject():
    """maybe_inject_memory MUST be a no-op when conversation_id is None.

    Even when caller_memory_enabled=True and an api_key has memory
    entries with conversation_id=None, the un-scoped path stays clean.
    """
    from app.memory.inject import maybe_inject_memory

    class _S:
        caller_memory_enabled = True

    with patch("app.config.settings", _S()):
        body = {"messages": [{"role": "user", "content": "hi"}]}
        out_body, injected = await maybe_inject_memory(
            db=None,  # unused on this short-circuit path
            body=body, api_key_id="key1",
            conversation_id=None, memory_tag=None,
            endpoint="messages",
        )
    assert injected is False
    assert out_body == body  # unchanged


# ── Q2: Anthropic — system prompt, not memory_blocks ────────────────


def test_q2_anthropic_inject_writes_to_system_not_memory_blocks():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_anthropic(body, PREFIX)
    # The prefix lands in body["system"] (string form when no prior).
    assert "system" in out
    # Critically — NOT in a top-level "memory_blocks" field.
    assert "memory_blocks" not in out


def test_q2_anthropic_inject_does_not_touch_user_message():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_anthropic(body, PREFIX)
    # First user message must be untouched.
    assert out["messages"][0]["content"] == "hi"


def test_q2_anthropic_preserves_existing_system_string():
    body = {
        "system": "you are a helpful assistant",
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = _inject_anthropic(body, PREFIX)
    # Prefix prepended, original preserved.
    assert out["system"].startswith(PREFIX)
    assert "you are a helpful assistant" in out["system"]


def test_q2_anthropic_preserves_existing_system_block_list():
    body = {
        "system": [{"type": "text", "text": "you are helpful"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = _inject_anthropic(body, PREFIX)
    # First block is the injected prefix, second is the caller's.
    assert isinstance(out["system"], list)
    assert len(out["system"]) == 2
    assert out["system"][0]["type"] == "text"
    assert "Persistent caller memory" in out["system"][0]["text"]
    assert out["system"][1]["text"] == "you are helpful"


# ── Q3: OpenAI — system prompt, not user/assistant content ──────────


def test_q3_openai_inject_does_not_touch_user_message():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_openai(body, PREFIX)
    # The user message must stay intact.
    user_msgs = [m for m in out["messages"] if m["role"] == "user"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["content"] == "hi"


def test_q3_openai_inject_synthesizes_system_at_index_0_when_missing():
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_openai(body, PREFIX)
    assert out["messages"][0]["role"] == "system"
    assert "Persistent caller memory" in out["messages"][0]["content"]


def test_q3_openai_inject_prepends_to_existing_system_at_index_0():
    body = {
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": "hi"},
        ]
    }
    out = _inject_openai(body, PREFIX)
    # Same number of messages — prefix concatenated into msg-0 content.
    assert len(out["messages"]) == 2
    assert out["messages"][0]["role"] == "system"
    assert out["messages"][0]["content"].startswith(PREFIX)
    assert "you are helpful" in out["messages"][0]["content"]


def test_q3_openai_inject_does_not_create_new_top_level_field():
    """OpenAI has no `memory_blocks` analog — make sure we don't invent one."""
    body = {"messages": [{"role": "user", "content": "hi"}]}
    out = _inject_openai(body, PREFIX)
    assert "memory_blocks" not in out
    assert "memory" not in out
    # Body keys are unchanged except the messages list.
    assert set(out.keys()) == {"messages"}


# ── Docstring-level lock guards ────────────────────────────────────


def test_inject_module_documents_resolved_decisions():
    """Future contributors must see the decisions are locked."""
    from pathlib import Path
    src = Path("app/memory/inject.py").read_text()
    assert "RESOLVED 2026-05-14" in src
    assert "Q1" in src and "Q2" in src and "Q3" in src
    # The rejected options are named so a reader knows what was rejected
    assert "memory_blocks" in src
    assert "first user message" in src


def test_rfc_documents_resolved_decisions():
    from pathlib import Path
    rfc = Path("docs/rfc/2026-05-proxy-memory-store.md").read_text()
    assert "locked 2026-05-14" in rfc
    assert "system-prompt prefix" in rfc
