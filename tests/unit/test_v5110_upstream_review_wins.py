"""v5.11.0 — Upstream-review wins: NVIDIA NIM provider + SSE live tail.

Inspired by 2026-06-29 audit of:
- Alishahryar1/free-claude-code (NVIDIA NIM provider catalog)
- snipeship/ccflare (`/api/requests/stream` live SSE)
"""
from __future__ import annotations

from pathlib import Path


# ── (1) NVIDIA NIM provider wiring ─────────────────────────────────────


def test_nvidia_nim_in_litellm_prefix_map():
    from app.routing.litellm_binding import PROVIDER_TYPE_TO_LITELLM
    assert PROVIDER_TYPE_TO_LITELLM.get("nvidia_nim") == "nvidia_nim", (
        "v5.11.0: nvidia_nim must map to the litellm 'nvidia_nim/' prefix."
    )


def test_nvidia_nim_has_default_model():
    """The test_all_known_types_have_default invariant in test_router.py
    would catch a missing entry; this test pins the chosen default."""
    from app.routing.litellm_binding import PROVIDER_DEFAULT_MODELS
    default = PROVIDER_DEFAULT_MODELS.get("nvidia_nim")
    assert default, "v5.11.0: nvidia_nim must have a default model entry."
    assert default.startswith("nvidia/"), (
        "v5.11.0: NIM default should be a vendor-namespaced NVIDIA model "
        "(the catalog uses ``vendor/model`` form — bare names would "
        "litellm-resolve to nvidia_nim/<bare> which the NIM endpoint "
        "doesn't recognize)."
    )


def test_nvidia_nim_in_scanner_api_key_gate():
    """Pre-flight 'no API key' check in scanner.py must include nvidia_nim
    so the operator sees the friendly 'Open Edit modal' hint instead of a
    600-char litellm traceback."""
    src = Path("app/providers/scanner.py").read_text()
    # Find the provider_type tuple that gates on api_key absence.
    needle = '"nvidia_nim"'
    gate_block_start = src.find(
        'provider.provider_type in ("anthropic", "openai"'
    )
    assert gate_block_start > 0, "couldn't find api_key pre-flight check"
    gate_block_end = src.find(")", gate_block_start)
    block = src[gate_block_start:gate_block_end]
    assert needle in block, (
        "v5.11.0: nvidia_nim must be in scanner.py's api_key pre-flight tuple."
    )


def test_nvidia_nim_in_capability_inference():
    src = Path("app/routing/capability_inference.py").read_text()
    assert 'provider_type == "nvidia_nim"' in src, (
        "v5.11.0: capability_inference.py must handle nvidia_nim (regions, etc)."
    )


# ── (2) SSE live-tail endpoint ─────────────────────────────────────────


def test_sse_module_exists():
    from app.api.admin_requests_stream import router  # noqa: F401


def test_sse_router_wired_into_main():
    src = Path("app/main.py").read_text()
    assert "admin_requests_stream" in src, (
        "v5.11.0: admin_requests_stream router must be imported in main.py"
    )
    assert "app.include_router(admin_requests_stream_router)" in src, (
        "v5.11.0: admin_requests_stream router must be include_router'd"
    )


def test_sse_endpoint_uses_watchdog():
    """Long-poll SSE handlers MUST have the v5.7.17 watchdog: otherwise
    a dashboard tab left open then closed leaks a DB session forever
    (the exact bug class we shipped v5.9.9 + v5.9.10 to close)."""
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert "watch_for_disconnect" in src
    assert "Depends(watch_for_disconnect)" in src
    # And it must precede ``require_admin`` so the watcher is armed
    # before the auth check kicks off (auth is the first await).
    watchdog_idx = src.find("Depends(watch_for_disconnect)")
    admin_idx = src.find("Depends(require_admin)")
    assert 0 < watchdog_idx < admin_idx, (
        "v5.11.0: watchdog must precede require_admin in the SSE signature."
    )


def test_sse_endpoint_uses_admin_auth():
    src = Path("app/api/admin_requests_stream.py").read_text()
    assert "Depends(require_admin)" in src, (
        "v5.11.0: SSE stream MUST be admin-only (the same bar as the "
        "rest of /api/admin/*)."
    )


def test_sse_endpoint_under_admin_prefix():
    """ccflare exposes /api/requests/stream; we mount under
    /api/admin/requests/stream to match our existing admin namespace
    and so the route is naturally gated by the admin auth contract
    (no public surface, no key allowlist needed)."""
    from app.api.admin_requests_stream import router
    paths = [r.path for r in router.routes]
    assert "/api/admin/requests/stream" in paths


# ── (3) version bumped ─────────────────────────────────────────────────


def test_version_bumped():
    src = Path("app/__version__.py").read_text()
    assert '"5.11.0"' in src, "__version__ should be 5.11.0"
