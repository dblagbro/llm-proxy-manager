"""v5.7.12 — bumped supervisor HTTP timeout from 30s to 90s.

Why: on the TMR cluster the supervisor's internal POST to /v1/messages
routes through anthropic-blocked → Gemini-fallback substitution. Observed
chain time ~33s in the 2026-06-16 log-watch — caused a 50%+ timeout
rate at the prior 30s ceiling. 90s removes the noise without hiding a
real slow-routing regression.
"""
from __future__ import annotations

from pathlib import Path


def test_supervisor_timeout_is_at_least_60_sec():
    """Pin the v5.7.12 bump. If someone tightens it back to 30s the
    50%-timeout-rate symptom returns."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    # The setting fallback default should be >= 60s
    assert "ai_provider_supervisor_http_timeout_sec" in src
    assert "90.0" in src or "90" in src
    # Old 30.0 literal must be gone from the classify_with_llm path.
    # (It may still appear in other unrelated places — search the
    # classify function specifically.)
    idx = src.find("def classify_with_llm")
    end = src.find("\nasync def ", idx + 1)
    body = src[idx:end] if end > idx else src[idx:]
    assert "timeout=30" not in body, (
        "ai_provider_supervisor classify_with_llm still hardcodes "
        "timeout=30 — v5.7.12 bumped this to a configurable default 90s."
    )


def test_supervisor_timeout_is_setting_overridable():
    """The 90s is the FALLBACK; operators can override via system_setting
    ai_provider_supervisor_http_timeout_sec when their cluster has
    different routing latency characteristics."""
    src = Path("app/monitoring/ai_provider_supervisor.py").read_text()
    assert "getattr(settings, \"ai_provider_supervisor_http_timeout_sec\"" in src
