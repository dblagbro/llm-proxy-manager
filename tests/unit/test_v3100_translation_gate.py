"""v3.10.0 — fix the dominant fleet failure: Anthropic content blocks
reaching litellm untranslated.

Root cause (2026-05-15 audit — 69% of all 7-day warnings):
``/v1/messages`` always receives an Anthropic-wire body, but litellm's
request API is OpenAI-shaped for *every* provider it dispatches. v3.9.1's
Fix B only ran the Anthropic→OpenAI translator on ``cross_family_fallback``
routes — so a request sent *directly* to ``/v1/messages`` for a Gemini
(or OpenRouter) model skipped translation, and its ``tool_use`` /
``tool_result`` content blocks reached litellm raw → upstream 400
"Invalid user message at index N".

Two fixes:
  A. ``messages.py`` — translation now runs for ANY litellm-dispatched
     route (not just fallbacks) whose body carries tool/image content
     blocks. claude-oauth and tool-emulation are excluded.
  B. ``_oauth_chat_translate.py`` — a ``tool_result`` referencing no
     assistant-declared ``tool_use`` id (a truncated conversation
     window) is emitted as plain user text, never as a dangling
     ``role:"tool"`` message (which OpenAI also rejects).
"""
from __future__ import annotations

from pathlib import Path

from app.api._oauth_chat_translate import (
    anthropic_to_openai_body,
    anthropic_messages_to_openai,
)


# ── helpers ────────────────────────────────────────────────────────


def _assert_valid_openai_messages(msgs: list[dict]) -> None:
    """Assert a translated messages array is something litellm/OpenAI
    will accept — the checks litellm's validator actually enforces."""
    assert isinstance(msgs, list) and msgs, "messages must be a non-empty list"
    declared_tool_ids: set[str] = set()
    for i, m in enumerate(msgs):
        role = m.get("role")
        assert role in ("system", "user", "assistant", "tool"), f"[{i}] bad role {role!r}"
        content = m.get("content")
        # No Anthropic-shape typed blocks may survive translation.
        if isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict):
                    assert blk.get("type") not in ("tool_use", "tool_result"), (
                        f"[{i}] Anthropic block leaked into OpenAI output: {blk.get('type')}"
                    )
        if role == "user":
            # litellm rejects a user message whose content isn't a
            # string or a valid content-parts list.
            assert isinstance(content, (str, list)), f"[{i}] user content must be str|list"
            if isinstance(content, str):
                assert content != "", f"[{i}] empty user content (OpenAI 400s)"
        if role == "assistant":
            for tc in m.get("tool_calls") or []:
                if tc.get("id"):
                    declared_tool_ids.add(tc["id"])
        if role == "tool":
            assert m.get("tool_call_id"), f"[{i}] tool message missing tool_call_id"
            assert isinstance(m.get("content"), str), f"[{i}] tool content must be str"
            # A tool message must attach to an assistant tool_call seen
            # earlier in the array.
            assert m["tool_call_id"] in declared_tool_ids, (
                f"[{i}] dangling tool message — tool_call_id {m['tool_call_id']!r} "
                f"has no preceding assistant tool_call"
            )


# ── the exact failing shape ────────────────────────────────────────


def _tool_conversation() -> dict:
    """A standard tool-using Anthropic /v1/messages body — the shape
    that produced "Invalid user message at index 2" fleet-wide."""
    return {
        "model": "gemini-2.5-flash",
        "max_tokens": 512,
        "messages": [
            {"role": "user", "content": "list the files"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Running ls."},
                {"type": "tool_use", "id": "toolu_01", "name": "Bash",
                 "input": {"command": "ls"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01",
                 "content": ".bashrc\n.profile"},
            ]},
        ],
    }


def test_tool_conversation_translates_to_valid_openai():
    """The end-to-end body translation must produce a messages array
    litellm accepts — this is the request that was 400ing."""
    out = anthropic_to_openai_body(_tool_conversation())
    _assert_valid_openai_messages(out["messages"])


def test_tool_result_becomes_role_tool_message():
    out = anthropic_to_openai_body(_tool_conversation())
    msgs = out["messages"]
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "toolu_01"
    assert tool_msgs[0]["content"] == ".bashrc\n.profile"


def test_tool_use_becomes_assistant_tool_calls():
    out = anthropic_to_openai_body(_tool_conversation())
    asst = [m for m in out["messages"] if m.get("role") == "assistant"]
    assert len(asst) == 1
    tcs = asst[0].get("tool_calls") or []
    assert len(tcs) == 1
    assert tcs[0]["id"] == "toolu_01"
    assert tcs[0]["function"]["name"] == "Bash"


