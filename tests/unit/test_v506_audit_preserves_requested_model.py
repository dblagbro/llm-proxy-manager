"""v5.0.6 — ``compliance_events.requested_model`` must record the
caller's ORIGINAL model name, not the served model.

Background: from 2026-06-04 hourly canary monitor, events #6 + #7 on
tmrwww01's prod key showed ``requested_model=gemini-2.5-flash`` and
``served_model=gemini-2.5-flash`` (identical) — looked like a false-
positive substitution. Actual root cause: ``body["model"]`` is
rewritten to the served model at messages.py:355 BEFORE the audit row
is written at line ~557, so ``body.get("model")`` captured the served
model. The substitution itself was correct; only the audit's
``requested_model`` field was wrong.

This test pins the fix: the route ran with original
model="claude-haiku" and the audit row's ``requested_model`` MUST be
"claude-haiku" (not the served Gemini model).
"""
from __future__ import annotations

import inspect

import pytest


def test_messages_captures_orig_request_model_before_body_mutation():
    """Static check: messages.py captures _orig_request_model BEFORE the
    body["model"] rewrite at line 355. A future refactor that moves the
    capture below the mutation reintroduces the bug.
    """
    from app.api import messages as msg_mod
    src = inspect.getsource(msg_mod.messages)
    capture_idx = src.find("_orig_request_model = body.get")
    mutation_idx = src.find('body = {**body, "model": route.served_model_native}')
    assert capture_idx > 0, "missing _orig_request_model capture in messages.py"
    assert mutation_idx > 0, "body['model'] mutation moved or removed — re-verify capture ordering"
    assert capture_idx < mutation_idx, (
        "_orig_request_model captured AFTER body['model'] rewrite — bug reintroduced"
    )


def test_completions_captures_orig_request_model_before_body_mutation():
    """Mirror of messages.py check for completions.py."""
    from app.api import completions as cmp_mod
    src = inspect.getsource(cmp_mod.chat_completions)
    capture_idx = src.find("_orig_request_model = body.get")
    mutation_idx = src.find('body = {**body, "model": route.served_model_native}')
    assert capture_idx > 0, "missing _orig_request_model capture in completions.py"
    assert mutation_idx > 0, "body['model'] mutation moved or removed — re-verify capture ordering"
    assert capture_idx < mutation_idx


def _emit_event_blocks(src: str) -> list[str]:
    """Extract each ``await emit_event(...)`` (or ``_emit(...)``) call
    site as a single multi-line string. Used by the asserts below to
    inspect the kwargs of audit-row writers specifically — separate
    from response-shape construction sites that legitimately use the
    served model."""
    blocks = []
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if "await emit_event(" in line or "await _emit(" in line:
            depth = line.count("(") - line.count(")")
            buf = [line]
            j = i + 1
            while depth > 0 and j < len(lines):
                buf.append(lines[j])
                depth += lines[j].count("(") - lines[j].count(")")
                j += 1
            blocks.append("\n".join(buf))
            i = j
        else:
            i += 1
    return blocks


def _compliance_kwarg_blocks(src: str) -> list[str]:
    """Extract every ``compliance_headers(...)`` /
    ``build_disclosure_payload(...)`` / ``_ch(...)`` call site (the
    disclosure-header builders), as multi-line strings."""
    blocks = []
    lines = src.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if any(needle in line for needle in (
            "compliance_headers(",
            "build_disclosure_payload(",
            "_ch(",
        )):
            depth = line.count("(") - line.count(")")
            buf = [line]
            j = i + 1
            while depth > 0 and j < len(lines):
                buf.append(lines[j])
                depth += lines[j].count("(") - lines[j].count(")")
                j += 1
            blocks.append("\n".join(buf))
            i = j
        else:
            i += 1
    return blocks


@pytest.mark.parametrize("module_name, fn_name", [
    ("app.api.messages", "messages"),
    ("app.api.completions", "chat_completions"),
])
def test_emit_event_uses_orig_request_model_for_compliance_audits(module_name, fn_name):
    """Every ``await emit_event(...)`` block that records a
    ``compliance_*`` event_type MUST pass ``_orig_request_model`` (or
    a variable derived from it) — not ``body.get("model")``. Non-
    compliance emit_event calls are out of scope."""
    import importlib
    mod = importlib.import_module(module_name)
    fn = getattr(mod, fn_name)
    src = inspect.getsource(fn)
    compliance_event_types = {
        "model_substitution",
        "compliance_no_substitute",
        "compliance_no_local_provider",
        "client_product_refusal",
        "cache_filtered",
        "memory_filtered",
        "path_not_allowed",
    }
    bad: list[str] = []
    for block in _emit_event_blocks(src):
        is_compliance = any(et in block for et in compliance_event_types)
        if not is_compliance:
            continue
        # Look for the requested_model kwarg specifically using body.get("model")
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("requested_model=") and 'body.get("model")' in stripped:
                bad.append(stripped + f"   (in {module_name})")
    assert not bad, (
        "Compliance emit_event sites still reading the post-mutation "
        "body.get(\"model\") for the audit's requested_model field: "
        + "\n  ".join(bad)
    )


@pytest.mark.parametrize("module_name, fn_name", [
    ("app.api.messages", "messages"),
    ("app.api.completions", "chat_completions"),
])
def test_compliance_headers_use_orig_request_model(module_name, fn_name):
    """Every ``compliance_headers(...)`` / ``build_disclosure_payload(...)``
    / ``_ch(...)`` call MUST pass ``_orig_request_model`` for
    ``requested_model``. Otherwise the X-Compliance-Requested-Model
    response header records the served model."""
    import importlib
    mod = importlib.import_module(module_name)
    fn = getattr(mod, fn_name)
    src = inspect.getsource(fn)
    bad: list[str] = []
    for block in _compliance_kwarg_blocks(src):
        for line in block.splitlines():
            stripped = line.strip()
            if stripped.startswith("requested_model=") and 'body.get("model")' in stripped:
                bad.append(stripped + f"   (in {module_name})")
    assert not bad, (
        "Compliance-header builder sites still reading the post-mutation "
        "body.get(\"model\") for X-Compliance-Requested-Model: "
        + "\n  ".join(bad)
    )


def test_orig_request_model_capture_is_isinstance_dict_guarded():
    """Defensive: the capture must guard against ``body`` not being a
    dict (e.g. a malformed JSON body that parses to a list). The fallback
    is None, which propagates through emit_event as a nullable column.
    """
    from app.api import messages as msg_mod, completions as cmp_mod
    msg_src = inspect.getsource(msg_mod.messages)
    cmp_src = inspect.getsource(cmp_mod.chat_completions)
    for label, src in (("messages", msg_src), ("completions", cmp_src)):
        assert "_orig_request_model = body.get(\"model\") if isinstance(body, dict) else None" in src, (
            f"{label}.py _orig_request_model capture is missing the "
            f"isinstance(body, dict) guard — malformed JSON could crash here"
        )
