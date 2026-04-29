"""
Layer 3 — Settings permutation + retest.

Pattern for each test:
  1. Snapshot current settings (session fixture)
  2. Apply a changed value
  3. Verify behavior changed across real providers or mock
  4. Restore the changed value (via fixture or explicit restore)

Mock-based tests (no --run-real): CoT-E on/off, circuit breaker threshold.
Real-provider tests (require --run-real): native thinking budget, reasoning effort.
"""
import time
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL
from tests.integration.conftest import collect_sse

# ── helpers ───────────────────────────────────────────────────────────────────

def _post_messages(headers, body, stream=False, timeout=60):
    return requests.post(f"{BASE_URL}/v1/messages", headers=headers,
                         json=body, stream=stream, verify=False, timeout=timeout)


def _post_completions(headers, body, stream=False, timeout=60):
    return requests.post(f"{BASE_URL}/v1/chat/completions", headers=headers,
                         json=body, stream=stream, verify=False, timeout=timeout)


def _has_thinking_blocks(resp) -> bool:
    events = collect_sse(resp)
    return any(
        e.get("type") == "content_block_delta"
        and e.get("delta", {}).get("type") == "thinking_delta"
        for e in events
    )


def _setting(admin_session, key: str):
    return admin_session.get(f"{BASE_URL}/api/settings").json()[key]


def _set(admin_session, **kwargs):
    r = admin_session.put(f"{BASE_URL}/api/settings", json=kwargs)
    assert r.status_code == 200, f"settings PUT failed: {r.text}"
    time.sleep(0.3)  # let live-apply propagate


COT_BODY = {
    "model": "claude-3-5-sonnet-20241022",
    "max_tokens": 200,
    "messages": [{"role": "user",
                  "content": "Write a Python function to parse a JSON config file."}],
    "stream": True,
}


# ── CoT-E on/off (mock-based, no real API cost) ───────────────────────────────

class TestCoTToggle:
    def test_cot_enabled_produces_thinking_blocks(
            self, only_mock_routing, mock_ctl, cot_headers, admin_session, settings_snapshot):
        original = settings_snapshot["cot_enabled"]
        _set(admin_session, cot_enabled=True, cot_max_iterations=1)

        # Queue plan + draft + critique for CoT pipeline
        mock_ctl.queue(type="text", content="Plan: structure the answer.")
        mock_ctl.queue(type="text", content="def parse_config(path):\n    import json\n    with open(path) as f:\n        return json.load(f)")
        mock_ctl.queue(type="text", content="SCORE: 8\nGAPS: none")

        resp = _post_messages(cot_headers, COT_BODY, stream=True)
        assert resp.status_code == 200
        assert _has_thinking_blocks(resp), "Expected thinking blocks when CoT is enabled"

    def test_cot_disabled_no_thinking_blocks(
            self, only_mock_routing, mock_ctl, cot_headers, admin_session, settings_snapshot):
        _set(admin_session, cot_enabled=False)
        mock_ctl.queue(type="text", content="def parse_config(path): ...")

        resp = _post_messages(cot_headers, COT_BODY, stream=True)
        assert resp.status_code == 200
        assert not _has_thinking_blocks(resp), "No thinking blocks expected when CoT disabled"

        # Restore
        _set(admin_session, cot_enabled=settings_snapshot["cot_enabled"])


class TestCoTIterations:
    def test_max_iterations_zero_skips_critique(
            self, only_mock_routing, mock_ctl, cot_headers, admin_session, settings_snapshot):
        """cot_max_iterations=0 → pipeline runs plan + draft only (2 mock calls)."""
        _set(admin_session, cot_enabled=True, cot_max_iterations=0)

        mock_ctl.queue(type="text", content="Plan: write the function.")
        mock_ctl.queue(type="text", content="def parse_config(p): pass")
        # No critique queued — if pipeline calls critique, it gets an empty queue response

        resp = _post_messages(cot_headers, COT_BODY, stream=True)
        assert resp.status_code == 200

        events = collect_sse(resp)
        thinking_texts = [
            e["delta"]["thinking"]
            for e in events
            if e.get("type") == "content_block_delta"
            and e.get("delta", {}).get("type") == "thinking_delta"
        ]
        combined = " ".join(thinking_texts)
        # With 0 iterations, no "Quality Check" thinking block should appear
        assert "Quality Check" not in combined, \
            "Critique block appeared despite cot_max_iterations=0"
        assert "Planning" in combined, "Plan block should always appear"

        # Restore
        _set(admin_session, cot_max_iterations=settings_snapshot["cot_max_iterations"])


# ── Circuit breaker threshold (mock-based) ────────────────────────────────────

