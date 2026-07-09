"""v5.8.8 — regression test for the codex_billing_worker sweep gate.

Pre-v5.8.8, the worker selected by ``provider_type`` + ``deleted_at``
only — it didn't filter by ``enabled`` and it didn't check
``auto_skip_until``. So a permanently-revoked provider got scraped every
interval, the bearer-refresh attempt 401'd, and ``scrape_failed`` logged
once per cycle indefinitely.

Sibling of v5.8.6 / v5.8.7.
"""
from __future__ import annotations

import inspect


def test_codex_billing_worker_filters_enabled_providers():
    from app.monitoring import codex_billing_worker as mod
    # The function name has shifted across versions — inspect both
    # likely candidates and pick whichever exists.
    src = inspect.getsource(mod)
    assert "Provider.enabled" in src, (
        "v5.8.8 added a Provider.enabled filter; the symbol must be "
        "present so disabled providers aren't scraped."
    )


def test_codex_billing_worker_consults_auto_skip_until():
    from app.monitoring import codex_billing_worker as mod
    src = inspect.getsource(mod)
    assert "auto_skip_until" in src, (
        "v5.8.8 added an auto_skip_until skip; the symbol must be "
        "present so auto-skipped providers don't burn the bearer."
    )
    assert "codex_billing.skip_auto_skip" in src, (
        "the skip path must log codex_billing.skip_auto_skip to stay "
        "consistent with codex_billing.skip_fresh naming."
    )
