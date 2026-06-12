"""v5.3.8 — logical-alias routing fix (deterministic opencode EMPTY).

Root cause: ``coordinator-code``/``coordinator-fast`` passed VERBATIM as
``model_override`` into select_provider. No provider has a capability row
matching the alias, so the v3.0.22 capability filter excluded every
SCANNED provider and kept only never-scanned ones ("give it a try") —
on GCP that made an unscanned cursor-oauth provider the sole candidate,
the alias went verbatim to the cursor-bridge, and Cursor's
ERROR_BAD_MODEL_NAME came back HTTP-200-wrapped as an empty completion
(empty content + all-zero usage), which streamed through to opencode as
EMPTY rc=0.

Fix surface:
  1. aliases.is_logical_alias / logical_alias_hint helpers.
  2. _request_pipeline._layer_logical_alias_hint — the LMRH hint the
     v5.0.0 design promised gets layered into the request (caller dims win).
  3. select_provider_with_503 passes model_override=None for logical
     aliases (family/capability filters bypass).
  4. resolve_auto_model_into_body substitutes the route's default model
     for logical aliases, exactly like ``auto``.
  5. _response_validators: shapeless 200-error bodies (no choices, no
     content, error key present) count as empty-success failures;
     ERROR_BAD_MODEL_NAME added to the marker list.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routing.aliases import (
    LOGICAL_ALIASES,
    is_logical_alias,
    logical_alias_hint,
)
from app.api._request_pipeline import (
    _layer_logical_alias_hint,
    resolve_auto_model_into_body,
)
from app.api._response_validators import looks_like_empty_success_failure
from app.routing.lmrh.parse import parse_hint


# ── helpers ──────────────────────────────────────────────────────────────────


def _route(model_id="", default_model="", name="prov"):
    return SimpleNamespace(
        profile=SimpleNamespace(model_id=model_id),
        provider=SimpleNamespace(default_model=default_model, name=name),
    )


# ── 1. alias predicates ──────────────────────────────────────────────────────


def test_is_logical_alias_all_four():
    for alias in ("coordinator-code", "coordinator-fast",
                  "coordinator-reasoning", "coordinator-local"):
        assert is_logical_alias(alias)


def test_is_logical_alias_negatives():
    assert not is_logical_alias("gemini-2.5-flash")
    assert not is_logical_alias("auto")
    assert not is_logical_alias("")
    assert not is_logical_alias(None)


def test_logical_alias_hint_strings():
    for alias, entry in LOGICAL_ALIASES.items():
        assert logical_alias_hint(alias) == entry["hint"]
    assert logical_alias_hint("gpt-4o") is None


def test_every_alias_hint_parses_into_dimensions():
    # The fix only works if each hardcoded hint string actually parses.
    for alias in LOGICAL_ALIASES:
        hint = parse_hint(logical_alias_hint(alias))
        assert hint is not None and hint.dimensions, alias
        assert hint.get("task") is not None, alias


# ── 2. hint layering ─────────────────────────────────────────────────────────


def test_layer_hint_onto_none():
    hint = _layer_logical_alias_hint(None, "coordinator-code")
    assert hint is not None
    assert hint.get("task").value == "code"
    exclude = hint.get("exclude")
    assert exclude.value == "anthropic" and exclude.required


def test_layer_hint_caller_dims_win():
    caller = parse_hint("task=reasoning")
    hint = _layer_logical_alias_hint(caller, "coordinator-code")
    # Caller's task survives; alias only fills missing keys.
    assert hint.get("task").value == "reasoning"
    assert hint.get("exclude").value == "anthropic"
    assert hint.get("cost").value == "standard"


def test_layer_hint_coordinator_fast_latency():
    hint = _layer_logical_alias_hint(None, "coordinator-fast")
    assert hint.get("latency").value == "low"
    assert hint.get("cost").value == "economy"


# ── 3. dispatch substitution ─────────────────────────────────────────────────


def test_logical_alias_substitutes_route_model():
    body = {"model": "coordinator-code", "messages": []}
    out = resolve_auto_model_into_body(
        body, _route(model_id="gemini-2.5-flash"), is_auto=False)
    assert out["model"] == "gemini-2.5-flash"


def test_logical_alias_falls_back_to_provider_default():
    body = {"model": "coordinator-fast", "messages": []}
    out = resolve_auto_model_into_body(
        body, _route(model_id="", default_model="gemini-2.5-flash"),
        is_auto=False)
    assert out["model"] == "gemini-2.5-flash"


def test_logical_alias_502_when_no_default_model():
    body = {"model": "coordinator-code", "messages": []}
    with pytest.raises(HTTPException) as exc:
        resolve_auto_model_into_body(body, _route(), is_auto=False)
    assert exc.value.status_code == 502


def test_real_model_name_untouched():
    body = {"model": "gpt-4o", "messages": []}
    out = resolve_auto_model_into_body(
        body, _route(model_id="something-else"), is_auto=False)
    assert out is body  # identity — no copy, no substitution


def test_auto_still_substitutes():
    body = {"model": "auto", "messages": []}
    out = resolve_auto_model_into_body(
        body, _route(model_id="gpt-5.5"), is_auto=True)
    assert out["model"] == "gpt-5.5"


# ── 4. empty-success guard ───────────────────────────────────────────────────


# Exact body captured from llm-proxy2-cursor-bridge on 2026-06-12 for
# model="coordinator-code" (Cursor answered ERROR_BAD_MODEL_NAME; the
# bridge synthesized this empty-but-well-formed 200).
_BRIDGE_EMPTY_BODY = {
    "id": "chatcmpl-c5624e66-43ab-49a8-bdc9-44aefcf5a53d",
    "object": "chat.completion",
    "model": "coordinator-code",
    "choices": [{"index": 0,
                 "message": {"role": "assistant", "content": ""},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
}


def test_guard_catches_bridge_empty_body():
    # Existing branch A (prompt_tokens == 0) — regression-pin it against
    # the real captured bridge body.
    assert looks_like_empty_success_failure(response_dict=_BRIDGE_EMPTY_BODY)


def test_guard_catches_shapeless_error_body():
    # v5.3.8 — no choices, no content, just an error key (bridge error
    # pass-through shape). Pre-fix this fell through every branch.
    body = {"error": {"code": "resource_exhausted",
                      "details": [{"debug": {"error": "ERROR_BAD_MODEL_NAME"}}]}}
    assert looks_like_empty_success_failure(response_dict=body)


def test_guard_catches_bad_model_name_marker():
    body = {"choices": [{"index": 0,
                         "message": {"role": "assistant", "content": ""},
                         "finish_reason": "stop"}],
            "usage": {}}
    raw = '{"error":{"debug":{"error":"ERROR_BAD_MODEL_NAME"}}}'
    assert looks_like_empty_success_failure(response_dict=body, raw_body=raw)


def test_guard_passes_real_completion():
    body = {"choices": [{"index": 0,
                         "message": {"role": "assistant", "content": "hi"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 1,
                      "total_tokens": 13}}
    assert not looks_like_empty_success_failure(response_dict=body)


def test_guard_passes_shapeless_non_error():
    # e.g. an embeddings-ish or unknown body with no error key — don't flag.
    assert not looks_like_empty_success_failure(
        response_dict={"data": [], "object": "list"})