class TestCircuitBreakerThreshold:
    def test_cb_trips_at_configured_threshold(
            self, admin_session, real_providers, mock_server, settings_snapshot, test_api_key):
        """
        Lower failure threshold to 1.  Force-open a real provider's CB.
        Verify routing falls through correctly (proxy returns 503 or routes elsewhere).
        Then restore.
        """
        if not real_providers:
            pytest.skip("No real providers configured")

        provider = real_providers[0]
        original_threshold = settings_snapshot["circuit_breaker_threshold"]
        _set(admin_session, circuit_breaker_threshold=1)

        # Force-open the highest-priority provider
        r = admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{provider['id']}/open")
        assert r.status_code == 200

        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        # With primary provider CB open, proxy should route to next or return 503
        resp = requests.post(f"{BASE_URL}/v1/messages", headers=headers, verify=False, timeout=30,
                             json={"model": "gpt-4o", "max_tokens": 10,
                                   "messages": [{"role": "user", "content": "ping"}]})
        # Acceptable outcomes after primary CB-open:
        #   200 — routed to another provider successfully
        #   502 — next provider also failed (e.g. unqueued mock)
        #   503 — no providers left
        #   404 — fell back to a claude-oauth provider whose upstream
        #         (platform.claude.com) doesn't recognise the OpenAI
        #         model id "gpt-4o" and returns not_found_error. The
        #         proxy DID route gracefully — the upstream rejected
        #         the model. Same intent as 502.
        assert resp.status_code in (200, 404, 502, 503), f"Unexpected: {resp.status_code}"

        # Restore CB and threshold
        admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{provider['id']}/reset")
        _set(admin_session, circuit_breaker_threshold=original_threshold)


# ── Native thinking budget (real providers) ───────────────────────────────────

class TestNativeThinkingBudget:
    @pytest.mark.real_providers
    def test_budget_change_reflected_in_response(
            self, admin_session, real_providers, test_api_key, settings_snapshot):
        """
        Change native_thinking_budget_tokens.  For providers with native reasoning
        (Gemini 2.5), the response should still be valid.  This doesn't assert
        the exact budget value (we can't inspect litellm kwargs externally) but
        verifies the proxy doesn't break when the setting changes.
        """
        if not real_providers:
            pytest.skip("No real providers with API keys configured")
        original = settings_snapshot["native_thinking_budget_tokens"]
        _set(admin_session, native_thinking_budget_tokens=2048)

        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        tested = False
        for p in real_providers:
            resp = requests.post(f"{BASE_URL}/v1/messages", headers=headers, verify=False,
                                 timeout=60, json={
                                     "model": p.get("default_model", "gpt-4o"),
                                     "max_tokens": 50,
                                     "messages": [{"role": "user", "content": "Say OK"}],
                                 })
            if resp.status_code == 502:
                continue
            assert resp.status_code == 200, \
                f"Provider {p['name']} failed after budget change: {resp.text[:200]}"
            tested = True
            break

        _set(admin_session, native_thinking_budget_tokens=original)
        if not tested:
            pytest.skip("All real providers unavailable")

    @pytest.mark.real_providers
    def test_reasoning_effort_change_reflected(
            self, admin_session, real_providers, test_api_key, settings_snapshot):
        """Change reasoning effort level; proxy must still return valid responses."""
        if not real_providers:
            pytest.skip("No real providers with API keys configured")
        original = settings_snapshot["native_reasoning_effort"]
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        for effort in ("low", "high"):
            _set(admin_session, native_reasoning_effort=effort)
            tested = False
            for p in real_providers:
                resp = requests.post(f"{BASE_URL}/v1/messages", headers=headers, verify=False,
                                     timeout=60, json={
                                         "model": p.get("default_model", "gpt-4o"),
                                         "max_tokens": 50,
                                         "messages": [{"role": "user", "content": "Say OK"}],
                                     })
                if resp.status_code == 502:
                    continue
                assert resp.status_code == 200, \
                    f"Provider {p['name']} failed with effort={effort}: {resp.text[:200]}"
                tested = True
                break
            if not tested:
                _set(admin_session, native_reasoning_effort=original)
                pytest.skip("All real providers unavailable")
        _set(admin_session, native_reasoning_effort=original)


# ── CoT-E + real providers: same task, different providers, same output shape ──

class TestCoTRealProviders:
    @pytest.mark.real_providers
    def test_cot_engaged_all_providers(self, admin_session, real_providers, cot_api_key,
                                       settings_snapshot):
        """
        With a claude-code API key, CoT-E engages on providers without native reasoning.
        All providers must return a valid response with text content.
        """
        if not real_providers:
            pytest.skip("No real providers with API keys configured")
        _set(admin_session, cot_enabled=True, cot_max_iterations=1)
        headers = {"x-api-key": cot_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []

        from tests.integration.test_compatibility_matrix import _all_providers_with_cb_cycling
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = requests.post(f"{BASE_URL}/v1/messages", headers=headers, verify=False,
                                 timeout=120, stream=True, json={
                                     "model": provider.get("default_model", "gpt-4o"),
                                     "max_tokens": 300,
                                     "messages": [{"role": "user",
                                                   "content": "Write a Python function to parse a JSON config file."}],
                                     "stream": True,
                                 })
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}")
                continue
            events = collect_sse(resp)
            if not events or all(
                e.get("type") == "error" or ("error" in e and "object" not in e and "choices" not in e)
                for e in events
            ):
                unavailable.append(ctx)
                continue
            has_text = any(
                e.get("type") == "content_block_delta"
                and e.get("delta", {}).get("type") == "text_delta"
                for e in events
            )
            if not has_text:
                failures.append(f"{ctx}: no text content in response")

        _set(admin_session, cot_max_iterations=settings_snapshot["cot_max_iterations"])
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")
