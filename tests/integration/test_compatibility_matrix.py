"""
Layer 2 — Real-provider compatibility matrix.
Runs against every enabled provider via circuit-breaker cycling.
Each test verifies that the proxy delivers a usable response for
a task type regardless of which model is underneath, and that the
client-visible format is identical across providers.

Requires: --run-real flag  (costs API credits)
"""
import json
import time
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL
from tests.integration.conftest import collect_sse

pytestmark = pytest.mark.real_providers

# ── Task-type prompts with structural pass criteria ───────────────────────────

TASKS = {
    "coding": {
        "prompt": "Write a Python function named `parse_config` that reads a JSON file "
                  "and returns a dict. Include basic error handling.",
        "check": lambda text: "```" in text and "def parse_config" in text,
        "description": "contains Python code block with function definition",
    },
    "debugging": {
        "prompt": (
            "Here is a Python traceback:\n\n"
            "KeyError: 'username'\n  File 'app.py', line 42, in handle_login\n"
            "    uid = session['username']\n\n"
            "What is the most likely cause and how do you fix it?"
        ),
        "check": lambda text: "key" in text.lower() and len(text) > 80,
        "description": "mentions 'key' and gives a non-trivial answer",
    },
    "config": {
        "prompt": "Write a minimal nginx location block that reverse-proxies "
                  "requests from /api/ to http://localhost:8000. "
                  "Include proxy_pass and proxy_set_header Host.",
        "check": lambda text: "location" in text and "proxy_pass" in text,
        "description": "contains nginx location and proxy_pass directives",
    },
    "troubleshooting": {
        "prompt": "A Docker container exits immediately with code 1. "
                  "List at least 3 diagnostic steps.",
        "check": lambda text: sum(1 for marker in ["1.", "2.", "3.", "•", "-", "*"]
                                  if marker in text) >= 2,
        "description": "contains at least 2 list markers",
    },
}

TOOL_DEF_ANTHROPIC = {
    "name": "get_status",
    "description": "Check the status of a service",
    "input_schema": {
        "type": "object",
        "properties": {"service": {"type": "string", "description": "Service name"}},
        "required": ["service"],
    },
}

TOOL_DEF_OPENAI = {
    "type": "function",
    "function": {
        "name": "get_status",
        "description": "Check the status of a service",
        "parameters": {
            "type": "object",
            "properties": {"service": {"type": "string"}},
            "required": ["service"],
        },
    },
}


# ── Fixture: iterate through all real providers via CB cycling ─────────────────

def _all_providers_with_cb_cycling(admin_session, real_providers):
    """
    Generator: for each provider in priority order, yield (provider, tripped_ids).
    All previously-tested providers have their CBs force-opened so routing
    falls through to the next one.  Restores all CBs at StopIteration.
    """
    tripped = []
    try:
        for provider in real_providers:
            # Trip all previously tested providers
            for pid in tripped:
                admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{pid}/open")
            if tripped:
                time.sleep(0.5)
            yield provider
            tripped.append(provider["id"])
    finally:
        for pid in tripped:
            admin_session.post(f"{BASE_URL}/cluster/circuit-breaker/{pid}/reset")
        if tripped:
            time.sleep(0.5)


# ── helpers ───────────────────────────────────────────────────────────────────

def _post(url, headers, body, stream=False, timeout=60):
    return requests.post(url, headers=headers, json=body,
                         stream=stream, verify=False, timeout=timeout)


def _anthropic_text(resp_json: dict) -> str:
    return " ".join(b.get("text", "") for b in resp_json.get("content", [])
                    if b.get("type") == "text")


def _openai_text(resp_json: dict) -> str:
    return resp_json.get("choices", [{}])[0].get("message", {}).get("content", "") or ""


