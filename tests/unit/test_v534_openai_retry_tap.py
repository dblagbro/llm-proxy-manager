"""v5.3.4 — Ship A + B: openai-python retry tap.

Verifies:
- Tap module + counter + install hook exist.
- Tap handler increments the counter on a "Retrying request" INFO line.
- Tap parses the endpoint from the message.
- Non-retry INFO lines from the same logger are NOT counted.
- install() is idempotent.
- install() sets propagate=False so the retry chatter stops bubbling
  to the root logger (Ship B).
- The handler swallows exceptions (logging handlers must not raise).
- Counter increments on the tap's own logger AND when emitted via
  the standard logging API (covers the production code path where
  openai-python's logger emits the record).
"""
from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_openai_tap_state():
    """Tests in this file mutate the openai._base_client logger's
    handler list + propagate flag + the tap module's _installed sentinel.
    Reset all three between tests so we don't stack handlers and
    overcount in the end-to-end test."""
    from app.observability import openai_retry_tap as tap_mod
    target = logging.getLogger("openai._base_client")
    saved_handlers = list(target.handlers)
    saved_propagate = target.propagate
    saved_level = target.level
    target.handlers = [h for h in target.handlers
                       if not isinstance(h, tap_mod._OpenAIRetryTap)]
    tap_mod._installed = False
    yield
    target.handlers = saved_handlers
    target.propagate = saved_propagate
    target.level = saved_level
    tap_mod._installed = False


# ── Pins ────────────────────────────────────────────────────────────


def test_tap_module_exists():
    from app.observability.openai_retry_tap import (
        _OpenAIRetryTap, install_openai_retry_tap,
    )
    assert issubclass(_OpenAIRetryTap, logging.Handler)
    assert callable(install_openai_retry_tap)


def test_counter_exported():
    """The counter + observer helper are importable. Using a private
    `_labelnames` attribute would fail under the test-suite's Noop
    prometheus shim; instead verify the public observer accepts the
    label without raising."""
    from app.observability.prometheus import OPENAI_RETRIES_TOTAL, observe_openai_retry
    assert OPENAI_RETRIES_TOTAL is not None
    assert callable(observe_openai_retry)
    # Must accept the endpoint kwarg shape we ship.
    observe_openai_retry("/chat/completions")
    observe_openai_retry("other")


def test_main_py_installs_the_tap():
    src = Path("app/main.py").read_text()
    assert "install_openai_retry_tap" in src
    assert "from app.observability.openai_retry_tap import install_openai_retry_tap" in src


# ── Behavioral ──────────────────────────────────────────────────────


def _make_record(msg: str, logger_name: str = "openai._base_client") -> logging.LogRecord:
    return logging.LogRecord(
        name=logger_name, level=logging.INFO,
        pathname=__file__, lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def test_handler_increments_on_retry_message():
    from app.observability.openai_retry_tap import _OpenAIRetryTap
    tap = _OpenAIRetryTap()
    with patch("app.observability.prometheus.observe_openai_retry") as m:
        tap.emit(_make_record("Retrying request to /chat/completions in 0.381504 seconds"))
        m.assert_called_once_with("/chat/completions")


def test_handler_extracts_alt_endpoint():
    from app.observability.openai_retry_tap import _OpenAIRetryTap
    tap = _OpenAIRetryTap()
    with patch("app.observability.prometheus.observe_openai_retry") as m:
        tap.emit(_make_record("Retrying request to /messages in 1.234 seconds"))
        m.assert_called_once_with("/messages")


def test_handler_ignores_non_retry_lines():
    """Other INFO from openai._base_client (e.g. a future "Defaulting to
    N retries" line) must NOT increment the retry counter — that would
    overcount."""
    from app.observability.openai_retry_tap import _OpenAIRetryTap
    tap = _OpenAIRetryTap()
    with patch("app.observability.prometheus.observe_openai_retry") as m:
        tap.emit(_make_record("Defaulting to 2 retries for client"))
        m.assert_not_called()


def test_handler_falls_back_to_other_when_unparseable():
    """If openai-python renames the message template, the tap still
    fires but with endpoint='other' so the operator at least sees the
    rate jump."""
    from app.observability.openai_retry_tap import _OpenAIRetryTap
    tap = _OpenAIRetryTap()
    with patch("app.observability.prometheus.observe_openai_retry") as m:
        tap.emit(_make_record("Retrying request without a recognisable path"))
        m.assert_called_once_with("other")


def test_handler_swallows_exceptions():
    """Logging handlers MUST NOT raise — if prometheus is unavailable
    or counter increment throws, the tap silently no-ops."""
    from app.observability.openai_retry_tap import _OpenAIRetryTap
    tap = _OpenAIRetryTap()
    with patch("app.observability.prometheus.observe_openai_retry",
               side_effect=RuntimeError("metrics broken")):
        # Must NOT raise
        tap.emit(_make_record("Retrying request to /chat/completions in 1 seconds"))


def test_install_is_idempotent():
    """Boot may invoke install() more than once (reload, hot-rewire,
    etc.) — must not stack duplicate handlers."""
    from app.observability import openai_retry_tap as tap_mod
    target = logging.getLogger("openai._base_client")
    # Reset module-level flag so the install() call body executes once
    tap_mod._installed = False
    before = len([h for h in target.handlers if isinstance(h, tap_mod._OpenAIRetryTap)])
    tap_mod.install_openai_retry_tap()
    tap_mod.install_openai_retry_tap()
    after = len([h for h in target.handlers if isinstance(h, tap_mod._OpenAIRetryTap)])
    assert after - before == 1, f"install stacked {after - before} taps"


def test_install_sets_propagate_false():
    """Ship B — retry chatter must stop bubbling to the root logger
    (which writes stdout). propagate=False on the target logger keeps
    the records local to our tap."""
    from app.observability import openai_retry_tap as tap_mod
    tap_mod._installed = False
    tap_mod.install_openai_retry_tap()
    target = logging.getLogger("openai._base_client")
    assert target.propagate is False


def test_logger_pipeline_increments_counter():
    """End-to-end: when openai._base_client.logger.info() is called with
    the retry template, the counter goes up. This is the actual
    production code path."""
    from app.observability import openai_retry_tap as tap_mod
    tap_mod._installed = False
    tap_mod.install_openai_retry_tap()

    with patch("app.observability.prometheus.observe_openai_retry") as m:
        logging.getLogger("openai._base_client").info(
            "Retrying request to /chat/completions in 0.5 seconds"
        )
        m.assert_called_once_with("/chat/completions")
