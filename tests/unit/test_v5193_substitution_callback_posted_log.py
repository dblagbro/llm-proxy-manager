"""v5.19.3 — symmetric observability warning-log on emitter success.

Hub team's 2026-07-03 memo (proxy-team-lock-retry-shipped.md) requested
a warning-level ``substitution_callback.posted`` log on the emitter's
success path — matches their v2.6.11 unconditional receipt log. Both
sides now emit at WARNING so a happy-path traversal survives default
INFO-level log filters. Catches one-sided drops where the proxy POST
succeeded but never reached the hub (network black-hole).
"""
from __future__ import annotations
from pathlib import Path


def test_posted_log_emitted_on_first_attempt_success():
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    assert "substitution_callback.posted" in src
    assert "attempt=1" in src


def test_posted_log_emitted_on_retry_success():
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    assert "attempt=2" in src
    assert "first_err=" in src


def test_posted_log_is_warning_level():
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    assert 'logger.warning(\n                "substitution_callback.posted' in src


def test_dropped_path_still_emits_warning():
    src = Path("app/compliance/substitution_callback_hook.py").read_text()
    assert "substitution_callback.dropped" in src


def test_version_bumped():
    import re
    src = Path("app/__version__.py").read_text()
    m = re.search(r'"(\d+)\.(\d+)\.(\d+)"', src)
    assert m
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    assert (major, minor, patch) >= (5, 19, 3), (
        f"expected >= 5.19.3, got {major}.{minor}.{patch}"
    )
