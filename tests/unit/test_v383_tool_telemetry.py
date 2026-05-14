"""v3.8.3 — tool-call telemetry in activity_log + Grok-Web native_tools=False.

Adds tool_calls_emitted / tool_call_format / tool_call_validated to
event_meta on llm_request rows so operator can audit per-(provider,
model) tool-call performance over time. Also backfills Grok-Web
ModelCapability rows to native_tools=False since the bridge is a
screen-scraped chat, not a function-calling API.

Phases 4 and 5 (probe worker + router weighting) consume the data
this phase produces.
"""
from __future__ import annotations

from pathlib import Path


# ── _extract_tool_call_stats helper ───────────────────────────────


def test_extract_anthropic_shape_counts_tool_use_blocks():
    from app.monitoring.helpers import _extract_tool_call_stats
    body = {
        "content": [
            {"type": "text", "text": "I'll check the weather."},
            {"type": "tool_use", "name": "get_weather", "input": {"city": "SF"}},
            {"type": "tool_use", "name": "get_time", "input": {"tz": "PT"}},
        ],
    }
    count, valid = _extract_tool_call_stats(body)
    assert count == 2
    assert valid is True


def test_extract_anthropic_invalid_when_input_not_dict():
    from app.monitoring.helpers import _extract_tool_call_stats
    body = {
        "content": [
            {"type": "tool_use", "name": "broken", "input": "not a dict"},
        ],
    }
    count, valid = _extract_tool_call_stats(body)
    assert count == 1
    assert valid is False


def test_extract_anthropic_invalid_when_name_missing():
    from app.monitoring.helpers import _extract_tool_call_stats
    body = {"content": [{"type": "tool_use", "input": {}}]}
    count, valid = _extract_tool_call_stats(body)
    assert count == 1
    assert valid is False


def test_extract_openai_shape_counts_tool_calls():
    from app.monitoring.helpers import _extract_tool_call_stats
    body = {
        "choices": [{
            "message": {
                "tool_calls": [
                    {"function": {"name": "get_weather", "arguments": '{"city":"SF"}'}},
                    {"function": {"name": "get_time", "arguments": '{"tz":"PT"}'}},
                ],
            },
        }],
    }
    count, valid = _extract_tool_call_stats(body)
    assert count == 2
    assert valid is True


def test_extract_openai_invalid_when_arguments_not_json():
    from app.monitoring.helpers import _extract_tool_call_stats
    body = {
        "choices": [{
            "message": {
                "tool_calls": [
                    {"function": {"name": "broken", "arguments": "not json"}},
                ],
            },
        }],
    }
    count, valid = _extract_tool_call_stats(body)
    assert count == 1
    assert valid is False


def test_extract_no_tool_calls_returns_zero_valid_true():
    """A response with no tool calls is trivially valid (validated=True)
    — the field is only meaningful when count > 0."""
    from app.monitoring.helpers import _extract_tool_call_stats
    # Anthropic shape — just text
    assert _extract_tool_call_stats({"content": [{"type": "text", "text": "hi"}]}) == (0, True)
    # OpenAI shape — no tool_calls
    assert _extract_tool_call_stats({"choices": [{"message": {"content": "hi"}}]}) == (0, True)
    # Empty / non-dict
    assert _extract_tool_call_stats({}) == (0, True)
    assert _extract_tool_call_stats(None) == (0, True)


# ── record_outcome signature ──────────────────────────────────────


def test_record_outcome_accepts_tool_call_format_param():
    """The new param must be optional (no breaking change) AND default
    to None so callers that don't pass it produce no tool telemetry."""
    import inspect
    from app.monitoring.helpers import record_outcome
    sig = inspect.signature(record_outcome)
    assert "tool_call_format" in sig.parameters
    assert sig.parameters["tool_call_format"].default is None


def test_record_outcome_only_stamps_meta_when_format_set():
    """Source-level check: the meta-stamping block is gated on
    ``if tool_call_format is not None:``. Non-tool requests stay lean."""
    src = Path("app/monitoring/helpers.py").read_text()
    assert "if tool_call_format is not None:" in src
    # Three fields stamped together
    idx = src.index("if tool_call_format is not None:")
    block = src[idx:idx + 800]
    assert 'meta["tool_call_format"]' in block
    assert 'meta["tool_calls_emitted"]' in block
    assert 'meta["tool_call_validated"]' in block


# ── Call-site wiring ──────────────────────────────────────────────


def test_messages_native_path_passes_tool_call_format():
    src = Path("app/api/messages.py").read_text()
    # In the native success-branch record_outcome, has_tools drives the
    # format string
    assert 'tool_call_format=("native" if has_tools else None)' in src


def test_messages_emulated_path_passes_tool_call_format():
    src = Path("app/api/messages.py").read_text()
    assert 'tool_call_format="emulated"' in src


def test_completions_native_path_passes_tool_call_format():
    src = Path("app/api/completions.py").read_text()
    assert 'tool_call_format=("native" if has_tools else None)' in src


def test_completions_emulated_path_passes_tool_call_format():
    src = Path("app/api/completions.py").read_text()
    assert 'tool_call_format="emulated"' in src


# ── Grok-Web backfill migration ───────────────────────────────────


def test_grok_web_native_tools_backfill_migration_present():
    src = Path("app/models/database.py").read_text()
    assert (
        "UPDATE model_capabilities SET native_tools=0 "
        "WHERE provider_id IN (SELECT id FROM providers WHERE provider_type='grok-web')"
        in src
    )


def test_grok_web_backfill_safe_to_rerun():
    """The migration sets native_tools=0 unconditionally on Grok-Web
    rows. Re-running is a no-op when all rows are already 0. Verify
    by inspecting the WHERE clause — it's scoped to provider_type
    only (no DELETE / no schema change)."""
    src = Path("app/models/database.py").read_text()
    idx = src.index("UPDATE model_capabilities SET native_tools=0")
    stmt = src[idx:idx + 300]
    assert "DELETE" not in stmt.upper()
    assert "provider_type='grok-web'" in stmt


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 8, 3)
