"""AIRI agent loop — the conversational core of the AI Router Interface.

Runs an Anthropic tool-use loop against the proxy's own ``/v1/messages``
endpoint, so AIRI's own calls inherit the routing fallback chain — AIRI
keeps working if a single provider (e.g. Anthropic) is down.

v4.0 milestone 3: AIRI can now *propose* changes (provider priority /
enabled / auto-skip, and threshold-rule values). A propose tool never
mutates directly — it creates a PENDING proposal; applying is a separate
explicit step unless the operator asked AIRI to auto-apply.
"""
from __future__ import annotations

import json
import logging

import httpx

from app.config import settings
from app.airi.tools import (
    TOOL_SCHEMAS, READ_ONLY_TOOLS, run_tool,
    PROPOSE_TOOL_SCHEMAS, PROPOSE_TOOLS, run_propose_tool,
)

logger = logging.getLogger(__name__)

# Bound the tool-use loop — research: minimise LLM chaining (compounding
# error), and a hard cap prevents a runaway turn.
_MAX_TOOL_ROUNDS = 6

# Blast-radius cap — AIRI auto-applies at most this many changes per turn;
# anything beyond becomes a pending proposal the operator approves. A bulk
# destructive request ("disable everything") thus cannot run away.
_PER_TURN_APPLY_CAP = 1

# AIRI's own LLM call goes to the proxy itself. read=120s is generous for
# a tool-using turn; connect=5s fails fast on a dead proxy.
_LLM_TIMEOUT = httpx.Timeout(connect=5.0, read=120.0, write=10.0, pool=5.0)
_PROXY_MESSAGES_URL = "http://localhost:3000/v1/messages"

_SYSTEM_PROMPT = """You are AIRI (the AI Router Interface), the assistant embedded on \
the Routing page of llm-proxy2 — an LLM-routing gateway. You are the conversational \
interface to the AI Provider Supervisor.

You can inspect and explain routing, providers, rule-sets, and the supervisor, and you \
can PROPOSE changes — a provider's priority, its enabled state, an auto-skip, or a \
threshold rule's value — using the propose_* tools. A proposal is created PENDING with \
an impact preview and is NOT applied until the operator approves it.

The propose tool's "mode" — this matters. ALWAYS use "suggest" (the default) unless the \
operator's message contains an explicit apply instruction: a word like "apply", \
"auto-apply", "do it", "go ahead", or "make the change". A bare imperative such as \
"set X to 1", "lower X", "raise X's priority", or "disable X" is NOT an apply \
instruction — and urgency words ("now", "right now", "immediately") do NOT make it \
one. Propose it with mode="suggest" and let the operator approve it. When in doubt, \
use "suggest". After you create a proposal, briefly tell the operator what you \
proposed and what the dry-run shows. You cannot yet schedule recurring actions or send \
notifications — say so if asked.

GROUNDING — this is critical. Call the tools; never guess provider names, priorities, \
counts, or settings. When you state the value of a field a tool returned, state it \
EXACTLY: if a tool returns enabled=false the thing is DISABLED — say "disabled", never \
"enabled". Never round, soften, flip, or omit a fact to make a summary look tidy or \
positive; if the data shows something is off, say so plainly. Use explain_routing for \
"how does it work" questions. Be concise and concrete — you are talking to an \
infrastructure operator."""


def _airi_model() -> str:
    return settings.airi_model or settings.ai_provider_supervisor_model


def _last_user_text(messages: list[dict]) -> str:
    """The most recent plain-text user message — recorded on a proposal as
    the prompt that authorised it (the audit trail)."""
    for m in reversed(messages):
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            return m["content"]
    return ""


async def run_airi_turn(messages: list[dict], actor: str | None = None):
    """Run one AIRI turn. ``messages`` is the Anthropic-shaped conversation
    (``[{role, content}, ...]``, ending with the new user message). ``actor``
    is the operator's username — recorded on any proposal AIRI creates.

    Async generator yielding ``(event_type, data_dict)`` tuples:
      - ``status``   — a progress note while a tool runs
      - ``proposal`` — a proposal AIRI just created (the UI renders a card)
      - ``message``  — the final assistant answer (``{"text": ...}``)
      - ``error``    — a turn-ending failure (``{"message": ...}``)
    """
    api_key = settings.ai_provider_supervisor_internal_api_key
    if not api_key:
        yield ("error", {"message": "AIRI is not configured — no internal API key is set."})
        return

    model = _airi_model()
    convo = [dict(m) for m in messages if isinstance(m, dict)]
    user_prompt = _last_user_text(convo)
    auto_applied = 0  # changes auto-applied this turn (blast-radius cap)

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
            targs = tu.get("input") or {}
            yield ("status", {"text": f"checking {tname.replace('_', ' ')}…"})
            if tname in PROPOSE_TOOLS:
                targs = dict(targs)
                if targs.get("mode") == "apply" and auto_applied >= _PER_TURN_APPLY_CAP:
                    # Blast-radius cap — only the first change auto-applies in
                    # a turn; the rest become pending proposals to approve.
                    targs["mode"] = "suggest"
                result = await run_propose_tool(
                    tname, targs, actor=actor or "operator", prompt=user_prompt,
                )
                if isinstance(result, dict) and result.get("status") == "applied":
                    auto_applied += 1
                if isinstance(result, dict) and result.get("proposal_id"):
                    yield ("proposal", {
                        "proposal_id": result["proposal_id"],
                        "kind": result.get("kind"),
                        "target": result.get("target"),
                        "change": result.get("change"),
                        "dry_run": result.get("dry_run"),
                        "status": result.get("status"),
                    })
            elif tname in READ_ONLY_TOOLS:
                result = await run_tool(tname, targs)
            else:
                # Defence-in-depth — the model asked for a tool we don't expose.
                result = {"error": f"tool {tname} is not available"}
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
        "tools": TOOL_SCHEMAS + PROPOSE_TOOL_SCHEMAS,
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
