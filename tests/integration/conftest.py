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
    """Parse SSE stream, return list of JSON event dicts.

    Excludes:
      - ``[DONE]`` sentinel
      - Events from non-default ``event:`` channels (e.g. the proxy's
        ``event: budget`` heartbeat) — those use a different payload
        shape and Anthropic-Messages tests don't expect them.

    The ``event: <kind>`` line precedes the ``data:`` line on a custom
    channel; we track the most recent ``event:`` and skip its data.
    """
    import json
    events = []
    current_event_channel = None  # None = default Anthropic stream
    for line in resp.iter_lines():
        if isinstance(line, bytes):
            line = line.decode()
        if line.startswith("event: "):
            current_event_channel = line[7:].strip()
            continue
        if line == "":
            current_event_channel = None  # blank line resets per SSE spec
            continue
        if line.startswith("data: ") and line[6:] != "[DONE]":
            if current_event_channel is not None:
                # Skip non-default-channel data (budget, error, run_recovered, etc.)
                continue
            try:
                evt = json.loads(line[6:])
            except Exception:
                continue
            if isinstance(evt, dict):
                events.append(evt)
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
    Force-open circuit breakers AND temporarily disable ALL non-mock providers so
    the mock is the only available route. Belt-and-suspenders: CB-open alone
    didn't always keep traffic off broken anthropic providers in claude-oauth
    dispatch (which short-circuits CB checks). Restores enabled state and CB
    state on teardown.

    v2.7.8 BUG-005: previously CB-open only — broken real providers leaked into
    streaming tests, causing 7 spurious failures.
    """
    tripped: list[str] = []
    disabled: list[str] = []  # (id, was_originally_enabled) — only restore if it was enabled
    for p in all_non_mock_providers:
        # CB open
        r = admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{p['id']}/open")
        if r.status_code == 200:
            tripped.append(p["id"])
        # Disable if currently enabled
        if p.get("enabled"):
            r = admin_session.patch(f"{BASE_URL}/api/providers/{p['id']}/toggle")
            if r.status_code == 200 and r.json().get("enabled") is False:
                disabled.append(p["id"])
    time.sleep(0.5)  # let state propagate
    yield mock_server
    # Restore: re-toggle enabled providers, then reset CBs
    for pid in disabled:
        admin_session.patch(f"{BASE_URL}/api/providers/{pid}/toggle")
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
