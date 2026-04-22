"""API key management and LLM endpoint auth tests."""
import uuid
import pytest

from tests.conftest import BASE_URL


class TestApiKeyCRUD:
    def test_create_returns_raw_key_once(self, admin_session):
        name = f"test-key-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys", json={"name": name, "key_type": "standard"})
        assert r.status_code == 200
        data = r.json()
        assert "raw_key" in data, "Raw key must be returned on creation"
        assert data["raw_key"].startswith("llmp-")
        key_id = data["id"]
        # Cleanup
        admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_list_shows_prefix_not_full_key(self, admin_session, test_api_key):
        r = admin_session.get(f"{BASE_URL}/api/keys")
        assert r.status_code == 200
        keys = r.json()
        assert isinstance(keys, list)
        for k in keys:
            assert "key_prefix" in k
            # Full raw key must NOT appear in list responses
            assert "raw_key" not in k

    def test_revoke_key_rejects_llm_calls(self, admin_session):
        """Create a key, make a valid call, revoke it, verify 401."""
        name = f"revoke-test-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys", json={"name": name, "key_type": "standard"})
        assert r.status_code == 200
        data = r.json()
        raw_key = data["raw_key"]
        key_id = data["id"]

        # LLM call with the key — must reach the proxy (regardless of provider outcome)
        import requests, urllib3
        urllib3.disable_warnings()
        resp = requests.post(
            f"{BASE_URL}/v1/messages",
            headers={"x-api-key": raw_key, "Content-Type": "application/json"},
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
            verify=False,
            timeout=30,
        )
        # 200 or a provider error — both mean auth passed
        assert resp.status_code != 401, "Key should be valid before revoke"

        # Revoke
        admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

        # Now the same key must be rejected
        resp2 = requests.post(
            f"{BASE_URL}/v1/messages",
            headers={"x-api-key": raw_key, "Content-Type": "application/json"},
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
            verify=False,
            timeout=10,
        )
        assert resp2.status_code == 401

    def test_missing_key_returns_401(self):
        import requests, urllib3
        urllib3.disable_warnings()
        r = requests.post(
            f"{BASE_URL}/v1/messages",
            headers={"Content-Type": "application/json"},
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
            verify=False,
            timeout=10,
        )
        assert r.status_code == 401

    def test_invalid_key_returns_401(self):
        import requests, urllib3
        urllib3.disable_warnings()
        r = requests.post(
            f"{BASE_URL}/v1/messages",
            headers={"x-api-key": "llmp-invalid-key-xyz", "Content-Type": "application/json"},
            json={"model": "claude-3-5-sonnet-20241022", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]},
            verify=False,
            timeout=10,
        )
        assert r.status_code == 401
