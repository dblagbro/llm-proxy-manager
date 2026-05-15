"""v3.9.17 — litellm pin widened to allow 1.84.x (P4 evaluation).

The v1.84.0 release shipped breaking changes — but the P4 evaluation
(2026-05-15) found every one of them is a LiteLLM **Proxy-server**
feature:
  - master-key propagation (proxy)
  - request-control-field stripping (proxy)
  - caller-tags behavior (proxy)
  - pass-through-endpoint auth default (proxy)
  - clientside-credential handling / BYOK (proxy)
  - onboarding flow (proxy)
  - CLI SSO login (proxy CLI)

We use litellm strictly as a **Python library**: ``acompletion`` /
``completion`` calls, exception classes, streaming-chunk parsing,
tool-call shapes. Upstream notes explicitly state those interfaces
are unchanged in 1.84.0. Empirical: the full 1907-test unit suite
passes against litellm 1.84.0 with zero failures.

These guards lock the pin shape so a future contributor doesn't
accidentally re-tighten it OR widen it past the next deliberate-eval
ceiling.
"""
from __future__ import annotations

from pathlib import Path


def test_pin_allows_1_84_x():
    req = Path("requirements.txt").read_text()
    assert "litellm>=1.83.0,<1.85.0" in req
    # The old <1.84.0 ceiling must be gone
    assert "<1.84.0" not in req


def test_pin_keeps_ceiling_for_next_major():
    """Ceiling stays at <1.85.0 so a 1.85.x bump triggers the same
    deliberate evaluation, rather than floating unbounded."""
    req = Path("requirements.txt").read_text()
    assert "<1.85.0" in req


def test_pin_rationale_documented():
    """The requirements.txt comment must explain WHY the ceiling moved
    — so the next person doesn't have to re-derive the eval."""
    req = Path("requirements.txt").read_text()
    assert "Proxy-server" in req or "proxy-server" in req
    assert "1907" in req  # the empirical test-count evidence


def _litellm_attrs_in_clean_interpreter() -> set[str]:
    """Import litellm in a SUBPROCESS so the check sees the real
    installed library, not the lightweight stub other unit tests
    inject into ``sys.modules['litellm']`` for speed. Returns the set
    of attribute names we care about that are present."""
    import subprocess
    import sys
    probe = (
        "import litellm, json; "
        "names=['acompletion','completion','AuthenticationError',"
        "'RateLimitError','APIConnectionError','Timeout',"
        "'InternalServerError']; "
        "print(json.dumps([n for n in names if hasattr(litellm, n)]))"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, f"litellm probe failed: {out.stderr[:500]}"
    import json as _json
    return set(_json.loads(out.stdout.strip()))


def test_litellm_core_call_symbols_present():
    """Smoke: the real installed litellm exposes acompletion/completion
    — our core call sites (app/routing/retry.py et al.). Run in a clean
    subprocess so suite-level litellm stubbing can't mask a real removal."""
    present = _litellm_attrs_in_clean_interpreter()
    assert "acompletion" in present
    assert "completion" in present


def test_litellm_exception_classes_present():
    """circuit_breaker.py + fallback.py match on litellm exception class
    names. Confirm the classes still exist so the error taxonomy keeps
    working. Clean-subprocess check (see helper)."""
    present = _litellm_attrs_in_clean_interpreter()
    for exc_name in (
        "AuthenticationError",
        "RateLimitError",
        "APIConnectionError",
        "Timeout",
        "InternalServerError",
    ):
        assert exc_name in present, f"litellm.{exc_name} missing"
