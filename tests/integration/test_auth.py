"""Auth API tests — login, logout, session, unauthorized access."""
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL, ADMIN_USER, ADMIN_PASS


class TestLogin:
    def test_valid_login_returns_user(self):
        s = requests.Session()
        s.verify = False
        r = s.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        assert r.status_code == 200
        d = r.json()
        assert d["username"] == ADMIN_USER
        assert d["role"] == "admin"

    def test_wrong_password_rejected(self):
        s = requests.Session()
        s.verify = False
        r = s.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": "wrong"})
        assert r.status_code == 401

    def test_unknown_user_rejected(self):
        s = requests.Session()
        s.verify = False
        r = s.post(f"{BASE_URL}/api/auth/login", json={"username": "nobody", "password": "x"})
        assert r.status_code == 401

    def test_me_returns_current_user(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        assert r.json()["username"] == ADMIN_USER


class TestUnauthorized:
    def test_providers_requires_auth(self):
        s = requests.Session()
        s.verify = False
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        r = s.get(f"{BASE_URL}/api/providers")
        assert r.status_code == 401

    def test_settings_requires_auth(self):
        s = requests.Session()
        s.verify = False
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        r = s.get(f"{BASE_URL}/api/settings")
        assert r.status_code == 401

    def test_apikeys_requires_auth(self):
        s = requests.Session()
        s.verify = False
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        r = s.get(f"{BASE_URL}/api/keys")
        assert r.status_code == 401

    def test_health_is_public(self):
        """Health endpoint must be reachable without auth (used by cluster peers)."""
        r = requests.get(f"{BASE_URL}/health", verify=False)
        assert r.status_code == 200
        d = r.json()
        assert "status" in d
        assert d["version"] == "2.0.0"


class TestLogout:
    def test_logout_invalidates_session(self):
        s = requests.Session()
        s.verify = False
        s.headers["X-Requested-With"] = "XMLHttpRequest"
        s.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
        # Confirm session works
        r = s.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 200
        # Logout
        s.post(f"{BASE_URL}/api/auth/logout")
        # Should now be rejected
        r = s.get(f"{BASE_URL}/api/auth/me")
        assert r.status_code == 401
