"""v5.0.9 refactor — `_compliance_handler.py` is the single home for the
four compliance orchestration sites that messages.py + completions.py
used to mirror inline.

These tests pin the contract:
- The four exported helper names exist.
- messages.py + completions.py call into the helpers instead of
  inlining the legacy patterns (static-grep: no
  ``detect_client_company(`` or ``refusal_headers_ua(`` import in
  either request handler anymore — those go through the helper).
- The helper handles the v5.0.6 _orig_request_model invariant for
  every audit-write site (signature requires the captured value as a
  parameter; the helper never reads body["model"] itself).
"""
import inspect

import pytest


def test_helper_exports_four_orchestration_functions():
    from app.api import _compliance_handler as h
    for name in (
        "raise_if_banned_client_ua",
        "raise_for_no_substitute_exception",
        "emit_substitution_disclosure_for_route",
        "disclosure_headers_for_upstream_error",
    ):
        assert hasattr(h, name), f"_compliance_handler missing export: {name}"


@pytest.mark.parametrize("module_name", ["app.api.messages", "app.api.completions"])
def test_request_handlers_no_longer_inline_ua_check(module_name):
    """The UA check moved to the helper. Neither file should still call
    ``detect_client_company(...)`` directly OR import
    ``refusal_headers_ua`` from the public compliance package."""
    import importlib
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "detect_client_company(" not in src, (
        f"{module_name} still calls detect_client_company directly — "
        "the v5.0.9 extraction was bypassed"
    )
    assert "refusal_headers_ua" not in src, (
        f"{module_name} still imports refusal_headers_ua — only the "
        "helper should reference it"
    )


@pytest.mark.parametrize("module_name", ["app.api.messages", "app.api.completions"])
def test_request_handlers_no_longer_inline_no_substitute_handling(module_name):
    """The 503 conversion moved to the helper. Neither file should still
    import ``refusal_headers_no_substitute`` or
    ``refusal_headers_no_local`` directly."""
    import importlib
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "refusal_headers_no_substitute" not in src, (
        f"{module_name} still uses refusal_headers_no_substitute inline"
    )
    assert "refusal_headers_no_local" not in src, (
        f"{module_name} still uses refusal_headers_no_local inline"
    )


@pytest.mark.parametrize("module_name", ["app.api.messages", "app.api.completions"])
def test_request_handlers_no_longer_inline_substitution_disclosure(module_name):
    """The 200-OK substitution disclosure moved to the helper. Neither
    file should still import ``build_disclosure_payload`` or
    ``compliance_headers`` directly."""
    import importlib
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    assert "build_disclosure_payload" not in src, (
        f"{module_name} still calls build_disclosure_payload inline"
    )
    # ``compliance_headers`` shows up in the helper import line we
    # don't want in the request handler.
    assert "from app.compliance import (\n        compliance_headers" not in src
    assert "from app.compliance import (\n            compliance_headers" not in src


@pytest.mark.parametrize("module_name", ["app.api.messages", "app.api.completions"])
def test_helper_called_for_orig_request_model(module_name):
    """The helpers must be called with ``_orig_request_model`` as the
    ``orig_request_model`` kwarg — that's the v5.0.6 invariant. A
    refactor that accidentally passes ``body.get("model")`` would
    silently reintroduce the audit-mislabel bug from pre-v5.0.6."""
    import importlib
    mod = importlib.import_module(module_name)
    src = inspect.getsource(mod)
    # Every call site to either of the three helpers that take
    # orig_request_model must use _orig_request_model.
    for needle in (
        "raise_for_no_substitute_exception",
        "emit_substitution_disclosure_for_route",
        "disclosure_headers_for_upstream_error",
    ):
        # Find each call site, scoop the kwargs region, assert.
        idx = 0
        while True:
            idx = src.find(needle + "(", idx)
            if idx < 0:
                break
            # Scoop up to the matching close paren (naive but fine here)
            paren = 1
            j = idx + len(needle) + 1
            while j < len(src) and paren > 0:
                if src[j] == "(":
                    paren += 1
                elif src[j] == ")":
                    paren -= 1
                j += 1
            block = src[idx:j]
            if "orig_request_model" in block:
                assert "_orig_request_model" in block, (
                    f"{module_name}: {needle}(...) call site at offset "
                    f"{idx} doesn't pass _orig_request_model — refactor "
                    f"reintroduced the pre-v5.0.6 audit-mislabel bug"
                )
            idx = j
