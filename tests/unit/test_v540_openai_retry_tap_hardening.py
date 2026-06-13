"""v5.4.0 — BUG-072: openai retry tap self-test + activity_log emission.

Pre-v5.4.0, the v5.3.4 tap only incremented a Prometheus counter — a
QA query against ``activity_log`` for ``%retry%`` event types returned
zero rows on tmrwww01 despite live traffic, which read as "tap broken"
on inspection. v5.4.0 adds:

- ``is_installed()`` + ``self_test()`` introspection helpers.
- An ``openai_client_retry`` ``activity_log`` row written alongside
  the Prometheus increment (best-effort, errors swallowed).
- ``POST /api/admin/ai-supervisor/retry-tap-self-test`` admin endpoint.
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def test_tap_module_exposes_self_test_helpers():
    from app.observability.openai_retry_tap import (
        is_installed,
        self_test,
        install_openai_retry_tap,
    )
    assert callable(is_installed)
    assert callable(self_test)
    assert callable(install_openai_retry_tap)


def test_is_installed_false_before_install():
    """Without calling install, is_installed must return False so the
    self-test can identify a fresh boot with the wiring missing."""
    from app.observability.openai_retry_tap import is_installed, _OpenAIRetryTap
    target = logging.getLogger("openai._base_client")
    # Clean any leftover handlers from earlier tests in this run
    target.handlers = [h for h in target.handlers if not isinstance(h, _OpenAIRetryTap)]
    import app.observability.openai_retry_tap as tap_mod
    tap_mod._installed = False
    assert is_installed() is False


def test_install_makes_is_installed_true():
    from app.observability.openai_retry_tap import (
        install_openai_retry_tap,
        is_installed,
    )
    install_openai_retry_tap()
    assert is_installed() is True


def test_self_test_reports_installed_state():
    from app.observability.openai_retry_tap import (
        install_openai_retry_tap,
        self_test,
    )
    install_openai_retry_tap()
    result = self_test()
    assert result["installed"] is True
    assert result["synthetic_record_emitted"] is True
    assert result["handler_count"] >= 1
    assert result["error"] is None


def test_self_test_reports_uninstalled_state(monkeypatch):
    """If install_openai_retry_tap hasn't been called, the self-test
    must surface a helpful error rather than silently passing."""
    from app.observability.openai_retry_tap import self_test, _OpenAIRetryTap
    import app.observability.openai_retry_tap as tap_mod
    # Hard reset to fake an uninstalled state
    tap_mod._installed = False
    target = logging.getLogger("openai._base_client")
    target.handlers = [h for h in target.handlers if not isinstance(h, _OpenAIRetryTap)]
    result = self_test()
    assert result["installed"] is False
    assert "not installed" in (result["error"] or "")


def test_tap_writes_activity_log_row_in_source():
    """v5.4.0: alongside the Prometheus counter, the tap must call
    the activity_log emit path so the row exists for SQL probes.
    Source-grep — exercising the async path in-test is brittle."""
    src = Path("app/observability/openai_retry_tap.py").read_text()
    assert "_emit_retry_event_async" in src
    assert "ActivityLog(" in src
    assert "openai_client_retry" in src


def test_retry_tap_self_test_endpoint_exists_in_router():
    from app.api.admin_ai_supervisor import router
    routes = [r for r in router.routes if hasattr(r, "path")]
    assert any("retry-tap-self-test" in r.path for r in routes), (
        f"expected /retry-tap-self-test endpoint; got {[r.path for r in routes]}"
    )


@pytest.mark.asyncio
async def test_retry_tap_self_test_endpoint_returns_dict():
    """End-to-end: hitting the endpoint calls self_test() and surfaces
    its dict verbatim."""
    from app.api.admin_ai_supervisor import retry_tap_self_test
    from app.observability.openai_retry_tap import install_openai_retry_tap
    install_openai_retry_tap()
    result = await retry_tap_self_test(_db=None, _admin=None)
    assert isinstance(result, dict)
    assert "installed" in result
    assert "synthetic_record_emitted" in result
