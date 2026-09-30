"""
Root conftest — session-scoped fixtures shared by all test layers.
"""
import os
import time
import uuid

import pytest
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import os as _os

# ── Where the integration suite points ───────────────────────────────────────
#
# v5.22.38 — three modes, and NONE of them is "production by default".
#
#   1. LLMPROXY_TEST_EPHEMERAL=1 — boot a private throwaway instance for this
#      run (see tests/_ephemeral.py). Self-contained: no network, no shared
#      deployment, admin/admin, deleted at exit. This is what CI uses and what
#      you want locally almost always.
#   2. LLMPROXY_TEST_BASE_URL=<url> with LLMPROXY_TEST_LIVE=1 — point at a real
#      deployment on purpose (the smoke instance, a dev node).
#   3. Neither — every live test SKIPS, and BASE_URL is a deliberately
#      unroutable sentinel.
#
# Mode 3's sentinel matters. Before this, BASE_URL defaulted to
# https://www.voipguru.org/llm-proxy2 — PRODUCTION — and the guard against
# using it was ``require_live_deployment()``, reached only via the
# ``admin_session`` fixture. Any test that built its own ``requests.Session``
# or Playwright ``page`` from BASE_URL walked straight past it: measured
# 2026-09-28 as 3 failures + 71 errors against the live deployment from a bare
# ``pytest tests/integration``. The skip is now structural (see
# ``pytest_collection_modifyitems``), and the sentinel means that even if a
# future test finds a way around it, it connects to nothing instead of to
# production.
_EPHEMERAL_REQUESTED = _os.environ.get("LLMPROXY_TEST_EPHEMERAL") == "1"
_EXPLICIT_BASE_URL = _os.environ.get("LLMPROXY_TEST_BASE_URL")

# ``.invalid`` is reserved by RFC 2606 and can never resolve.
_UNROUTABLE = "http://llmproxy-tests-not-configured.invalid/llm-proxy2"

if _EPHEMERAL_REQUESTED:
    from tests._ephemeral import start_ephemeral
    BASE_URL = start_ephemeral()
elif _EXPLICIT_BASE_URL:
    BASE_URL = _EXPLICIT_BASE_URL
else:
    BASE_URL = _UNROUTABLE

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
# v5.22.38 — the proxy has to be able to reach the mock. A containerised
# deployment reaches the host over the docker bridge; an ephemeral instance is a
# plain host process, so for it the bridge IP is wrong and loopback is right.
MOCK_HOST = "127.0.0.1" if _os.environ.get("LLMPROXY_TEST_EPHEMERAL") == "1" else DOCKER_BRIDGE_IP
MOCK_BASE_URL = f"http://{MOCK_HOST}:{MOCK_PORT}"