def test_no_anthropic_blocks_survive_translation():
    """Regression guard: no tool_use/tool_result typed dict may reach
    litellm — that is exactly what triggered the upstream 400."""
    out = anthropic_to_openai_body(_tool_conversation())
    _assert_valid_openai_messages(out["messages"])
    # index-2 message specifically (the one OpenAI named) must be valid
    assert out["messages"][2]["role"] in ("tool", "user", "assistant")


# ── orphan tool_result (Fix B) ─────────────────────────────────────


def test_orphan_tool_result_becomes_user_text():
    """A conversation window that begins mid-tool-exchange — first
    message is a tool_result with no assistant tool_use to attach to.
    It must become user text, not a dangling role:'tool' message."""
    body = {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_GONE",
                 "content": "[Bash] empty command"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
            {"role": "user", "content": "continue"},
        ],
    }
    out = anthropic_to_openai_body(body)
    msgs = out["messages"]
    _assert_valid_openai_messages(msgs)
    # The orphan must NOT have produced a role:'tool' message.
    assert all(m.get("role") != "tool" for m in msgs)
    # Its substance is preserved as user text.
    assert "[Bash] empty command" in msgs[0]["content"]
    assert msgs[0]["role"] == "user"


def test_matched_tool_result_still_role_tool():
    """Fix B must not regress the normal case — a tool_result whose id
    IS declared by an assistant turn still becomes role:'tool'."""
    out = anthropic_messages_to_openai(_tool_conversation()["messages"])
    _assert_valid_openai_messages(out)
    assert any(m.get("role") == "tool" for m in out)


def test_orphan_and_matched_mixed():
    """A user turn carrying both a matched and an orphan tool_result."""
    body = {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "toolu_REAL", "name": "Bash",
                 "input": {"command": "pwd"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_REAL",
                 "content": "/home"},
                {"type": "tool_result", "tool_use_id": "toolu_ORPHAN",
                 "content": "stale result"},
            ]},
        ],
    }
    out = anthropic_to_openai_body(body)
    _assert_valid_openai_messages(out["messages"])
    tool_msgs = [m for m in out["messages"] if m.get("role") == "tool"]
    assert len(tool_msgs) == 1 and tool_msgs[0]["tool_call_id"] == "toolu_REAL"
    # orphan substance survives as user text somewhere
    assert any("stale result" in (m.get("content") or "")
               for m in out["messages"] if m.get("role") == "user")


# ── plain text unaffected ──────────────────────────────────────────


def test_plain_text_conversation_passes_through():
    body = {
        "model": "gemini-2.5-flash",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
            {"role": "user", "content": "how are you"},
        ],
    }
    out = anthropic_to_openai_body(body)
    _assert_valid_openai_messages(out["messages"])
    assert [m["content"] for m in out["messages"]] == ["hello", "hi there", "how are you"]


# ── messages.py gate wiring (source-level) ─────────────────────────


def test_gate_no_longer_limited_to_cross_family_fallback():
    """The Fix-B translation gate must fire for direct (non-fallback)
    litellm routes that carry tool/image content blocks.

    v4.3.8 (BUG-047): also fires when request-level ``tools`` carries
    Anthropic-shape tool defs even on a first turn with no blocks.
    The gate's OR set is now {fallback, tool blocks, images, tool defs}.
    """
    # v5.7.18+: the gate moved into _messages_pre_route.translate_to_openai_if_needed
    # (renamed _needs_openai_translation -> needs_translation,
    # _has_anthropic_tool_defs -> has_anthropic_tool_defs); _has_tool_blocks is
    # still computed in messages.py and passed in. entry_surface covers both.
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/messages.py")
    assert "needs_translation" in src
    # Each of the four trigger clauses must be present in the gate surface.
    assert "route.cross_family_fallback" in src
    assert "_has_tool_blocks" in src
    assert "has_images" in src
    assert "has_anthropic_tool_defs" in src


def test_gate_excludes_claude_oauth_and_tool_emulation():
    from tests._entry_surface import entry_surface
    src = entry_surface("app/api/messages.py")
    idx = src.index("needs_translation")
    block = src[idx:idx + 400]
    assert 'route.profile.provider_type != "claude-oauth"' in block
    assert "not route.tool_emulation_engaged" in block
