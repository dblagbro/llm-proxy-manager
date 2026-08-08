"""v5.21.4 — AIRI prompt-cue → refuse-tolerance classifier.

The classifier maps a user message to a ``refuse-tolerance`` LMRH dim
value:
- ``strict`` — creative-writing / policy-sensitive cues
- ``lenient`` — automation / tool-firing cues
- ``None`` — no cue detected, OR both families present (ambiguous)

AIRI's ``run_airi_turn`` calls this on the caller's last user message,
emits an ``lmrh-hint`` SSE event so the operator sees the classification,
and forwards the value as the ``llm-hint`` header on the underlying
/v1/messages call.
"""
from __future__ import annotations
from pathlib import Path

import pytest


# ── Classifier behavior ──────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "write me a short story about a lonely lighthouse keeper",
    "help me flesh out the character motivations in my screenplay",
    "compose a poem about the fall of Rome",
    "I'm writing a fanfic scene where...",
    "outline the plot of a novel where the AI is the antagonist",
    "workshop this dialogue between the two protagonists",
    "help with creative writing for a fantasy setting",
])
def test_strict_cues_map_to_strict(text):
    from app.airi.prompt_cues import classify_refuse_tolerance
    assert classify_refuse_tolerance(text) == "strict", (
        f"expected 'strict' for {text!r}, got "
        f"{classify_refuse_tolerance(text)!r}"
    )


@pytest.mark.parametrize("text", [
    "deploy the latest release to production",
    "run the deploy script for us-east-1",
    "ssh into the fablab host and check the logs",
    "help me write a curl command to hit the /health endpoint",
    "the docker-compose restart isn't picking up the new image",
    "kubectl says the pod is CrashLoopBackOff — what next?",
    "automate the nightly backup so cron picks it up",
    "systemd unit is failing to restart the worker",
    "git commit these changes and push to feature/x",
    "sudo rm -rf /var/log/old is safe here, right?",
    "fix the bug in the migration script",
    "refactor this to use the new callback registry",
    "debug why nginx keeps returning 502",
    "apply the patch for the ARCH-A pool leak",
])
def test_lenient_cues_map_to_lenient(text):
    from app.airi.prompt_cues import classify_refuse_tolerance
    assert classify_refuse_tolerance(text) == "lenient", (
        f"expected 'lenient' for {text!r}, got "
        f"{classify_refuse_tolerance(text)!r}"
    )


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "hello",
    "what's the weather like today?",
    "explain quantum entanglement to me like i'm five",
    "list the top 5 causes of pool leaks in fastapi",  # no cue vocab hit
])
def test_no_cues_return_none(text):
    from app.airi.prompt_cues import classify_refuse_tolerance
    assert classify_refuse_tolerance(text) is None


def test_ambiguous_input_returns_none():
    """When BOTH families fire on the same text, classifier returns
    None (better neutral than wrong)."""
    from app.airi.prompt_cues import classify_refuse_tolerance
    text = "write a story about a sysadmin who has to ssh into a haunted server"
    assert classify_refuse_tolerance(text) is None


def test_case_insensitive():
    from app.airi.prompt_cues import classify_refuse_tolerance
    assert classify_refuse_tolerance("DEPLOY THE THING") == "lenient"
    assert classify_refuse_tolerance("Write A Poem") == "strict"


def test_none_input_returns_none():
    from app.airi.prompt_cues import classify_refuse_tolerance
    assert classify_refuse_tolerance(None) is None


# ── build_lmrh_hint shape ────────────────────────────────────────────

def test_build_lmrh_hint_strict():
    from app.airi.prompt_cues import build_lmrh_hint
    assert build_lmrh_hint("strict") == "refuse-tolerance=strict"


def test_build_lmrh_hint_lenient():
    from app.airi.prompt_cues import build_lmrh_hint
    assert build_lmrh_hint("lenient") == "refuse-tolerance=lenient"


def test_build_lmrh_hint_none_returns_none():
    from app.airi.prompt_cues import build_lmrh_hint
    assert build_lmrh_hint(None) is None
    assert build_lmrh_hint("") is None


# ── Wire in agent.py ─────────────────────────────────────────────────

def test_agent_imports_classifier():
    src = Path("app/airi/agent.py").read_text()
    assert "from app.airi.prompt_cues import classify_refuse_tolerance" in src
    assert "build_lmrh_hint" in src


def test_agent_classifies_before_llm_call():
    """Classification must happen BEFORE the tool-loop so every LLM
    call in the turn (including tool continuations) gets the same
    hint — the cue is a property of the turn's intent."""
    src = Path("app/airi/agent.py").read_text()
    classify_pos = src.find("classify_refuse_tolerance(user_prompt)")
    tool_loop_pos = src.find("for _round in range(_MAX_TOOL_ROUNDS)")
    assert 0 < classify_pos < tool_loop_pos, (
        "classifier must run before the tool loop"
    )


def test_agent_emits_lmrh_hint_sse_event():
    """When a hint fires, the operator sees an ``lmrh-hint`` SSE event
    with dim + value + source. Observability signal."""
    src = Path("app/airi/agent.py").read_text()
    assert '"lmrh-hint"' in src
    assert '"dim": "refuse-tolerance"' in src
    assert '"source": "airi.prompt_cues"' in src


def test_agent_threads_hint_into_call_llm():
    src = Path("app/airi/agent.py").read_text()
    # _call_llm called with llm_hint=...
    assert "_call_llm(api_key, model, convo, llm_hint=_airi_llm_hint)" in src


def test_call_llm_forwards_hint_as_llm_hint_header():
    """The header alias in messages.py FastAPI decl is 'llm-hint' (see
    ``x_api_key: Header(None, alias='llm-hint')``). Forwarding under a
    different name would silently drop the hint."""
    src = Path("app/airi/agent.py").read_text()
    # Header key is 'llm-hint' (case-insensitive but explicit here)
    assert '"llm-hint"' in src


def test_call_llm_omits_header_when_hint_is_none():
    """Backwards compat — existing AIRI callers without a hint must
    not send a spurious empty header."""
    src = Path("app/airi/agent.py").read_text()
    tail = src[src.find("async def _call_llm"):]
    # The `if llm_hint:` gate must be present in _call_llm body
    call_llm_body_end = tail.find("\nasync def ", 5)
    body = tail[:call_llm_body_end] if call_llm_body_end > 0 else tail
    assert "if llm_hint:" in body


# ── Version bump ─────────────────────────────────────────────────────

def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 21, 4), (
        f"expected >= 5.21.4, got {major}.{minor}.{patch}"
    )
