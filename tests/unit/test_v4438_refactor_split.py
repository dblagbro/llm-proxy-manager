"""v4.4.38 — incremental architectural refactor source-guard tests.

Pin the three splits landed in v4.4.38 so a future refactor that
collapses them back together fails CI loudly rather than silently
re-tangling the modules.

The corresponding refactors are documented in:
- refactor-log.md §"2026-06-02 — v4.4.38"
- architecture.md "Module Map" — updated for the new files
"""
from pathlib import Path


# ── #1: router.py litellm-binding extract ────────────────────────────


def test_litellm_binding_module_exists():
    """v4.4.38: extracted PROVIDER_TYPE_TO_LITELLM + PROVIDER_DEFAULT_MODELS
    + build_litellm_model + build_litellm_kwargs + resolve_chat_model_for_provider
    + _is_embedding_model + _model_family_provider_types + _native_thinking_params
    out of router.py into a dedicated litellm_binding.py. router.py keeps the
    select_provider strategy + RouteResult; litellm_binding.py owns the
    "how do we call litellm given a Provider row" question."""
    p = Path("app/routing/litellm_binding.py")
    assert p.exists(), "litellm_binding.py missing — was the v4.4.38 split reverted?"
    src = p.read_text()
    assert "PROVIDER_TYPE_TO_LITELLM" in src
    assert "PROVIDER_DEFAULT_MODELS" in src
    assert "def build_litellm_model" in src
    assert "def build_litellm_kwargs" in src
    assert "async def resolve_chat_model_for_provider" in src


def test_router_reexports_litellm_binding():
    """Existing callers do ``from app.routing.router import build_litellm_model, …``
    — the re-export in router.py must preserve that surface."""
    src = Path("app/routing/router.py").read_text()
    assert "from app.routing.litellm_binding import" in src
    # All seven names must be re-exported
    for name in (
        "PROVIDER_TYPE_TO_LITELLM",
        "PROVIDER_DEFAULT_MODELS",
        "build_litellm_model",
        "build_litellm_kwargs",
        "resolve_chat_model_for_provider",
        "_is_embedding_model",
        "_model_family_provider_types",
        "_native_thinking_params",
    ):
        assert name in src, f"{name} not re-exported from router.py"


def test_router_no_longer_defines_litellm_binding_helpers():
    """The moved symbols must NOT be re-defined in router.py — otherwise
    the re-export is shadowed and a future cursor-oauth-class bug could
    sneak in via the wrong copy of the table."""
    src = Path("app/routing/router.py").read_text()
    # ``def build_litellm_model`` and ``def build_litellm_kwargs`` must not appear
    # as ``def`` statements (they appear in the import block + docstring).
    assert "def build_litellm_model(" not in src.replace(
        "from app.routing.litellm_binding import", ""
    ).replace(
        "    build_litellm_model,", ""
    )


# ── #2: cascade dispatch extract ─────────────────────────────────────


def test_try_cascade_dispatch_lives_in_messages_dispatch():
    """v4.4.38: the cascade orchestration (cheap-route call + grader
    verdict + accept-or-fall-through) was extracted from messages.py
    into ``_messages_dispatch.try_cascade_dispatch``. Continues the
    v3.10.9 ``dispatch_claude_oauth_chain`` extraction pattern."""
    src = Path("app/api/_messages_dispatch.py").read_text()
    assert "async def try_cascade_dispatch" in src
    assert "grade_answer" in src   # the cascade-specific dependency
    assert "X-Cascade" in src      # cascade-specific header


def test_messages_py_delegates_cascade():
    """messages.py must call try_cascade_dispatch rather than inline the
    cheap-route + grader logic. Source-grep pins this so a future
    refactor that re-inlines fires this test."""
    src = Path("app/api/messages.py").read_text()
    assert "try_cascade_dispatch" in src
    # The pre-extraction inline grader-pick logic shouldn't be back —
    # the inner ``try: cheap_route = await select_provider(... prefer_cheapest=True ...)``
    # block is the hallmark.
    assert "prefer_cheapest=True" not in src, (
        "messages.py contains prefer_cheapest=True — the cascade was "
        "re-inlined. Restore the try_cascade_dispatch delegation."
    )


# ── #3: grok-web bridge axial split ──────────────────────────────────


def test_grok_web_bridge_module_exists():
    """v4.4.38: ``_bridge_chat`` (the bridge-mode dispatch — POSTs to the
    Playwright sidecar at ``Provider.extra_config.bridge_url``) was
    extracted from grok_web.py into grok_web_bridge.py as the first step
    of the manual/bridge axial split. Manual-mode HTTP replay still lives
    in grok_web.py."""
    p = Path("app/providers/grok_web_bridge.py")
    assert p.exists(), "grok_web_bridge.py missing — was the v4.4.38 split reverted?"
    src = p.read_text()
    assert "async def _bridge_chat" in src


def test_grok_web_reexports_bridge_chat():
    """tests/unit/test_grok_web.py imports ``_bridge_chat`` from
    ``app.providers.grok_web`` — preserve that surface via re-export."""
    src = Path("app/providers/grok_web.py").read_text()
    assert "from app.providers.grok_web_bridge import _bridge_chat" in src


def test_grok_web_bridge_uses_lazy_import_to_avoid_cycle():
    """grok_web.py re-exports from grok_web_bridge, and grok_web_bridge
    needs GrokWebError/_pick_conversation_id from grok_web. To avoid a
    module-load-time circular, grok_web_bridge defers its import of
    grok_web names to call time. If a future change hoists that import
    to module level, the load order becomes fragile."""
    src = Path("app/providers/grok_web_bridge.py").read_text()
    # The import must be inside the function body, not at module top.
    assert "    from app.providers.grok_web import" in src, (
        "grok_web_bridge.py must keep the grok_web import inside _bridge_chat "
        "to avoid a circular import at module load time. If you hoisted it, "
        "either move the shared names to a third file or revert."
    )
