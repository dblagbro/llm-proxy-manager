"""
Root conftest — session-scoped fixtures shared by all test layers.
"""
import time
import uuid

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BASE_URL = "https://www.voipguru.org/llm-proxy2"
ADMIN_USER = "admin"
ADMIN_PASS = "Super*120120"
MOCK_PORT = 9876
DOCKER_BRIDGE_IP = "172.18.0.1"
MOCK_BASE_URL = f"http://{DOCKER_BRIDGE_IP}:{MOCK_PORT}"


def pytest_addoption(parser):
    parser.addoption(
        "--run-real",
        action="store_true",
        default=False,
        help="Run real-provider compatibility and settings-permutation tests (costs API credits)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "real_providers: needs real LLM calls — use --run-real to enable")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-real"):
        skip = pytest.mark.skip(reason="real-provider test — pass --run-real to enable")
        for item in items:
            if "real_providers" in item.keywords:
                item.add_marker(skip)


def _api_session() -> requests.Session:
    """New session with admin credentials and API-friendly headers."""
    s = requests.Session()
    s.verify = False
    s.headers.update({"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"})
    r = s.post(f"{BASE_URL}/api/auth/login", json={"username": ADMIN_USER, "password": ADMIN_PASS})
    assert r.status_code == 200, f"Admin login failed: {r.status_code} {r.text[:200]}"
    return s


@pytest.fixture(scope="session")
def admin_session() -> requests.Session:
    return _api_session()


@pytest.fixture(scope="session")
def settings_snapshot(admin_session):
    """Capture settings at session start; restore unconditionally at session end."""
    r = admin_session.get(f"{BASE_URL}/api/settings")
    assert r.status_code == 200
    original = r.json()
    yield original
    # Restore — strip keys the API doesn't accept (None SMTP fields etc)
    restorable = {k: v for k, v in original.items() if v is not None}
    admin_session.put(f"{BASE_URL}/api/settings", json=restorable)


@pytest.fixture(scope="session")
def test_api_key(admin_session) -> str:
    """Create a standard API key for LLM endpoint calls; delete at session end."""
    r = admin_session.post(
        f"{BASE_URL}/api/keys",
        json={"name": f"pytest-{uuid.uuid4().hex[:8]}", "key_type": "standard"},
    )
    assert r.status_code == 200, f"API key creation failed: {r.text}"
    data = r.json()
    key_id = data["id"]
    raw_key = data["raw_key"]
    yield raw_key
    admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")


@pytest.fixture(scope="session")
def cot_api_key(admin_session) -> str:
    """Create a claude-code API key to trigger CoT-E automatically."""
    r = admin_session.post(
        f"{BASE_URL}/api/keys",
        json={"name": f"pytest-cot-{uuid.uuid4().hex[:8]}", "key_type": "claude-code"},
    )
    assert r.status_code == 200, f"CoT API key creation failed: {r.text}"
    data = r.json()
    key_id = data["id"]
    raw_key = data["raw_key"]
    yield raw_key
    admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")


@pytest.fixture(scope="session")
def all_non_mock_providers(admin_session) -> list[dict]:
    """All enabled non-mock providers regardless of API key configuration."""
    r = admin_session.get(f"{BASE_URL}/api/providers")
    assert r.status_code == 200
    return sorted(
        [p for p in r.json() if p["enabled"] and "mock" not in p["name"].lower()],
        key=lambda p: p["priority"],
    )


@pytest.fixture(scope="session")
def real_providers(admin_session) -> list[dict]:
    """Enabled real providers that have API keys configured, ordered by priority.
    Excludes any mock/test providers (names containing 'mock') and unconfigured providers."""
    r = admin_session.get(f"{BASE_URL}/api/providers")
    assert r.status_code == 200
    return sorted(
        [
            p for p in r.json()
            if p["enabled"]
            and "mock" not in p["name"].lower()
            and p.get("api_key")
        ],
        key=lambda p: p["priority"],
    )


@pytest.fixture(scope="session")
def mock_server(admin_session):
    """
    Start the local mock LLM server and register it as a proxy provider.
    The mock listens on 0.0.0.0:{MOCK_PORT} so Docker containers reach it
    via {DOCKER_BRIDGE_IP}:{MOCK_PORT}.
    """
    from tests.mock_llm_server import start_mock_server

    srv = start_mock_server(MOCK_PORT)

    # Register as a provider in the proxy (lowest priority — never selected unless forced)
    r = admin_session.post(
        f"{BASE_URL}/api/providers",
        json={
            "name": "pytest-mock",
            "provider_type": "compatible",
            "api_key": "mock-key",
            "base_url": MOCK_BASE_URL,
            "default_model": "mock-gpt",
            "priority": 99,
            "enabled": True,
            "timeout_sec": 15,
            "exclude_from_tool_requests": False,
        },
    )
    assert r.status_code == 200, f"Mock provider registration failed: {r.text}"
    provider_id = r.json()["id"]

    yield {"id": provider_id, "srv": srv}

    # Teardown
    admin_session.delete(f"{BASE_URL}/api/providers/{provider_id}")
    srv.stop()