def _assert_anthropic_shape(data: dict, context: str = ""):
    assert data.get("type") == "message", f"{context}: expected type=message, got {data}"
    assert data.get("role") == "assistant", f"{context}: expected role=assistant"
    assert isinstance(data.get("content"), list), f"{context}: content must be list"
    assert any(b.get("type") == "text" for b in data["content"]), \
        f"{context}: expected at least one text block"
    assert data.get("stop_reason"), f"{context}: missing stop_reason"
    assert "usage" in data, f"{context}: missing usage"
    assert data["usage"].get("output_tokens", 0) > 0, f"{context}: zero output tokens"


def _assert_openai_shape(data: dict, context: str = ""):
    assert data.get("object") == "chat.completion", \
        f"{context}: expected chat.completion, got {data.get('object')}"
    assert data.get("choices"), f"{context}: empty choices"
    msg = data["choices"][0].get("message", {})
    assert msg.get("role") == "assistant", f"{context}: expected assistant role"
    assert data["choices"][0].get("finish_reason"), f"{context}: missing finish_reason"
    assert "usage" in data, f"{context}: missing usage"


def _skip_if_no_providers(real_providers):
    if not real_providers:
        pytest.skip("No real providers with API keys configured")


def _stream_failed(events: list) -> bool:
    """True if SSE stream is empty or contains only error events (provider billing/auth failure).
    Handles both Anthropic format (type=error) and OpenAI format ({error: ...} with no object)."""
    if not events:
        return True
    return all(
        e.get("type") == "error"  # Anthropic error event
        or ("error" in e and "object" not in e and "choices" not in e)  # OpenAI error event
        for e in events
    )


# ── Wire format equivalence across providers ──────────────────────────────────

class TestWireFormatPerProvider:
    def test_anthropic_non_stream_all_providers(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Say OK"}],
            })
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            try:
                _assert_anthropic_shape(resp.json(), ctx)
            except AssertionError as e:
                failures.append(str(e))
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")

    def test_openai_non_stream_all_providers(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/chat/completions", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Say OK"}],
            })
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}: {resp.text[:200]}")
                continue
            try:
                _assert_openai_shape(resp.json(), ctx)
            except AssertionError as e:
                failures.append(str(e))
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")

    def test_anthropic_stream_all_providers(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Say OK"}],
                "stream": True,
            }, stream=True)
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}")
                continue
            events = collect_sse(resp)
            if _stream_failed(events):
                unavailable.append(ctx)
                continue
            types = {e.get("type") for e in events}
            required = {"message_start", "content_block_start", "content_block_stop",
                        "message_delta", "message_stop"}
            missing = required - types
            if missing:
                failures.append(f"{ctx}: missing SSE event types: {missing}")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")

    def test_openai_stream_all_providers(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/chat/completions", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 20,
                "messages": [{"role": "user", "content": "Say OK"}],
                "stream": True,
            }, stream=True)
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}")
                continue
            chunks = collect_sse(resp)
            if _stream_failed(chunks):
                unavailable.append(ctx)
                continue
            bad = [c for c in chunks if c.get("object") != "chat.completion.chunk"]
            if bad:
                failures.append(f"{ctx}: chunk has wrong object type: {bad[0]}")
            elif not chunks[-1]["choices"][0].get("finish_reason"):
                failures.append(f"{ctx}: last chunk missing finish_reason")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")

    def test_llm_capability_header_all_providers(self, admin_session, real_providers, test_api_key):
        """LLM-Capability header must be present on every response."""
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            })
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}")
                continue
            cap = resp.headers.get("LLM-Capability") or resp.headers.get("llm-capability")
            if not cap:
                failures.append(f"{ctx}: LLM-Capability header missing")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")


# ── Task-type response quality across providers ───────────────────────────────

class TestTaskTypePerProvider:
    @pytest.mark.parametrize("task_name,task", list(TASKS.items()))
    def test_task_completeness(self, admin_session, real_providers, test_api_key,
                               task_name, task):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        failures = []
        unavailable = []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            resp = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 400,
                "messages": [{"role": "user", "content": task["prompt"]}],
            }, timeout=90)
            ctx = f"Provider {provider['name']}, task={task_name}"
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code}")
                continue
            text = _anthropic_text(resp.json())
            if not task["check"](text):
                failures.append(f"{ctx}: response doesn't {task['description']}. "
                                 f"Got: {text[:200]}")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")


