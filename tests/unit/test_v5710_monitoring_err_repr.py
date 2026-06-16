"""v5.7.10 — monitoring loops use %r (repr) for exception formatting.

Why: ``str(httpx.TimeoutException())`` is the empty string. With the
prior ``err=%s`` format, exceptions of this kind logged
``ai_provider_supervisor.llm_call_failed err=`` — empty error message,
no class hint, no way to diagnose. ``%r`` gives ``TimeoutException()``
even when ``str()`` is empty.

Pin: every background-loop except handler in app/monitoring/ that
catches a bare ``Exception`` should format the exception with %r so
3am diagnostics show what type fired.
"""
from __future__ import annotations

from pathlib import Path
import pytest


MONITORING_FILES = [
    "app/monitoring/ai_provider_supervisor.py",
    "app/monitoring/ai_rate_limiter.py",
    "app/monitoring/compliance_audit_worker.py",
    "app/monitoring/cursor_oauth_expiry_monitor.py",
]


@pytest.mark.parametrize("path", MONITORING_FILES)
def test_no_str_format_on_exception_in_except_blocks(path):
    """No ``err=%s\", exc`` patterns inside monitoring loops — must be
    ``err=%r\", exc`` so empty-str exception classes still log usefully."""
    src = Path(path).read_text()
    # Crude but effective: scan for ``err=%s"`` paired with exc on the
    # same logger.warning call. False-positive on user-supplied strings
    # is fine; this is a monitoring-loop file.
    bad_pattern = 'err=%s", exc'
    assert bad_pattern not in src, (
        f"{path} still contains ``err=%s\", exc`` — switch to ``%r`` "
        "so empty-str exceptions log a class name."
    )


def test_repr_renders_empty_str_exception_with_class_name():
    """Sanity: this is the WHY for the file. ``str()`` on an exception
    with no args is empty; ``repr()`` shows the class."""
    import httpx
    exc = httpx.TimeoutException("")  # explicit empty message
    assert str(exc) == ""              # the production observation
    rendered = "%r" % exc
    assert "TimeoutException" in rendered
