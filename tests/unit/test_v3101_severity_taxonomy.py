"""v3.10.1 — activity-log severity taxonomy.

Until v3.10.1 every failed request logged ``severity="warning"``, so a
provider failing 100% of requests was indistinguishable from a routine
rate-limit 429. ``record_outcome``'s failure path now derives severity
from the classified ``error_class``: expected/transient failure modes
(rate_limit / timeout / network) stay ``warning``; operator-actionable
ones (auth / billing / bad_request / upstream_5xx / unknown) emit
``error``.
"""
from __future__ import annotations

from pathlib import Path

from app.monitoring.helpers import severity_for_error_class


# ── the mapping ────────────────────────────────────────────────────


def test_transient_classes_are_warning():
    for cls in ("rate_limit", "timeout", "network"):
        assert severity_for_error_class(cls) == "warning", cls


def test_actionable_classes_are_error():
    for cls in ("auth", "billing", "bad_request", "upstream_5xx", "unknown"):
        assert severity_for_error_class(cls) == "error", cls


def test_none_and_empty_are_error():
    """An unclassified failure must not hide as a warning."""
    assert severity_for_error_class(None) == "error"
    assert severity_for_error_class("") == "error"


def test_unrecognized_class_defaults_to_error():
    """A future error_class we don't know about should surface, not hide."""
    assert severity_for_error_class("some_new_class") == "error"


def test_every_classify_error_output_is_mapped():
    """Cross-check against circuit_breaker.classify_error's documented
    output set — every value it can return must map to a valid
    severity."""
    from app.routing import circuit_breaker  # noqa: F401
    classes = (
        "auth", "billing", "rate_limit", "timeout",
        "network", "upstream_5xx", "bad_request", "unknown",
    )
    for c in classes:
        assert severity_for_error_class(c) in ("warning", "error")


# ── wiring ─────────────────────────────────────────────────────────


def test_record_outcome_failure_path_uses_taxonomy():
    """The failure branch must derive severity from the error class,
    not hardcode ``warning``."""
    src = Path("app/monitoring/helpers.py").read_text()
    # The old hardcoded failure severity must be gone.
    assert 'severity="warning", msg=msg' not in src
    # The failure emit now uses the taxonomy helper.
    assert "severity=severity_for_error_class(meta[\"error_class\"])" in src


def test_success_path_still_info():
    """The success path is unchanged — still severity=info."""
    src = Path("app/monitoring/helpers.py").read_text()
    assert 'severity="info"' in src