# ── Multi-turn context preservation across providers ──────────────────────────

class TestMultiTurnPerProvider:
    def test_multi_turn_context(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            model = provider.get("default_model", "gpt-4o")
            # Turn 1
            r1 = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": model, "max_tokens": 150,
                "messages": [{"role": "user",
                               "content": "Define a Python class named `Stack` with push and pop."}],
            }, timeout=60)
            if r1.status_code == 502:
                unavailable.append(ctx)
                continue
            if r1.status_code != 200:
                failures.append(f"{ctx} turn1: HTTP {r1.status_code}")
                continue
            t1_text = _anthropic_text(r1.json())
            if "Stack" not in t1_text and "class" not in t1_text.lower():
                failures.append(f"{ctx}: turn1 didn't mention Stack. Got: {t1_text[:200]}")
                continue
            # Turn 2 — reference prior context
            r2 = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": model, "max_tokens": 150,
                "messages": [
                    {"role": "user",
                     "content": "Define a Python class named `Stack` with push and pop."},
                    {"role": "assistant", "content": t1_text},
                    {"role": "user", "content": "Now add a `peek` method to the Stack class."},
                ],
            }, timeout=60)
            if r2.status_code == 502:
                unavailable.append(f"{ctx} turn2")
                continue
            if r2.status_code != 200:
                failures.append(f"{ctx} turn2: HTTP {r2.status_code}")
                continue
            t2_text = _anthropic_text(r2.json())
            if "peek" not in t2_text.lower():
                failures.append(f"{ctx}: turn2 doesn't reference 'peek'. Got: {t2_text[:200]}")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) >= len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")


# ── Native tool use across providers ─────────────────────────────────────────

class TestNativeToolUsePerProvider:
    def test_tool_call_structure_all_providers(self, admin_session, real_providers, test_api_key):
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            if provider.get("exclude_from_tool_requests"):
                continue
            ctx = f"Provider {provider['name']}"
            resp = _post(f"{BASE_URL}/v1/messages", headers, {
                "model": provider.get("default_model", "gpt-4o"),
                "max_tokens": 100,
                "tools": [TOOL_DEF_ANTHROPIC],
                "messages": [{"role": "user",
                               "content": "Check the status of the nginx service using the tool."}],
            }, timeout=60)
            if resp.status_code == 502:
                unavailable.append(ctx)
                continue
            if resp.status_code != 200:
                failures.append(f"{ctx}: HTTP {resp.status_code} {resp.text[:100]}")
                continue
            d = resp.json()
            tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
            text_blocks = [b for b in d.get("content", []) if b.get("type") == "text"]
            if not (tool_blocks or text_blocks):
                failures.append(f"{ctx}: no content blocks in response")
            elif tool_blocks:
                tb = tool_blocks[0]
                if tb.get("name") != "get_status" and "service" not in str(tb.get("input", {})):
                    failures.append(f"{ctx}: tool block doesn't reference get_status")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) == len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")


# ── Streaming vs non-streaming consistency ────────────────────────────────────

