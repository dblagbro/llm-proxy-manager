"""v5.8.0 — end-to-end integration-protocol test.

Hits the live smoke instance:

  1. GET /announce — describes the proxy.
  2. POST /api/integration/chat — the integrating AI describes its
     project; the management AI mints a key via tool-call.
  3. POST /v1/messages with the new key — proves the minted key works.

Set ``LLMPROXY_BASE`` + ``INTEGRATION_PASSPHRASE`` env vars to run
against a non-default target.
"""
from __future__ import annotations

import os
import ssl

import pytest
import urllib.request
import json as _json


BASE = os.environ.get("LLMPROXY_BASE", "https://www.voipguru.org/llm-proxy2-smoke")
PASSPHRASE = os.environ.get("INTEGRATION_PASSPHRASE", "")


def _http(method: str, path: str, body: dict | None = None, headers: dict | None = None):
    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=_json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=180) as r:
            return r.status, _json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _json.loads(e.read())


def test_announce_is_public_and_describes_proxy():
    status, body = _http("GET", "/announce")
    assert status == 200
    assert body["name"] == "llm-proxy v2"
    # Endpoint catalog present
    assert "/v1/messages" in body["endpoints"]["messages"]["path"]
    # Integration mechanism described
    assert body["integration"]["endpoint"] == "/api/integration/chat"
    assert "passphrase" in body["integration"]["request_shape"]


def test_integration_chat_rejects_bad_passphrase():
    status, _ = _http(
        "POST",
        "/api/integration/chat",
        {
            "passphrase": "wrong-secret",
            "project_name": "test",
            "message": "hi",
        },
    )
    assert status == 401


@pytest.mark.skipif(not PASSPHRASE, reason="INTEGRATION_PASSPHRASE not set")
def test_full_integration_flow_mints_a_key_and_the_key_works():
    """End-to-end: integrating AI front-loads everything; management
    AI mints a key on turn 1; we use the key to /v1/messages."""
    status, body = _http(
        "POST",
        "/api/integration/chat",
        {
            "passphrase": PASSPHRASE,
            "project_name": "v580-e2e-test",
            "message": (
                "I'm building an automated test client to verify the "
                "v5.8.0 integration protocol. AI use case: single "
                "/v1/messages calls per test run, claude-haiku class "
                "model. No tools needed; mcp_tools_allow=[] so the "
                "proxy doesn't inject anything. Daily budget $2 "
                "(this is automated testing only). Please provision."
            ),
        },
    )
    assert status == 200, f"chat returned {status}: {body}"
    assert body.get("provisioned") is not None, (
        f"Expected key minted on turn 1; got response={body.get('response')!r}"
    )
    new_key = body["provisioned"]["api_key"]
    assert new_key.startswith("llmp-") or new_key.startswith("sk-")

    # Use the new key
    status, chat = _http(
        "POST",
        "/v1/messages",
        {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 30,
            "messages": [{"role": "user", "content": "say hi in 3 words"}],
        },
        headers={"x-api-key": new_key, "anthropic-version": "2023-06-01"},
    )
    # Either 200 (real upstream available) or a graceful 502/503 if no
    # haiku-class provider is configured for the test environment; the
    # key auth itself should succeed (no 401).
    assert status != 401, f"key didn't authenticate: {chat}"