def pytest_addoption(parser):
    parser.addoption(
        "--run-real",
        action="store_true",
        default=False,
        help="Run real-provider compatibility and settings-permutation tests (costs API credits)",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "real_providers: needs real LLM calls — use --run-real to enable")
    config.addinivalue_line("markers", "live_deployment: needs a deployment to run against")
    config.addinivalue_line(
        "markers",
        "shared_deployment: needs the shared nginx estate specifically (other "
        "services, cluster peers) — cannot run against an ephemeral instance",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-real"):
        skip = pytest.mark.skip(reason="real-provider test — pass --run-real to enable")
        for item in items:
            if "real_providers" in item.keywords:
                item.add_marker(skip)

    # ── v5.22.38: the live gate, moved from the fixtures to collection ───────
    #
    # ``require_live_deployment()`` is only reached through ``_api_session()``,
    # i.e. only by tests that use the ``admin_session`` fixture. Tests that
    # build their own ``requests.Session()`` or Playwright ``page`` from
    # BASE_URL never call it. Measured 2026-09-28 on a bare
    # ``pytest tests/integration``: 78 correctly skipped, but 71 errors and 3
    # failures went to the live deployment anyway — test_playwright_ui.py (its
    # own browser fixture), test_auth.py (its own Session) and
    # test_manual_override_flow.py (its own page).
    #
    # A gate a test can skip by not using a fixture is not a gate. Everything
    # under tests/integration/ is now marked ``live_deployment`` by location,
    # so opting out requires editing this hook rather than forgetting a
    # fixture. Individual tests elsewhere can carry the marker explicitly.
    # Tests that need the shared estate itself — the v1 proxy, the coordinator
    # hub, paperless, real cluster peers — cannot be served by a private
    # instance, so they skip whenever the target is ephemeral. They are not
    # "live tests that happen to be picky": an ephemeral run would have to
    # stand up four unrelated applications to satisfy them.
    if not LIVE_TARGET_IS_SHARED:
        skip_shared = pytest.mark.skip(
            reason="needs the shared nginx estate — set LLMPROXY_TEST_LIVE=1 with "
                   "LLMPROXY_TEST_BASE_URL pointed at a real deployment"
        )
        for item in items:
            if "shared_deployment" in item.keywords:
                item.add_marker(skip_shared)

    if LIVE_TESTS_ENABLED:
        return
    skip_live = pytest.mark.skip(reason=_LIVE_SKIP_REASON)
    for item in items:
        path = str(getattr(item, "fspath", "") or "")
        in_integration = f"{os.sep}tests{os.sep}integration{os.sep}" in path
        if in_integration or "live_deployment" in item.keywords:
            item.add_marker(skip_live)


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
    # v5.22.16 — one master switch for "may touch the live deployment".
    # v5.22.38 — and never against an ephemeral instance: its DB is deleted
    # seconds from now, so purging tombstones in it is pure waste.
    if not LIVE_TARGET_IS_SHARED:
        print("\n[session-finish] purge skipped — target is not a shared deployment")
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


# v5.22.16 — live-deployment access is opt-in.
#
# BASE_URL defaults to https://www.voipguru.org/llm-proxy2 — PRODUCTION. Every
# fixture below authenticates as admin against it and several create and
# delete API keys there, so `pytest tests/unit` on an operator's machine was
# quietly mutating the live deployment. It was not obvious, because on a box
# that can reach production these tests simply pass.
#
# The same fixtures are why the full suite could not be gated in CI: on a
# clean runner they fail rather than skip (no network to voipguru, no admin
# password), so a genuine regression is indistinguishable from "no deployment
# here".
#
# Both problems have one fix: require an explicit opt-in, and SKIP without it.
# Unit runs become self-contained by default, CI can gate the whole suite, and
# touching production becomes a deliberate act.
# v5.22.38 — an ephemeral instance IS a deployment, just a private one, so it
# satisfies the gate without anyone opting into touching a shared environment.
LIVE_TESTS_ENABLED = _EPHEMERAL_REQUESTED or _os.environ.get("LLMPROXY_TEST_LIVE") == "1"

# True only when the target is someone else's deployment. Guards the
# session-finish tombstone purge, which is pointless against a DB that is about
# to be deleted and rude against one that is not.
LIVE_TARGET_IS_SHARED = LIVE_TESTS_ENABLED and not _EPHEMERAL_REQUESTED

_LIVE_SKIP_REASON = (
    "needs a deployment to run against. Easiest: LLMPROXY_TEST_EPHEMERAL=1, "
    "which boots a private throwaway instance for this run and needs no "
    "network. To target a real deployment instead, set LLMPROXY_TEST_LIVE=1 "
    "and LLMPROXY_TEST_BASE_URL — and note that production is a shared "
    "environment these tests write to."
)


def require_live_deployment() -> None:
    """Skip the calling test unless live-deployment access was opted into."""
    if not LIVE_TESTS_ENABLED:
        pytest.skip(_LIVE_SKIP_REASON)


def _api_session() -> requests.Session:
    """New session with admin credentials and API-friendly headers."""
    require_live_deployment()
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


# ── Ephemeral runs need at least one provider ─────────────────────────────────
#
# v5.22.38 — a freshly booted instance has an empty providers table, so anything
# that actually calls an LLM endpoint gets
# ``503 No providers configured. Operator action: enable at least one provider``.
# Measured: 14 of 66 runnable integration tests failed on exactly that.
#
# The suite already has the machinery — ``mock_server`` starts the local mock LLM
# and registers it as a provider — but only tests that *request* that fixture got
# it. Against a shared deployment that was fine, because real providers were
# already configured there. An ephemeral instance has to provide its own.
#
# Autouse only in ephemeral mode: against a real deployment this must not
# silently add a provider row.
@pytest.fixture(scope="session", autouse=True)
def _ephemeral_default_provider(request):
    if not _EPHEMERAL_REQUESTED:
        yield
        return
    # Reuse the existing fixture rather than duplicating registration, so the
    # mock's lifecycle and teardown stay in one place.
    request.getfixturevalue("mock_server")
    yield
