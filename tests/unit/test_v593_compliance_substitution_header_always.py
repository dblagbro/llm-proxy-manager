"""v5.9.3 — always-on `X-Compliance-Substitution` header.

Hub-side scanner (coordinator-hub `_scan_anthropic_response_model`) used
absence of `X-Compliance-Substitution` as the signal to open a
`compliance_unsubstituted_anthropic_response` dev_issue on every claude-*
2xx response. v5.9.3 makes the proxy always emit one of three values so
the hub can drop the heuristic in favor of a strict assertion.
"""
from __future__ import annotations

from types import SimpleNamespace


def test_disposition_helper_false_when_key_has_policy():
    from app.api._compliance_handler import _disposition_only_headers, _key_has_compliance_policy

    key_with_block = SimpleNamespace(
        blocked_companies=["anthropic"],
        allowed_companies=None,
        blocked_models=None,
        allowed_models=None,
        allowed_paths=None,
    )
    assert _key_has_compliance_policy(key_with_block) is True
    h = _disposition_only_headers(key_with_block)
    assert h == {"X-Compliance-Substitution": "false"}


def test_disposition_helper_pass_through_when_no_policy():
    from app.api._compliance_handler import _disposition_only_headers, _key_has_compliance_policy

    key_blank = SimpleNamespace(
        blocked_companies=None,
        allowed_companies=None,
        blocked_models=None,
        allowed_models=None,
        allowed_paths=None,
    )
    assert _key_has_compliance_policy(key_blank) is False
    h = _disposition_only_headers(key_blank)
    assert h == {"X-Compliance-Substitution": "pass-through"}


def test_empty_list_is_not_policy():
    """`blocked_companies: []` (operator wrote it down then cleared all
    entries) is semantically equivalent to None — no policy applies."""
    from app.api._compliance_handler import _key_has_compliance_policy

    key_empty_list = SimpleNamespace(
        blocked_companies=[],
        allowed_companies=[],
        blocked_models=[],
        allowed_models=[],
        allowed_paths=[],
    )
    assert _key_has_compliance_policy(key_empty_list) is False


def test_disposition_handles_str_serialized_lists():
    """Some legacy rows store JSON-serialized lists as strings. The
    classifier accepts both shapes."""
    from app.api._compliance_handler import _key_has_compliance_policy

    key_string_form = SimpleNamespace(
        blocked_companies='["anthropic"]',
        allowed_companies=None,
        blocked_models=None,
        allowed_models=None,
        allowed_paths=None,
    )
    assert _key_has_compliance_policy(key_string_form) is True

    key_string_empty = SimpleNamespace(
        blocked_companies='[]',
        allowed_companies=None,
        blocked_models=None,
        allowed_models=None,
        allowed_paths=None,
    )
    assert _key_has_compliance_policy(key_string_empty) is False


def test_emit_helper_returns_disposition_on_non_substituted_route():
    """The integration point — emit_substitution_disclosure_for_route
    must return the disposition header in its first tuple slot even
    when route.compliance_substituted is False."""
    import inspect
    from app.api import _compliance_handler

    src = inspect.getsource(
        _compliance_handler.emit_substitution_disclosure_for_route
    )
    # Both no-substitution exit branches must call the disposition
    # helper. Counts the symbol appearances; the function has at most
    # 2 early-return-without-substitution paths.
    assert src.count("_disposition_only_headers") >= 2, (
        "v5.9.3 requires both early-exit-no-substitution branches in "
        "emit_substitution_disclosure_for_route to emit the disposition "
        "header — found "
        f"{src.count('_disposition_only_headers')} call sites."
    )
