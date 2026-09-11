"""v5.22.18 — webhooks must not be signed with the cluster secret.

``post_webhook`` used ``app.cluster.auth.sign_payload``, so caller-facing
webhooks were signed with ``CLUSTER_SYNC_SECRET`` — the key that
authenticates ``POST /cluster/sync``, where ``apply_sync`` can write
providers, api_keys, users and settings. The cluster's authentication key was
being exercised, in signature form, against every external receiver: two
trust domains sharing one secret, with the weaker one facing outward.

A dedicated key now takes precedence. The cluster secret remains a fallback
so upgrading does not silently stop signing, but using it is warned about
because it is the state to get out of.
"""

import hashlib
import hmac
from pathlib import Path

import pytest

from app.api import webhook as wh

BODY = b'{"cost":0.01,"event":"done"}'
WEBHOOK_SECRET = "w" * 40
CLUSTER_SECRET = "c" * 40


@pytest.fixture(autouse=True)
def _reset_warn_latch():
    wh._fallback_warned = False
    yield
    wh._fallback_warned = False


class TestKeySeparation:
    def test_dedicated_key_is_preferred_over_the_cluster_secret(self, monkeypatch):
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", WEBHOOK_SECRET)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", CLUSTER_SECRET)

        key, source = wh._signing_key()
        assert source == "webhook"
        assert key == WEBHOOK_SECRET.encode()

    def test_signature_differs_from_the_cluster_signature(self, monkeypatch):
        """The whole point: a webhook signature must not be derivable from,
        or reveal anything about, the cluster key."""
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", WEBHOOK_SECRET)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", CLUSTER_SECRET)

        got = wh.sign_webhook(BODY)
        cluster_sig = hmac.new(CLUSTER_SECRET.encode(), BODY, hashlib.sha256).hexdigest()
        assert got != cluster_sig
        assert got == hmac.new(WEBHOOK_SECRET.encode(), BODY, hashlib.sha256).hexdigest()

    def test_module_no_longer_imports_cluster_signing(self):
        src = Path("app/api/webhook.py").read_text()
        assert "from app.cluster.auth import" not in src, (
            "webhook.py is signing with cluster credentials again"
        )


class TestFallbackBehaviour:
    def test_falls_back_to_cluster_secret_so_upgrades_keep_signing(self, monkeypatch):
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", None)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", CLUSTER_SECRET)

        key, source = wh._signing_key()
        assert source == "cluster-fallback"
        assert key == CLUSTER_SECRET.encode()

    def test_fallback_is_warned_about_once(self, monkeypatch, caplog):
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", None)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", CLUSTER_SECRET)

        with caplog.at_level("WARNING"):
            wh._signing_key()
            wh._signing_key()
        hits = [r for r in caplog.records if "signing_key_fallback" in r.getMessage()]
        assert len(hits) == 1, "fallback warning should not spam every delivery"


class TestFailsClosed:
    def test_no_key_means_no_signature(self, monkeypatch):
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", None)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", None)
        assert wh.sign_webhook(BODY) is None

    @pytest.mark.asyncio
    async def test_no_key_means_no_delivery(self, monkeypatch):
        """An unsigned webhook tells the receiver nothing about who sent it,
        so sending one is worse than not sending."""
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", None)
        monkeypatch.setattr(wh.settings, "cluster_sync_secret", None)

        called = []

        class _Boom:
            def __init__(self, *a, **k):
                called.append(True)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        monkeypatch.setattr(wh.httpx, "AsyncClient", _Boom)
        await wh.post_webhook("https://example.com/hook", {"event": "done"})
        assert called == [], "sent a webhook with no signature"

    @pytest.mark.asyncio
    async def test_delivery_failure_never_raises_into_the_caller(self, monkeypatch):
        """post_webhook runs after a completion; it must not be able to fail
        the request that triggered it."""
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", WEBHOOK_SECRET)

        class _Broken:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                raise RuntimeError("connection reset")

        monkeypatch.setattr(wh.httpx, "AsyncClient", _Broken)
        await wh.post_webhook("https://example.com/hook", {"event": "done"})


class TestSignatureShape:
    def test_signs_the_exact_bytes_sent(self, monkeypatch):
        """Signing a different serialisation than the one transmitted is the
        bug class that produced the cluster-sync 403s."""
        monkeypatch.setattr(wh.settings, "webhook_signing_secret", WEBHOOK_SECRET)
        src = Path("app/api/webhook.py").read_text()
        assert "sig = sign_webhook(body)" in src
        assert "content=body" in src
