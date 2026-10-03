"""v5.22.41 (BUG-095) — token accounting was lost on the tool-emulation path.

The operator reported that "the pricing estimates in llm-proxy are low". This is
one mechanism for that, and it is not a rounding error — it is a hard zero.

Chain:

1. ``call_with_tool_prompt`` made the upstream call and returned only
   ``choice.message.content``, discarding ``resp.usage``.
2. The six emulated-response builders in ``app/cot/sse.py`` therefore filled in
   ``usage`` with hardcoded zeros.
3. Any cost computed from those numbers is $0.

The path is far more common than its name suggests. ``tool_emulation`` engages
when ``has_tools and not provider.native_tools`` — and the proxy **injects its
own MCP tools**, so ``has_tools`` is true even for a caller that sent none.
Measured end-to-end on a plain text request with no tools and an
OpenAI-compatible provider:

    has_tools=True ntools=3 injected=True emul=True native_tools=False
    -> usage {"input_tokens": 0, "output_tokens": 0}

while the upstream had reported ``prompt_tokens=10, completion_tokens=2``.

So: any key with tool injection enabled, talking to a provider without native
tool support, on either ``/v1/messages`` or ``/v1/chat/completions``, reported
zero tokens and zero cost.
"""
from __future__ import annotations

import inspect

import pytest

from app.cot import sse

ANTHROPIC_BUILDERS = (
    ("anthropic_text_response", ("hello", "m")),
    ("anthropic_tool_response", ("f", {}, "m")),
    ("anthropic_tools_response", ([{"name": "f", "input": {}}], "m")),
)
OPENAI_BUILDERS = (
    ("openai_text_response", ("hello", "m")),
    ("openai_tool_response", ("f", {}, "m")),
    ("openai_tools_response", ([{"name": "f", "input": {}}], "m")),
)
USAGE = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}


@pytest.mark.parametrize("name,args", ANTHROPIC_BUILDERS + OPENAI_BUILDERS)
def test_builder_accepts_usage(name, args):
    """Every emulated-response builder must be able to carry real usage."""
    fn = getattr(sse, name)
    assert "usage" in inspect.signature(fn).parameters, (
        f"{name} cannot accept usage, so anything it builds will report zero "
        f"tokens and zero cost"
    )
    fn(*args, usage=USAGE)  # must not raise


@pytest.mark.parametrize("name,args", ANTHROPIC_BUILDERS)
def test_anthropic_builder_reports_real_tokens(name, args):
    out = getattr(sse, name)(*args, usage=USAGE)
    assert out["usage"]["input_tokens"] == 10, out["usage"]
    assert out["usage"]["output_tokens"] == 2, out["usage"]


@pytest.mark.parametrize("name,args", OPENAI_BUILDERS)
def test_openai_builder_reports_real_tokens(name, args):
    out = getattr(sse, name)(*args, usage=USAGE)
    assert out["usage"]["prompt_tokens"] == 10, out["usage"]
    assert out["usage"]["completion_tokens"] == 2, out["usage"]
    assert out["usage"]["total_tokens"] == 12, out["usage"]


@pytest.mark.parametrize("name,args", ANTHROPIC_BUILDERS + OPENAI_BUILDERS)
def test_builder_still_defaults_to_zero_without_usage(name, args):
    """Back-compat: a caller that cannot supply usage behaves as before.

    Kept deliberately — the default is what made the bug invisible, so the
    guarantee worth pinning is that supplying usage *works*, not that omitting
    it raises.
    """
    out = getattr(sse, name)(*args)
    assert "usage" in out


def test_call_with_tool_prompt_can_surface_usage():
    """The source of the loss: it used to discard ``resp.usage`` entirely."""
    from app.cot.tool_emulation import call_with_tool_prompt

    params = inspect.signature(call_with_tool_prompt).parameters
    assert "usage_out" in params, (
        "call_with_tool_prompt must be able to hand the upstream usage back, or "
        "the emulation path has nothing real to report"
    )
    assert params["usage_out"].default is None, (
        "usage_out must be optional so existing callers are unaffected"
    )


# endpoint -> (json_builders, sse_generators) that must each carry usage.
# messages.py also fixes the streaming path: its SSE generators previously either
# omitted usage from the message_delta frame or emitted a fabricated
# "output_tokens":10. completions.py's OpenAI-shape SSE generators have no usage
# frame to populate, so only its three JSON builders are threaded.
_THREADED = {
    "app/api/messages.py": (3, 3),
    "app/api/completions.py": (3, 0),
}


@pytest.mark.parametrize("endpoint", sorted(_THREADED))
def test_endpoint_threads_usage_into_its_builders(endpoint):
    """Both wire shapes must collect the usage and pass it on.

    Source-level because the emulation path needs a live upstream to exercise;
    the end-to-end behaviour is verified against the mock provider in the
    integration suite (tests/integration/test_routing_mock.py).
    """
    from pathlib import Path

    src = Path(endpoint).read_text()
    assert "usage_out=_emul_usage" in src, (
        f"{endpoint} does not capture the emulated upstream's usage"
    )
    want_json, want_sse = _THREADED[endpoint]
    got = src.count("usage=_emul_usage")
    assert got == want_json + want_sse, (
        f"{endpoint} threads usage into {got} sites; expected "
        f"{want_json + want_sse} ({want_json} JSON builders + {want_sse} SSE "
        f"generators). A site left out reports zero tokens for that shape."
    )
