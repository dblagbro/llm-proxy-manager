"""
Root conftest — session-scoped fixtures shared by all test layers.
"""
import time
import uuid

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import os as _os

BASE_URL = _os.environ.get("LLMPROXY_TEST_BASE_URL", "https://www.voipguru.org/llm-proxy2")
ADMIN_USER = _os.environ.get("LLMPROXY_TEST_ADMIN_USER", "admin")
# v4.4.29 — credential moved out of source. Pre-fix the admin password
# lived in plaintext in this committed file, making it indefinitely
# visible in git history on a public repo. Now read from
# LLMPROXY_TEST_ADMIN_PASS at test time (operator sets it in their
# shell or .env); fall back to the documented default "admin" so a
# from-scratch checkout against a default-credentials dev box still
# works. Integration runs that purge tombstones (gated behind
# LLMPROXY_TEST_PURGE_LIVE=1 since v4.4.24/F-INFRA-001) also need
# this env var set to authenticate against live.
ADMIN_PASS = _os.environ.get("LLMPROXY_TEST_ADMIN_PASS", "admin")
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


def pytest_sessionfinish(session, exitstatus):
    """v3.1.x: hard-purge tombstoned api_keys created by tests in this session
    (or any prior session that died before cleanup ran).

    Without this, every soft-delete from a session-scoped fixture leaves a
    row in the cluster_sync apply pass for the full 7-day tombstone
    retention window. Across many CI runs this slows apply_sync the same
    way the 2026-05-07 incident did (127 stale tombstones → ~3s sync apply
    per cycle).

    Calls the admin-only ``/api/keys/_purge-test-tombstones`` endpoint
    which hard-deletes rows whose name matches a test pattern AND whose
    ``deleted_at`` is older than 60s (cluster-sync convergence buffer).
    Best-effort — failures don't fail the session.

    v4.4.24 (F-INFRA-001) — gated behind ``LLMPROXY_TEST_PURGE_LIVE=1``.
    This hook POSTs to the LIVE production deployment, which made the
    pure unit suite non-hermetic: ``pytest tests/unit/`` hit
    ``www.voipguru.org`` at session-finish even with no integration
    tests selected, breaking CI portability and tripping ``-W error``
    on the InsecureRequestWarning. Integration runs that create
    test-scoped tombstones still want this cleanup, so set the env var
    in those contexts. Default OFF keeps unit runs self-contained.
    """
    import os
    if os.environ.get("LLMPROXY_TEST_PURGE_LIVE") != "1":
        return
    try:
        s = _api_session()
        r = s.post(f"{BASE_URL}/api/keys/_purge-test-tombstones", timeout=10)
        if r.status_code == 200:
            purged = r.json().get("purged", 0)
            if purged:
                print(f"\n[session-finish] purged {purged} test-key tombstones")
        # v3.5.11 BUG-003 fix — also purge pytest-mock provider rows.
        # Pre-fix these soft-deleted rows survived 7 days until the
        # daily prune worker swept them, bloating the providers table
        # and confusing operators reading /api/providers raw output.
        # Mirror the api-keys purge above. Best-effort.
        r2 = s.post(f"{BASE_URL}/api/providers/_purge-test-tombstones", timeout=10)
        if r2.status_code == 200:
            purged2 = r2.json().get("purged", 0)
            if purged2:
                print(f"[session-finish] purged {purged2} test-provider tombstones")
    except Exception as e:
        # Test session has already finished; don't let a cleanup error
        # mask test results or leak a non-zero exit.
        print(f"\n[session-finish] purge failed (best-effort): {e}")


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
