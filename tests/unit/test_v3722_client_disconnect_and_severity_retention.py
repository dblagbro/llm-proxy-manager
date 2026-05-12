"""v3.7.22 — ClientDisconnect handler (#253) + severity-tiered activity_log
retention (#254)."""
from __future__ import annotations

from pathlib import Path


# ── #253: ClientDisconnect handler ─────────────────────────────────


def test_main_registers_client_disconnect_handler():
    """Source-level check: ``app.main`` imports ClientDisconnect from
    starlette and registers an exception handler that returns 499."""
    src = Path("app/main.py").read_text()
    assert "from starlette.requests import ClientDisconnect" in src
    assert "@app.exception_handler" in src
    assert "ClientDisconnect" in src
    # 499 is the nginx convention for "client closed request"
    assert "status_code=499" in src


def test_client_disconnect_handler_returns_jsonresponse():
    """The handler must return a JSONResponse so FastAPI emits a clean
    HTTP status instead of letting the exception bubble to a 500."""
    src = Path("app/main.py").read_text()
    # Locate the handler body
    idx = src.index("_handle_client_disconnect")
    body = src[idx:idx + 800]
    assert "JSONResponse" in body
    assert "client_disconnect" in body


def test_client_disconnect_handler_logs_at_debug():
    """The handler must NOT log at warning/error — the client is gone,
    there is nothing actionable, and the line would just be noise."""
    src = Path("app/main.py").read_text()
    idx = src.index("_handle_client_disconnect")
    body = src[idx:idx + 800]
    assert "logger.debug" in body
    # No logger.warning / .error in the handler body
    assert "logger.warning" not in body[:600]
    assert "logger.error" not in body[:600]


# ── #254: severity-tiered activity_log retention ──────────────────


def test_config_has_severity_retention_fields():
    """Settings exposes new warning + error retention fields."""
    from app.config import settings
    assert hasattr(settings, "activity_log_warning_retention_days")
    assert hasattr(settings, "activity_log_error_retention_days")
    # Defaults: warning 365d, error 1825d (5y). info stays at 30d.
    assert settings.activity_log_warning_retention_days == 365
    assert settings.activity_log_error_retention_days == 1825
    assert settings.activity_log_retention_days == 30


def test_prune_has_severity_helper():
    """``_prune_activity_log_by_severity`` exists and takes (severity, keep_days)."""
    from app.monitoring import prune
    assert hasattr(prune, "_prune_activity_log_by_severity")


def test_prune_helpers_for_severity_settings():
    from app.monitoring import prune
    assert prune._warning_retention_days() == 365
    assert prune._error_retention_days() == 1825


def test_prune_helpers_clamp_minimum_to_one_day():
    """Operators can't set retention to 0 or negative — the helpers
    coerce to a 1-day floor so a misconfigured value doesn't wipe the
    table on the next sweep."""
    src = Path("app/monitoring/prune.py").read_text()
    # The clamp pattern is `return max(1, v)` — present for warning + error
    idx = src.index("_warning_retention_days")
    body = src[idx:idx + 500]
    assert "max(1, v)" in body
    idx2 = src.index("_error_retention_days")
    body2 = src[idx2:idx2 + 500]
    assert "max(1, v)" in body2


def test_sweep_calls_severity_helper_three_times():
    """Source-level check: the sweep dispatches info + warning + error
    via the severity-aware helper, not via the old _prune_table call."""
    src = Path("app/monitoring/prune.py").read_text()
    idx = src.index("async def _sweep_once")
    body = src[idx:idx + 4000]
    # Three calls to the new helper, one per severity
    assert body.count("_prune_activity_log_by_severity") >= 3
    assert '"info"' in body
    assert '"warning"' in body
    assert '"error"' in body
    # Each severity records its own count in the output dict
    assert "activity_log_warnings" in body
    assert "activity_log_errors" in body


def test_last_sweep_result_includes_new_fields():
    from app.monitoring.prune import get_last_sweep
    result = get_last_sweep()
    assert "warning_keep_days" in result
    assert "error_keep_days" in result


def test_sweep_log_line_includes_new_counts():
    """The post-sweep INFO log line must include the new severity counts
    so operators tailing logs can see what fired."""
    src = Path("app/monitoring/prune.py").read_text()
    idx = src.index("prune.swept")
    body = src[idx:idx + 800]
    assert "activity_log_warnings" in body
    assert "activity_log_errors" in body
    assert "warning_keep_days" in body
    assert "error_keep_days" in body


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 22)
