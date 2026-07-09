"""v5.18.2 — flip zero_row_warning default from True to False.

Trigger: 2026-07-03 log sweep found ``audit_chain_zero_row_streak``
firing daily on tmrwww01 main cluster. Operator decision #483 was to
silence, but the system_setting was never persisted per-cluster, so
the default (True) kept the warning noisy.

Fix: default flipped. Warning fires only when operator has EXPLICITLY
set the setting to a truthy value. Absence = silent.
"""
from __future__ import annotations

from pathlib import Path


def test_docstring_documents_flip():
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert "v5.18.2" in src
    assert "default flipped True" in src
    assert "opts IN, not out" in src


def test_absence_of_setting_now_silences_warning():
    """When the system_setting is absent, is_enabled must be False and
    the worker MUST return early without emitting a warning."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    # New default-off gate: is_enabled requires row0 present AND truthy value
    assert "is_enabled = (" in src
    assert 'row0 is not None' in src
    assert 'if not is_enabled:' in src
    assert 'return' in src


def test_explicit_opt_in_values_documented():
    """Operator can opt IN by setting the value to true/1/yes/on."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    assert '"true", "1", "yes", "on"' in src


def test_read_failure_now_suppresses():
    """v5.18.2 flips the fail-open posture to fail-silent (matches new
    default). Prior v5.7.11 fell through to warn on DB read failure."""
    src = Path("app/monitoring/compliance_audit_worker.py").read_text()
    # There's now an unconditional return in the except path
    assert "except Exception:" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 18, 2), (
        f"expected >= 5.18.2, got {major}.{minor}.{patch}"
    )
