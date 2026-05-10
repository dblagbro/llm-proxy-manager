"""v3.7.17 — expose admin-readonly-catalog key_type in API Keys UI
so operators can provision the key the coordinator-hub team's
"Proxy Catalog Admin Key" setting expects."""
from __future__ import annotations

from pathlib import Path


def test_typescript_keytype_union_includes_catalog():
    src = Path("frontend/src/types/index.ts").read_text()
    assert "'admin-readonly-catalog'" in src
    assert "export type KeyType" in src


def test_apikeys_page_dropdown_includes_catalog():
    src = Path("frontend/src/pages/APIKeysPage.tsx").read_text()
    idx = src.index("const KEY_TYPES")
    line = src[idx:idx + 200]
    assert "admin-readonly-catalog" in line


def test_apikeys_page_has_catalog_hint_paragraph():
    """When the form is set to the catalog type, show a small
    explanation paragraph clarifying it's catalog-only / no-inference."""
    src = Path("frontend/src/pages/APIKeysPage.tsx").read_text()
    assert "Catalog-scope key" in src
    assert "Cannot make inference calls" in src


def test_helphint_text_explains_catalog_scope():
    """The HelpHint tooltip on the Key Type label must mention
    admin-readonly-catalog so operators can disambiguate without
    leaving the form."""
    src = Path("frontend/src/pages/APIKeysPage.tsx").read_text()
    assert "admin-readonly-catalog" in src
    # Tooltip must explain the misnamed "readonly"
    assert "Despite the name" in src or "narrow-scope" in src


def test_backend_already_accepts_catalog_keytype():
    """The backend has accepted this key_type since v3.7.2; just
    sanity-check the comment on the Pydantic field is still there."""
    src = Path("app/api/apikeys.py").read_text()
    assert "admin-readonly-catalog" in src


def test_catalog_scope_module_still_uses_admin_readonly_catalog():
    """Renaming the key_type string would break the contract with
    the coordinator-hub team. Asserting the canonical value persists."""
    from app.auth.catalog_scope import _CATALOG_ALLOWED_KEY_TYPES
    assert "admin-readonly-catalog" in _CATALOG_ALLOWED_KEY_TYPES
    assert "admin" in _CATALOG_ALLOWED_KEY_TYPES


def test_version_bumped():
    from app.__version__ import __version__
    parts = tuple(int(p) for p in __version__.split("."))
    assert parts >= (3, 7, 17)
