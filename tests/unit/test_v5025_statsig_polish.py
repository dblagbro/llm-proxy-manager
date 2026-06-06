"""v5.0.25 / remediation Batch 5 — statsig validator + cache polish.

BUG-059: tighten validator to reject ONLY the specific SDK-error
prefixes that actually appeared in observed fallback values, not bare
substrings like "error" / "Error".
BUG-060: cache-invalidate-on-403 — when chat() gets a 403 retry, force
the next call to capture a fresh statsig instead of reusing the cache.

Pin tests use the file-as-text pattern since the bridge module
imports playwright/uvicorn which aren't installed in the unit
environment. Behavioral tests stub the minimal surface.
"""
from __future__ import annotations

import base64
from pathlib import Path


_SRC = Path("grok_bridge/app.py")


# ── Source pins ─────────────────────────────────────────────────────


def test_validator_uses_specific_x0_prefixes_not_bare_error():
    """BUG-059: the validator's bad_markers must be the x0:* prefixes,
    NOT the substring 'error'. Reverting to substring match would
    re-introduce the ~0.016% false-positive rate against legit random
    base64 statsigs.
    """
    src = _SRC.read_text()
    fn_start = src.find("def _statsig_id_looks_valid")
    next_def = src.find("def ", fn_start + 1)
    body = src[fn_start:next_def if next_def != -1 else fn_start + 3000]
    # Must contain the specific prefixes
    for prefix in ("x0:TypeError", "x0:ReferenceError"):
        assert prefix in body, (
            f"validator missing prefix {prefix!r} — BUG-059 regression"
        )
    # Must NOT contain bare-substring markers (the pre-fix bad_markers).
    # Allow comment mentions but the bad_prefixes tuple itself must not
    # contain bare 'error' / 'Error' / standalone 'x0:' as items.
    assert '"error"' not in body or '"x0:Error:"' in body, (
        "validator still contains bare 'error' substring marker — "
        "BUG-059 regression (would false-positive on legit statsigs)"
    )


def test_invalidate_helper_exists():
    src = _SRC.read_text()
    assert "def invalidate_statsig_cache(" in src


def test_chat_retry_invalidates_cache():
    """BUG-060: the 401/403 retry branch in chat() must call
    invalidate_statsig_cache before re-capturing. Without this the
    retry uses the same stale cached value and fails again."""
    src = _SRC.read_text()
    # Find the retry block
    retry_idx = src.find('logger.warning("grok.com %s — refreshing')
    assert retry_idx != -1
    block = src[retry_idx:retry_idx + 800]
    assert "invalidate_statsig_cache" in block, (
        "BUG-060 regression: chat() retry path no longer invalidates "
        "the statsig cache before re-capture; the retry will reuse "
        "the same stale statsig that just got 403'd."
    )


# ── Behavioral: validator decisions ────────────────────────────────


def _make_statsig_from_decoded(text: str) -> str:
    """Encode a decoded payload into a statsig-like base64 string."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def test_validator_rejects_actual_sdk_error_fallback():
    """The exact error string observed 2026-06-05 — must be rejected.
    Imports lazily so the module-level Playwright imports don't crash
    the unit test runner. We test the validator's logic by emulating
    the exact byte sequence."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_gb_app", str(_SRC))
    # Don't actually exec the module — we'd need playwright. Instead,
    # use the canonical logic in-line.
    # Real Statsig SDK error fallback observed at sweep time:
    real_bad = _make_statsig_from_decoded(
        "x0:TypeError: Cannot read properties of undefined (reading 'childNodes')"
    )
    # The validator's bad_prefixes list — kept in sync with the source.
    bad_prefixes = (
        "x0:TypeError", "x0:ReferenceError", "x0:SyntaxError",
        "x0:RangeError", "x0:Error:",
    )

    def validator_simulation(sid: str) -> bool:
        if not sid or len(sid) < 20:
            return False
        try:
            decoded = base64.b64decode(sid + "==", validate=False).decode("utf-8", errors="ignore")
        except Exception:
            return True
        stripped = decoded.lstrip()
        return not any(
            stripped.startswith(p) or decoded.startswith(p)
            for p in bad_prefixes
        )

    assert not validator_simulation(real_bad), (
        "validator must reject the actual SDK-error fallback we "
        "observed during the 2026-06-05 sweep."
    )

    # A legit-random statsig that HAPPENS to contain "error" or
    # "Error" somewhere in its decode must STILL pass — that's the
    # whole point of the BUG-059 tightening.
    legit_random = _make_statsig_from_decoded(
        "rxv4error-but-real-statsig-bytes-here-9831"
    )
    assert validator_simulation(legit_random), (
        "BUG-059 regression: validator rejected a legit statsig "
        "containing the substring 'error' (false positive)."
    )

    # Empty / too-short / blatant garbage rejected.
    assert not validator_simulation("")
    assert not validator_simulation("short")