class TestStreamConsistencyPerProvider:
    def test_stream_non_stream_content_equivalent(self, admin_session, real_providers, test_api_key):
        """
        Streamed and non-streamed responses for the same prompt should contain
        equivalent content (both mention 'sum' for a sum-list question).
        """
        _skip_if_no_providers(real_providers)
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        prompt = "In one sentence, what does sum([1, 2, 3]) return in Python?"
        unavailable, failures = [], []
        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            ctx = f"Provider {provider['name']}"
            model = provider.get("default_model", "gpt-4o")
            body = {"model": model, "max_tokens": 60,
                    "messages": [{"role": "user", "content": prompt}]}
            r_plain = _post(f"{BASE_URL}/v1/messages", headers, body)
            if r_plain.status_code == 502:
                unavailable.append(ctx)
                continue
            if r_plain.status_code != 200:
                failures.append(f"{ctx} plain: HTTP {r_plain.status_code}")
                continue
            plain_text = _anthropic_text(r_plain.json()).lower()
            r_stream = _post(f"{BASE_URL}/v1/messages", headers, {**body, "stream": True},
                             stream=True)
            if r_stream.status_code == 502:
                unavailable.append(f"{ctx} stream")
                continue
            if r_stream.status_code != 200:
                failures.append(f"{ctx} stream: HTTP {r_stream.status_code}")
                continue
            events = collect_sse(r_stream)
            if _stream_failed(events):
                unavailable.append(f"{ctx} stream")
                continue
            stream_text = "".join(
                e["delta"]["text"] for e in events
                if e.get("type") == "content_block_delta"
                and e.get("delta", {}).get("type") == "text_delta"
            ).lower()
            if "6" not in plain_text and "six" not in plain_text:
                failures.append(f"{ctx} plain: answer doesn't mention 6. Got: {plain_text}")
            if "6" not in stream_text and "six" not in stream_text:
                failures.append(f"{ctx} stream: answer doesn't mention 6. Got: {stream_text}")
        if failures:
            pytest.fail("\n".join(failures))
        if len(unavailable) >= len(real_providers):
            pytest.skip(f"All providers unavailable: {unavailable}")


# ── Compatibility summary report ──────────────────────────────────────────────

class TestCompatibilitySummary:
    def test_generate_matrix(self, admin_session, real_providers, test_api_key,
                              settings_snapshot):
        """
        Non-asserting test: runs all providers through a quick capability probe
        and prints a Markdown summary table.  Always passes.
        """
        import os, datetime
        headers = {"x-api-key": test_api_key, "Content-Type": "application/json"}
        rows = []

        for provider in _all_providers_with_cb_cycling(admin_session, real_providers):
            name = provider["name"]
            row = {"provider": name, "text": "?", "stream": "?", "tools": "?", "cot_e": "?"}
            model = provider.get("default_model", "gpt-4o")

            # Text
            try:
                r = _post(f"{BASE_URL}/v1/messages", headers, {
                    "model": model, "max_tokens": 15,
                    "messages": [{"role": "user", "content": "Say OK"}],
                }, timeout=30)
                row["text"] = "✓" if r.status_code == 200 else f"✗({r.status_code})"
            except Exception as e:
                row["text"] = f"✗(err)"

            # Stream
            try:
                r = _post(f"{BASE_URL}/v1/messages", headers, {
                    "model": model, "max_tokens": 15,
                    "messages": [{"role": "user", "content": "Say OK"}],
                    "stream": True,
                }, stream=True, timeout=30)
                evts = collect_sse(r)
                row["stream"] = "✓" if r.status_code == 200 and evts else f"✗"
            except Exception:
                row["stream"] = "✗"

            # Tools
            try:
                r = _post(f"{BASE_URL}/v1/messages", headers, {
                    "model": model, "max_tokens": 60,
                    "tools": [TOOL_DEF_ANTHROPIC],
                    "messages": [{"role": "user",
                                  "content": "Use get_status to check nginx."}],
                }, timeout=45)
                d = r.json()
                has_tool = any(b.get("type") == "tool_use" for b in d.get("content", []))
                row["tools"] = "✓(native)" if has_tool else "~(text)"
            except Exception:
                row["tools"] = "✗"

            rows.append(row)

        # Print table
        header = "| Provider | Text | Stream | Tools |\n|----------|------|--------|-------|"
        lines = [header]
        for row in rows:
            lines.append(f"| {row['provider']} | {row['text']} | {row['stream']} | {row['tools']} |")
        table = "\n".join(lines)
        print(f"\n\n## Provider Compatibility Matrix — {datetime.datetime.now():%Y-%m-%d %H:%M}\n\n{table}\n")

        # Save to file
        os.makedirs("tests/results", exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(f"tests/results/compatibility-matrix-{ts}.md", "w") as f:
            f.write(f"# Provider Compatibility Matrix\nGenerated: {datetime.datetime.now()}\n\n{table}\n")
