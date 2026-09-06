"""Unit tests for async webhook delivery with HMAC signing."""
import json
import sys
import types

import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.api.webhook import post_webhook


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeClient:
    """Captures request bodies and returns a configurable response."""
    captured: list[dict] = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def post(self, url, content=None, headers=None):
        _FakeClient.captured.append({"url": url, "content": content, "headers": headers})
        return _FakeResp(_FakeClient.next_status)

    next_status = 200


@pytest.fixture(autouse=True)
def reset_fake_client():
    _FakeClient.captured = []
    _FakeClient.next_status = 200
    yield


def _require_signing_secret(monkeypatch):
    """v5.22.16 — webhooks are HMAC-signed with the cluster secret, and since
    v5.22.15 signing fails closed when that secret is unset rather than
    silently signing with an empty key. These tests exercise the signed path,
    so they must configure one."""
    from app.cluster import auth as _auth

    monkeypatch.setattr(_auth.settings, "cluster_sync_secret", "w" * 32)


class TestPostWebhook:
    @pytest.mark.asyncio
    async def test_sends_signed_post(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        await post_webhook("https://example.com/hook", {"event": "done", "cost": 0.01})

        assert len(_FakeClient.captured) == 1
        sent = _FakeClient.captured[0]
        assert sent["url"] == "https://example.com/hook"
        assert sent["headers"]["Content-Type"] == "application/json"
        assert "X-LLM-Proxy-Sig" in sent["headers"]
        assert sent["headers"]["X-LLM-Proxy-Sig"]  # non-empty

    @pytest.mark.asyncio
    async def test_body_is_sorted_json(self, monkeypatch):
        """Body must use sort_keys=True so signatures are deterministic."""
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        await post_webhook("https://example.com/hook", {"b": 2, "a": 1, "c": 3})

        body = _FakeClient.captured[0]["content"]
        parsed = json.loads(body.decode())
        assert parsed == {"a": 1, "b": 2, "c": 3}
        # Also check raw byte order matches sort_keys=True
        assert body.index(b'"a"') < body.index(b'"b"') < body.index(b'"c"')

    @pytest.mark.asyncio
    async def test_signature_deterministic_for_same_payload(self, monkeypatch):
        """Same payload → same signature (barring key rotation)."""
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        payload = {"event": "done"}
        await post_webhook("https://a/hook", payload)
        await post_webhook("https://b/hook", payload)

        sig1 = _FakeClient.captured[0]["headers"]["X-LLM-Proxy-Sig"]
        sig2 = _FakeClient.captured[1]["headers"]["X-LLM-Proxy-Sig"]
        assert sig1 == sig2

    @pytest.mark.asyncio
    async def test_different_payloads_different_sigs(self, monkeypatch):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        await post_webhook("https://example.com/hook", {"event": "a"})
        await post_webhook("https://example.com/hook", {"event": "b"})

        sig1 = _FakeClient.captured[0]["headers"]["X-LLM-Proxy-Sig"]
        sig2 = _FakeClient.captured[1]["headers"]["X-LLM-Proxy-Sig"]
        assert sig1 != sig2

    @pytest.mark.asyncio
    async def test_network_failure_swallowed(self, monkeypatch):
        """A raising httpx client must not propagate the exception."""
        class _RaisingClient:
            def __init__(self, *args, **kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def post(self, *args, **kwargs):
                raise RuntimeError("connection refused")

        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _RaisingClient)

        # Should complete without raising
        await post_webhook("https://unreachable/", {"event": "done"})

    @pytest.mark.asyncio
    async def test_4xx_response_logs_but_does_not_raise(self, monkeypatch):
        import httpx
        _FakeClient.next_status = 404
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        # Should complete without raising
        await post_webhook("https://example.com/hook", {"event": "done"})
        assert len(_FakeClient.captured) == 1

    @pytest.mark.asyncio
    async def test_5xx_response_logs_but_does_not_raise(self, monkeypatch):
        import httpx
        _FakeClient.next_status = 503
        monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
        _require_signing_secret(monkeypatch)

        await post_webhook("https://example.com/hook", {"event": "done"})
        assert len(_FakeClient.captured) == 1
