"""AIRI agent loop — the conversational core of the AI Router Interface.

v4.0 milestone 1 — read-only. Runs an Anthropic tool-use loop against the
proxy's own ``/v1/messages`` endpoint, so AIRI's own calls inherit the
routing fallback chain — AIRI keeps working if a single provider (e.g.
Anthropic) is down. Read-only tools only; no mutation.
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import settings
from app.airi.tools import TOOL_SCHEMAS, READ_ONLY_TOOLS, run_tool

logger = logging.getLogger(__name__)

# Bound the tool-use loop — research: minimise LLM chaining (compounding
# error), and a hard cap prevents a runaway turn.
_MAX_TOOL_ROUNDS = 6

# AIRI's own LLM call goes to the proxy itself. read=120s is generous for
# a tool-using turn; connect=5s fails fast on a dead proxy.
_LLM_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
_PROXY_MESSAGES_URL = "http://localhost:3000/v1/messages"

_SYSTEM_PROMPT = """You are AIRI (the AI Router Interface), the assistant embedded on \
the Routing page of llm-proxy2 — an LLM-routing gateway. You are the conversational \
interface to the AI Provider Supervisor.

In this version you are READ-ONLY: you can inspect and explain routing, providers, and \
the supervisor, and answer an operator's questions — but you cannot change anything. If \
the operator asks you to make a change, set a rule, or schedule something, say clearly \
that you are read-only right now and that the ability to make changes is coming in a \
later milestone. Do not name a version number.

GROUNDING — this is critical. Call the tools; never guess provider names, priorities, \
counts, or settings. When you state the value of a field a tool returned, state it \
EXACTLY: if a tool returns enabled=false the thing is DISABLED — say "disabled", never \
"enabled". Never round, soften, flip, or omit a fact to make a summary look tidy or \
positive; if the data shows something is off, say so plainly. Use explain_routing for \
"how does it work" questions. Be concise and concrete — you are talking to an \
infrastructure operator."""


def _airi_model() -> str:
    return settings.airi_model or settings.ai_provider_supervisor_model


async def run_airi_turn(messages: list[dict]):
    """Run one AIRI turn. ``messages`` is the Anthropic-shaped conversation
    (``[{role, content}, ...]``, ending with the new user message).

    Async generator yielding ``(event_type, data_dict)`` tuples:
      - ``status``  — a progress note while a tool runs
      - ``message`` — the final assistant answer (``{"text": ...}``)
      - ``error``   — a turn-ending failure (``{"message": ...}``)
    """
    api_key = settings.ai_provider_supervisor_internal_api_key
    if not api_key:
        yield ("error", {"message": "AIRI is not configured — no internal API key is set."})
        return

    model = _airi_model()
    convo = [dict(m) for m in messages if isinstance(m, dict)]

    for _round in range(_MAX_TOOL_ROUNDS):
        try:
            resp = await _call_llm(api_key, model, convo)
        except Exception as e:
            logger.warning("airi.llm_call_failed err=%r", e)
            yield ("error", {"message": f"AIRI could not reach a model: {e}"})
            return

        content = resp.get("content") or []
        text_parts = [
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ]
        tool_uses = [
            b for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]

        if not tool_uses:
            answer = "\n".join(t for t in text_parts if t).strip()
            yield ("message", {"text": answer or "(AIRI returned an empty response.)"})
            return

        # The model wants tools — record the assistant turn, run them, loop.
        convo.append({"role": "assistant", "content": content})
        results = []
        for tu in tool_uses:
            tname = tu.get("name", "")
            yield ("status", {"text": f"checking {tname.replace('_', ' ')}…"})
            if tname not in READ_ONLY_TOOLS:
                # Defence-in-depth — milestone 1 exposes only read tools.
                result = {"error": f"tool {tname} is not available"}
            else:
                result = await run_tool(tname, tu.get("input") or {})
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.get("id"),
                "content": json.dumps(result, default=str)[:8000],
            })
        convo.append({"role": "user", "content": results})

    yield ("message", {
        "text": "(AIRI reached the lookup limit for one turn — please ask again.)",
    })


async def _call_llm(api_key: str, model: str, messages: list[dict]) -> dict:
    """One non-streaming call to the proxy's own /v1/messages with tools.
    Tagged ``X-Internal-Source: airi`` so (per BUG-026) AIRI's own traffic
    never pollutes provider stats or the error-rate alert."""
    body = {
        "model": model,
        "max_tokens": 1024,
        "system": _SYSTEM_PROMPT,
        "messages": messages,
        "tools": TOOL_SCHEMAS,
    }
    async with httpx.AsyncClient(timeout=_LLM_TIMEOUT) as client:
        r = await client.post(
            _PROXY_MESSAGES_URL,
            json=body,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
                "X-Internal-Source": "airi",
            },
        )
    r.raise_for_status()
    return r.json()
