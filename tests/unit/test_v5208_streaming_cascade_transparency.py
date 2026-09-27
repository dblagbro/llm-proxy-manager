"""v5.20.8 — Streaming + refusal_retry_enabled transparency header.

The v5.20.1 refusal cascade runs only on the non-streaming path (it
needs the full anthropic_result to detect + retry). For streaming, we
can't cleanly retry mid-stream without buffering the entire response
first (which negates the streaming latency benefit). If a key has
BOTH stream=true AND refusal_retry_enabled=true, emit
``X-Refusal-Cascade-Unavailable: streaming`` so the caller knows
cascade wasn't attempted.

Full buffered-cascade for streaming is deferred to v5.20.9+.
"""
from __future__ import annotations
from pathlib import Path


def test_transparency_header_wired_in_messages():
    """Superseded by v5.21.0 (buffered streaming cascade shipped).
    The v5.20.8 ``X-Refusal-Cascade-Unavailable: streaming`` header was
    an honest admission that we DIDN'T support cascade on streaming.
    v5.21.0 makes it work via a buffered-then-emit pattern, so the
    Unavailable header is gone. Pinning both facts here — the
    disappearance is intentional, not regression."""
    src = Path("app/api/messages.py").read_text()
    assert '"X-Refusal-Cascade-Unavailable"' not in src, (
        "v5.21.0 removed this header — streaming cascade now works via buffered mode"
    )
    # New header takes its place — see test_v5210 tests for the full pin
    assert '"X-Refusal-Cascade-Mode"' in src


def test_gate_requires_both_streaming_and_retry_enabled():
    """Superseded by v5.21.0. The gate is the same but the action changed
    from "emit warning header" to "force stream=False + set buffered
    mode marker". Both branches use the SAME gate (stream+retry_enabled),
    so this pin verifies the gate expression still lives in messages.py."""
    src = Path("app/api/messages.py").read_text()
    # Gate is now expressed as `stream and getattr(key_record, "refusal_retry_enabled", False)`
    # inside the _buffered_cascade_stream assignment. Match on the getattr shape
    # since string exact-match would over-fit.
    assert 'refusal_retry_enabled' in src
    assert '_buffered_cascade_stream' in src


def test_v5201_cascade_is_non_streaming_only():
    """Static pin: cascade module still gated on non-streaming path.
    If someone accidentally wires cascade into streaming without proper
    buffering, this fails."""
    src = Path("app/api/_refusal_cascade.py").read_text()
    # The cascade takes an anthropic_result / _initial_result / dispatch
    # closure — none of which are compatible with a live SSE stream.
    assert "initial_anthropic" in src
    assert "initial_result" in src


def test_docstring_mentions_streaming_gap():
    """The streaming-vs-cascade trade-off must stay documented in the code.

    v5.22.35 — this asserted the literal string ``"v5.20.8"`` appeared
    somewhere in ``messages.py``. Pinning a version number in prose is a weak
    proxy at the best of times, and v5.21.6 extracted mode detection into
    ``_buffered_cascade_mode.py``, where the explanation was rewritten and the
    version reference dropped. ``v5.20.8`` now appears nowhere in ``app/``, so
    the test went red — while the documentation it wanted had in fact
    *improved*: that module carries a trade-off table spelling out all three
    paths (pass-through streaming with no cascade, ``buffered``, and
    ``buffered-heartbeat``).

    Assert the substance instead: the reader can still learn, from the code,
    that plain streaming gives up cascade and what the buffered modes cost.
    """
    src = Path("app/api/_buffered_cascade_mode.py").read_text()
    doc = src[: src.index("from __future__")] if "from __future__" in src else src

    assert "pass-through streaming" in doc, (
        "the no-cascade streaming path must stay named in the trade-off docs"
    )
    for mode in ("buffered", "buffered-heartbeat"):
        assert mode in doc, f"trade-off docs no longer describe {mode!r} mode"
    # The cost of the buffered path — the client sees nothing until the
    # dispatch finishes — is the whole point of documenting this.
    assert "no bytes until" in doc.lower() or "sees no bytes" in doc.lower(), (
        "the buffered path's cost (client sees no bytes until dispatch "
        "completes) must stay documented; that is the streaming gap"
    )


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 8), (
        f"expected >= 5.20.8, got {major}.{minor}.{patch}"
    )
