"""v3.8.0 (#251) — provider_type value rename: codex-oauth → ChatGPT-oauth-plan.

Operator-locked rename 2026-05-13. Breaking change (signals the v3.8 major).
Backward-compat alias at the API entry preserves callers that still POST
the old name; existing DB rows are renamed by the migration.
"""
from __future__ import annotations

from pathlib import Path


# ── No string literal "codex-oauth" remaining in code ─────────────


def _strip_comments_and_strings(src: str, lang: str) -> str:
    """Strip line comments + block comments so the scan only sees real
    code tokens. Crude but sufficient for the rename audit: a "codex-oauth"
    inside a comment is fine; we only fail on actual identifier-position
    literals that would execute."""
    import re
    if lang == "py":
        # Strip # line comments + triple-quoted docstrings
        src = re.sub(r'""".*?"""', "", src, flags=re.DOTALL)
        src = re.sub(r"'''.*?'''", "", src, flags=re.DOTALL)
        src = re.sub(r"#.*", "", src)
    elif lang == "ts":
        # Strip /* */ block comments + // line comments
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
        src = re.sub(r"//.*", "", src)
    return src


# Allowlist of intentional remaining references (the backward-compat shim
# at the API entry). Drop these in a future version when the shim is
# removed.
_BACKEND_ALLOWLIST_FRAGMENTS = (
    # Backward-compat shim at the API entry — normalizes legacy POSTs.
    'body.provider_type == "codex-oauth"',
    # One-shot migration that renames existing DB rows. References the
    # OLD value intentionally.
    "WHERE provider_type='codex-oauth'",
)


def test_no_codex_oauth_string_literal_in_backend():
    bad = []
    for p in Path("app").rglob("*.py"):
        if "__pycache__" in str(p):
            continue
        src = _strip_comments_and_strings(p.read_text(), "py")
        for allow in _BACKEND_ALLOWLIST_FRAGMENTS:
            src = src.replace(allow, "")
        for needle in ('"codex-oauth"', "'codex-oauth'"):
            if needle in src:
                bad.append(f"{p}: {needle}")
    assert not bad, "unexpected codex-oauth literals remain:\n  " + "\n  ".join(bad)


def test_no_codex_oauth_string_literal_in_frontend():
    bad = []
    for p in Path("frontend/src").rglob("*"):
        if p.is_dir() or "node_modules" in str(p):
            continue
        if p.suffix not in (".ts", ".tsx"):
            continue
        src = _strip_comments_and_strings(p.read_text(), "ts")
        for needle in ('"codex-oauth"', "'codex-oauth'"):
            if needle in src:
                bad.append(f"{p}: {needle}")
    assert not bad, "unexpected codex-oauth literals remain in frontend:\n  " + "\n  ".join(bad)


# ── DB migration ───────────────────────────────────────────────────


def test_migration_renames_existing_rows():
    src = Path("app/models/database.py").read_text()
    assert "UPDATE providers SET provider_type='ChatGPT-oauth-plan'" in src
    assert "WHERE provider_type='codex-oauth'" in src


# ── Backward-compat alias at API entry ────────────────────────────


def test_create_provider_normalizes_legacy_name():
    """Callers POSTing provider_type='codex-oauth' get normalized to
    'ChatGPT-oauth-plan' before storage. Single shim location for easy
    removal in a future version."""
    src = Path("app/api/providers.py").read_text()
    # The shim must be inside create_provider, before the type-specific
    # validation branches.
    idx = src.index("async def create_provider")
    body = src[idx:idx + 3000]
    assert 'body.provider_type == "codex-oauth"' in body
    assert 'body.provider_type = "ChatGPT-oauth-plan"' in body


# ── Type-specific branches use the new name ───────────────────────


def test_provider_create_routes_chatgpt_oauth_plan():
    """The OAuth-flavor branch that handles codex-style auth must key
    off the new name."""
    src = Path("app/api/providers.py").read_text()
    assert 'body.provider_type == "ChatGPT-oauth-plan"' in src


def test_dispatcher_routes_chatgpt_oauth_plan():
    src = Path("app/api/completions.py").read_text()
    assert '"ChatGPT-oauth-plan"' in src


def test_router_recognizes_chatgpt_oauth_plan():
    # v4.4.38: the PROVIDER_TYPE_TO_LITELLM table moved to
    # litellm_binding.py; assertion is structurally identical.
    src = Path("app/routing/litellm_binding.py").read_text()
    assert '"ChatGPT-oauth-plan"' in src


# ── Provider supervisor + manual override still work ──────────────


def test_codex_billing_worker_filters_chatgpt_oauth_plan():
    """The Phase 1 codex billing worker queries by provider_type — must
    use the new name after rename."""
    src = Path("app/monitoring/codex_billing_worker.py").read_text()
    assert 'Provider.provider_type == "ChatGPT-oauth-plan"' in src


def test_codex_billing_endpoint_gates_on_chatgpt_oauth_plan():
    src = Path("app/api/codex_billing.py").read_text()
    assert 'provider_type != "ChatGPT-oauth-plan"' in src


# ── Frontend rename ───────────────────────────────────────────────


def test_provider_form_dropdown_uses_new_name():
    src = Path("frontend/src/components/providers/ProviderForm.tsx").read_text()
    assert "'ChatGPT-oauth-plan'" in src
    # The OAUTH_FLAVORS map must key on the new name
    assert "'ChatGPT-oauth-plan': {" in src


def test_codex_billing_panel_renders_for_new_name():
    """The frontend panel gating condition uses the new provider_type."""
    src = Path("frontend/src/pages/ProvidersPage.tsx").read_text()
    # The exchange branch in saveMutation checks the new name
    assert "data.provider_type === 'ChatGPT-oauth-plan'" in src


def test_version_bumped_to_v380():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    # The major bump signals the breaking-rename
    assert parts >= (3, 8, 0)
