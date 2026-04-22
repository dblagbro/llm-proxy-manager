"""
Integration-test conftest — fixtures that manipulate live proxy state.
"""
import time

import pytest
import requests

from tests.conftest import BASE_URL, ADMIN_USER, ADMIN_PASS


def _llm_headers(api_key: str) -> dict:
    return {"x-api-key": api_key, "Content-Type": "application/json"}


def collect_sse(resp: requests.Response) -> list[dict]:
    """Parse SSE stream, return list of JSON event dicts (excluding [DONE])."""
    import json
    events = []
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode()
        if line.startswith("data: ") and line[6:] != "[DONE]":
            try:
                events.append(json.loads(line[6:]))
            except Exception:
                pass
    return events


@pytest.fixture
def llm_headers(test_api_key):
    return _llm_headers(test_api_key)


@pytest.fixture
def cot_headers(cot_api_key):
    return _llm_headers(cot_api_key)


@pytest.fixture
def only_mock_routing(admin_session, all_non_mock_providers, mock_server):
    """
    Force-open circuit breakers on ALL non-mock providers so the mock is the only
    available route.  Uses all_non_mock_providers (includes unconfigured ones) so
    providers without API keys don't bypass the mock.  Resets all CBs in teardown.
    """
    tripped = []
    for p in all_non_mock_providers:
        r = admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{p['id']}/open")
        if r.status_code == 200:
            tripped.append(p["id"])
    time.sleep(0.5)  # let state propagate
    yield mock_server
    for pid in tripped:
        admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{pid}/reset")
    time.sleep(0.3)


@pytest.fixture
def mock_ctl(mock_server):
    """Per-test helper: queue responses on the mock and read what it received."""
    mock_server["srv"].clear_received()

    class Ctl:
        def queue(self, **kwargs):
            mock_server["srv"].queue_response(**kwargs)

        def received(self) -> list[dict]:
            return mock_server["srv"].get_received()

        def last(self) -> dict:
            r = self.received()
            return r[-1] if r else {}

    return Ctl()
