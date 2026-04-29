"""R3 compaction tests.

Covers:
  - should_compact threshold math
  - _split_for_compaction preserves system + last 8 messages
  - to_openai_tools shape conversion
  - adapt_tools_for_route picks emulation vs native correctly
  - apply_compaction_to_db rewrites run_messages with dense seqs
"""
from __future__ import annotations

import pytest


# ── Token math ─────────────────────────────────────────────────────────────


def test_should_compact_at_80_percent():
    from app.runs.tokens import should_compact
    assert should_compact(80_000, 100_000) is True
    assert should_compact(80_001, 100_000) is True


def test_should_compact_below_80_percent():
    from app.runs.tokens import should_compact
    assert should_compact(79_999, 100_000) is False
    assert should_compact(0, 100_000) is False


def test_should_compact_zero_max_returns_false():
    from app.runs.tokens import should_compact
    assert should_compact(1000, 0) is False


def test_compaction_target_is_50_percent():
    from app.runs.tokens import compaction_target
    assert compaction_target(100_000) == 50_000


# ── Split logic ────────────────────────────────────────────────────────────


def _msg(role, text):
    return {"role": role, "content": text}


def test_split_preserves_system_and_last_8():
    from app.runs.compaction import _split_for_compaction
    msgs = [_msg("system", "S")]
    # 12 user/assistant turns = 12 messages — body keeps 4, tail keeps 8
    for i in range(12):
        msgs.append(_msg("user", f"u{i}"))
        msgs.append(_msg("assistant", f"a{i}"))
    sys_msgs, body, tail = _split_for_compaction(msgs)
    assert len(sys_msgs) == 1
    assert sys_msgs[0]["content"] == "S"
    assert len(tail) == 8
    assert len(body) == 24 - 8
    # Tail must be the LAST 8 messages
    assert tail[-1]["content"] == "a11"


def test_split_with_short_conversation_returns_no_body():
    from app.runs.compaction import _split_for_compaction
    msgs = [_msg("system", "S"), _msg("user", "hi"), _msg("assistant", "hello")]
    sys_msgs, body, tail = _split_for_compaction(msgs)
    assert sys_msgs == [_msg("system", "S")]
    assert body == []
    # Tail keeps the 2 non-system messages
    assert len(tail) == 2


# ── Tool spec translation ──────────────────────────────────────────────────


def test_to_openai_tools_shape():
    from app.runs.tools import to_openai_tools
    src = [{
        "name": "Read",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }]
    out = to_openai_tools(src)
    assert out == [{
        "type": "function",
        "function": {
            "name": "Read",
            "description": "Read a file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }]


def test_adapt_tools_for_route_native_anthropic_passes_through():
    from app.runs.tools import adapt_tools_for_route
    src = [{"name": "X", "description": "y", "input_schema": {"type": "object"}}]
    tools, prompt = adapt_tools_for_route(
        src, litellm_model="anthropic/claude-sonnet-4-5", native_tools=True,
    )
    assert tools == src
    assert prompt is None


def test_adapt_tools_for_route_native_openai_translates():
    from app.runs.tools import adapt_tools_for_route
    src = [{"name": "X", "description": "y", "input_schema": {"type": "object"}}]
    tools, prompt = adapt_tools_for_route(
        src, litellm_model="openai/gpt-4o", native_tools=True,
    )
    assert tools is not None
    assert tools[0]["type"] == "function"
    assert prompt is None


def test_adapt_tools_for_route_no_native_uses_pbtc_emulation():
    from app.runs.tools import adapt_tools_for_route
    src = [{"name": "X", "description": "y", "input_schema": {"type": "object"}}]
    tools, prompt = adapt_tools_for_route(
        src, litellm_model="some/old-model", native_tools=False,
    )
    assert tools is None
    assert prompt is not None
    assert "tool" in prompt.lower()


def test_adapt_tools_for_route_empty_passes_through_as_none():
    from app.runs.tools import adapt_tools_for_route
    tools, prompt = adapt_tools_for_route([], litellm_model="x/y", native_tools=True)
    assert tools is None and prompt is None


# ── apply_compaction_to_db ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_compaction_to_db_rewrites_messages_with_dense_seqs():
    import time
    from sqlalchemy import select
    from app.models.database import init_db, AsyncSessionLocal
    from app.models.db import Run, RunMessage
    from app.runs.compaction import apply_compaction_to_db
    from app.runs.ids import new_run_id
    from app.runs.state import RunStatus

    await init_db()
    rid = new_run_id()
    now = time.time()
    async with AsyncSessionLocal() as db:
        db.add(Run(
            id=rid, api_key_id="t", owner_node_id="local",
            status=RunStatus.RUNNING.value, deadline_ts=now + 60,
            max_turns=10, model_preference=[], tools_spec=[], metadata_json={},
            model_calls=0, tool_calls=0, tokens_in=0, tokens_out=0,
            created_at=now, updated_at=now,
        ))
        for i in range(5):
            db.add(RunMessage(run_id=rid, seq=i + 1, role="user",
                              content=f"orig-{i}", tokens=0, created_at=now))
        await db.commit()

        new_msgs = [
            {"role": "system", "content": "S"},
            {"role": "assistant", "content": "summary"},
            {"role": "user", "content": "tail-1"},
            {"role": "assistant", "content": "tail-2"},
        ]
        await apply_compaction_to_db(db, run_id=rid, new_messages=new_msgs)
        await db.commit()

        rows = (await db.execute(
            select(RunMessage).where(RunMessage.run_id == rid)
            .order_by(RunMessage.seq.asc())
        )).scalars().all()

    assert [r.seq for r in rows] == [1, 2, 3, 4]
    assert [r.role for r in rows] == ["system", "assistant", "user", "assistant"]
    assert rows[1].content == "summary"
