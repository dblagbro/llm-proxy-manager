"""v3.7.2 — admin-readonly-catalog scope tests."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.auth.catalog_scope import require_catalog_auth, _CATALOG_ALLOWED_KEY_TYPES


def _request(*, cookies: dict | None = None, headers: dict | None = None):
    req = MagicMock()
    req.cookies = cookies or {}
    req.headers = headers or {}
    return req


# ── allowed key_types are the documented set ──────────────────────


def test_allowed_key_types_contains_admin_and_catalog():
    assert "admin" in _CATALOG_ALLOWED_KEY_TYPES
    assert "admin-readonly-catalog" in _CATALOG_ALLOWED_KEY_TYPES


def test_allowed_key_types_does_not_contain_standard():
    """Standard inference keys must NOT grant catalog access."""
    assert "standard" not in _CATALOG_ALLOWED_KEY_TYPES
    assert "claude-code" not in _CATALOG_ALLOWED_KEY_TYPES


# ── require_catalog_auth — auth flows ─────────────────────────────


@pytest.mark.asyncio
async def test_no_auth_at_all_returns_401():
    req = _request()
    db = MagicMock()
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=None)), \
         patch("app.auth.catalog_scope._try_catalog_apikey", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ex:
            await require_catalog_auth(req, db)
    assert ex.value.status_code == 401


@pytest.mark.asyncio
async def test_session_admin_path_succeeds():
    req = _request(cookies={"llmproxy_session": "abc"})
    fake_admin = MagicMock()
    fake_admin.role = "admin"
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=fake_admin)):
        result = await require_catalog_auth(req, MagicMock())
    assert result is fake_admin


@pytest.mark.asyncio
async def test_api_key_with_correct_scope_succeeds():
    req = _request(headers={"x-api-key": "llmp-abc123"})
    fake_admin = MagicMock()
    fake_admin.role = "admin-readonly-catalog"
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=None)), \
         patch("app.auth.catalog_scope._try_catalog_apikey", new=AsyncMock(return_value=fake_admin)):
        result = await require_catalog_auth(req, MagicMock())
    assert result is fake_admin


@pytest.mark.asyncio
async def test_api_key_with_wrong_scope_returns_403():
    """An api_key was supplied but key_type is e.g. 'standard' — must
    return 403 (not 401) so callers can distinguish 'fix your scope'
    from 'auth missing'."""
    req = _request(headers={"x-api-key": "llmp-standardkey"})
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=None)), \
         patch("app.auth.catalog_scope._try_catalog_apikey", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as ex:
            await require_catalog_auth(req, MagicMock())
    assert ex.value.status_code == 403
    assert "scope" in ex.value.detail.lower()


@pytest.mark.asyncio
async def test_bearer_api_key_recognized_via_llmp_prefix():
    """Bearer header with an llmp-* prefix should be treated as an
    api key, not a session token — even if the session-token lookup
    happens to succeed via coincidence."""
    req = _request(headers={"Authorization": "Bearer llmp-some-key"})
    fake_admin = MagicMock()
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=None)), \
         patch("app.auth.catalog_scope._try_catalog_apikey", new=AsyncMock(return_value=fake_admin)):
        result = await require_catalog_auth(req, MagicMock())
    assert result is fake_admin


@pytest.mark.asyncio
async def test_session_takes_priority_over_api_key():
    """If both are supplied, session admin wins (cheaper path, no DB
    lookup for the api_key)."""
    req = _request(cookies={"llmproxy_session": "abc"}, headers={"x-api-key": "llmp-abc"})
    session_admin = MagicMock()
    apikey_admin = MagicMock()
    apikey_called = AsyncMock(return_value=apikey_admin)
    with patch("app.auth.catalog_scope._try_session_admin", new=AsyncMock(return_value=session_admin)), \
         patch("app.auth.catalog_scope._try_catalog_apikey", new=apikey_called):
        result = await require_catalog_auth(req, MagicMock())
    assert result is session_admin
    apikey_called.assert_not_called()


# ── wiring regression ─────────────────────────────────────────────


def test_llm_models_routes_use_catalog_auth():
    """Both GET and PUT on /api/llm/models/{model_id} must use
    require_catalog_auth, not the wider require_admin."""
    from pathlib import Path
    src = Path("app/api/llm_models.py").read_text()
    assert "require_catalog_auth" in src
    # The GET and PUT route handlers should reference the dep
    assert src.count("Depends(require_catalog_auth)") == 2
    # Old direct usage of require_admin should be gone from this file
    assert "require_admin" not in src
