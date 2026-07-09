"""v5.20.0 — Refusal detection + prompt hardening + response-tail wire.

Trigger: 2026-07-05 operator report — DevinGPT saw a model refuse a
task and substitute adjacent content (silent task substitution, HTTP
200, valid content, wrong task). Ship: per-API-key opt-in for:
- refusal detection on responses (emits X-Refusal-Detected header)
- prompt hardening (append "if you can't, reply REFUSED:" instruction)
- refusal_retry_enabled column reserved for v5.20.1

This test file pins the shape so v5.20.1 (retry) doesn't accidentally
regress the detection contract.
"""
from __future__ import annotations
from pathlib import Path


# ---- detection patterns ----

def test_explicit_refused_marker_detected():
    from app.refusal_detection import detect_refusal
    m = detect_refusal("REFUSED: I don't reproduce copyrighted lyrics.")
    assert m is not None
    assert m.pattern_name == "explicit_refused_marker"
    assert m.category == "explicit_refused"


def test_task_substitution_detected():
    """The operator's actual case: 'I can't write that song, but here's
    similar lyrics.' Response is HTTP 200 with valid content but wrong task."""
    from app.refusal_detection import detect_refusal
    text = (
        "I can't reproduce those exact lyrics, but here's an original "
        "song in a similar style..."
    )
    m = detect_refusal(text)
    assert m is not None
    assert m.category == "task_substitution"


def test_here_is_original_alternative_detected():
    from app.refusal_detection import detect_refusal
    text = "Sure! Here's an original poem inspired by the theme."
    m = detect_refusal(text)
    assert m is not None
    assert m.category == "task_substitution"


def test_capability_deny_detected():
    from app.refusal_detection import detect_refusal
    text = "As an AI language model, I'm not able to help with that."
    m = detect_refusal(text)
    assert m is not None
    assert m.category == "capability_deny"


def test_non_refusal_returns_none():
    from app.refusal_detection import detect_refusal
    text = "Once upon a time, in a land far away, there lived a dragon."
    assert detect_refusal(text) is None


def test_empty_text_returns_none():
    from app.refusal_detection import detect_refusal
    assert detect_refusal("") is None
    assert detect_refusal(None) is None  # type: ignore[arg-type]


def test_matched_snippet_bounded():
    """Snippet should stay bounded even when the match is embedded in a
    long response. Use a task_substitution pattern (which matches
    anywhere) to trigger the match well inside the string."""
    from app.refusal_detection import detect_refusal
    prefix = "prose about a nice topic. " * 30
    suffix = " and then more prose about various things. " * 30
    text = prefix + "I can't write those exact lyrics, but here's an original song " + suffix
    m = detect_refusal(text)
    assert m is not None
    # Snippet is bounded (radius=40 each side + match text)
    assert len(m.matched_snippet) < 250


# ---- Anthropic-shape extraction ----

def test_extract_text_from_response():
    from app.refusal_detection import extract_text_from_anthropic_response
    resp = {
        "content": [
            {"type": "text", "text": "Hello world."},
            {"type": "tool_use", "name": "foo"},  # skipped
            {"type": "text", "text": "Second block."},
        ]
    }
    out = extract_text_from_anthropic_response(resp)
    assert "Hello world." in out
    assert "Second block." in out
    assert "foo" not in out


def test_extract_handles_bad_shape():
    from app.refusal_detection import extract_text_from_anthropic_response
    assert extract_text_from_anthropic_response(None) == ""
    assert extract_text_from_anthropic_response({}) == ""
    assert extract_text_from_anthropic_response({"content": "not a list"}) == ""


# ---- schema + wire pins ----

def test_apikey_columns_present():
    src = Path("app/models/db_apikey.py").read_text()
    assert "refusal_detection_enabled = Column(Boolean" in src
    assert "refusal_prompt_hardening = Column(Boolean" in src
    assert "refusal_retry_enabled = Column(Boolean" in src


def test_alter_table_statements_present():
    src = Path("app/models/database.py").read_text()
    assert "ADD COLUMN refusal_detection_enabled INTEGER" in src
    assert "ADD COLUMN refusal_prompt_hardening INTEGER" in src
    assert "ADD COLUMN refusal_retry_enabled INTEGER" in src


def test_prompt_hardening_wired_in_messages():
    src = Path("app/api/messages.py").read_text()
    assert "refusal_prompt_hardening" in src
    assert "REFUSAL_HARDENING_INSTRUCTION" in src


def test_response_tail_wires_detection():
    src = Path("app/api/_messages_response_tail.py").read_text()
    assert "refusal_detection_enabled" in src
    assert "X-Refusal-Detected" in src
    assert "detect_refusal" in src
    assert 'event_type="refusal_detected"' in src


def test_response_tail_accepts_anthropic_result():
    src = Path("app/api/_messages_response_tail.py").read_text()
    assert "anthropic_result: Any = None" in src


def test_messages_passes_anthropic_result_to_tail():
    src = Path("app/api/messages.py").read_text()
    # The apply_response_tail call must include anthropic_result= kwarg.
    tail_call = src[src.find("apply_response_tail("):]
    tail_call = tail_call[:tail_call.find(")")]
    assert "anthropic_result=anthropic_result" in tail_call


def test_hardening_instruction_says_refused_marker():
    """The hardening instruction MUST say the model should reply
    with 'REFUSED:' — that's the machine-detectable marker the
    detector uses for explicit_refused_marker pattern."""
    from app.refusal_detection import REFUSAL_HARDENING_INSTRUCTION
    assert "REFUSED:" in REFUSAL_HARDENING_INSTRUCTION


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 20, 0), (
        f"expected >= 5.20.0, got {major}.{minor}.{patch}"
    )
