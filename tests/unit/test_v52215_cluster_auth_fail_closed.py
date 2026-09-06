"""v5.22.15 — an unset cluster secret must disable auth, not weaken it.

The bug
-------
``app/cluster/auth.py`` used to sign and verify with::

    key = (settings.cluster_sync_secret or "").encode()

``cluster_sync_secret`` defaults to ``None``. So a node with the variable
unset did not turn authentication off — it moved the whole cluster onto a key
every attacker already knows: ``b""``. Anyone who could reach
``POST /cluster/sync`` could sign their own payload with the empty key and
have ``apply_sync`` write providers, api_keys, users and settings into the
node. The nginx private-range allow-list was the only control left, and a
network ACL is not authentication.

``test_empty_key_forgery_is_rejected`` below is that exact attack. It failed
against the old implementation and passes against the new one — which is the
only reason to trust the fix.
"""

import hashlib
import hmac

import pytest

from app.cluster import auth as cluster_auth
from app.cluster.auth import (
    MIN_RECOMMENDED_SECRET_LEN,
    ClusterAuthNotConfigured,
    audit_cluster_auth_config,
    cluster_auth_configured,
    sign_payload,
    verify_cluster_request,
    verify_payload,
)

BODY = b'{"node_id":"attacker","providers":[]}'


@pytest.fixture
def no_secret(monkeypatch):
    monkeypatch.setattr(cluster_auth.settings, "cluster_sync_secret", None)


@pytest.fixture
def good_secret(monkeypatch):
    monkeypatch.setattr(
        cluster_auth.settings, "cluster_sync_secret", "s" * MIN_RECOMMENDED_SECRET_LEN
    )


class TestUnsetSecretFailsClosed:
    def test_empty_key_forgery_is_rejected(self, no_secret):
        """THE regression test. Forge a signature the way an attacker would —
        HMAC over the body with the empty key — and present it."""
        forged = hmac.new(b"", BODY, hashlib.sha256).hexdigest()
        assert verify_payload(BODY, forged) is False
        assert verify_cluster_request(BODY, forged) is False

    def test_no_signature_at_all_is_rejected(self, no_secret):
        assert verify_payload(BODY, "") is False

    def test_arbitrary_signature_is_rejected(self, no_secret):
        assert verify_payload(BODY, "0" * 64) is False

    def test_signing_refuses_rather_than_emitting_a_weak_token(self, no_secret):
        """Signing with an empty key would hand a mis-configured peer a
        signature it would accept. Refuse instead."""
        with pytest.raises(ClusterAuthNotConfigured):
            sign_payload(BODY)

    def test_configured_helper_reports_false(self, no_secret):
        assert cluster_auth_configured() is False


class TestConfiguredSecretStillWorks:
    """Fail-closed must not mean fail-always — the live cluster depends on
    this path."""

    def test_round_trip(self, good_secret):
        assert verify_payload(BODY, sign_payload(BODY)) is True
        assert verify_cluster_request(BODY, sign_payload(BODY)) is True

    def test_wrong_signature_rejected(self, good_secret):
        assert verify_payload(BODY, sign_payload(b"different")) is False

    def test_tampered_body_rejected(self, good_secret):
        sig = sign_payload(BODY)
        assert verify_payload(BODY + b" ", sig) is False

    def test_empty_key_forgery_rejected_when_configured(self, good_secret):
        forged = hmac.new(b"", BODY, hashlib.sha256).hexdigest()
        assert verify_payload(BODY, forged) is False

    def test_configured_helper_reports_true(self, good_secret):
        assert cluster_auth_configured() is True


class TestStartupAudit:
    def test_silent_when_cluster_disabled(self, monkeypatch, no_secret):
        monkeypatch.setattr(cluster_auth.settings, "cluster_enabled", False)
        assert audit_cluster_auth_config() == []

    def test_reports_missing_secret(self, monkeypatch, no_secret):
        monkeypatch.setattr(cluster_auth.settings, "cluster_enabled", True)
        problems = audit_cluster_auth_config()
        assert len(problems) == 1
        assert "CLUSTER_SYNC_SECRET is unset" in problems[0]

    def test_reports_short_secret(self, monkeypatch):
        monkeypatch.setattr(cluster_auth.settings, "cluster_enabled", True)
        monkeypatch.setattr(cluster_auth.settings, "cluster_sync_secret", "short")
        problems = audit_cluster_auth_config()
        assert len(problems) == 1
        assert "only 5 characters" in problems[0]

    def test_quiet_on_a_strong_secret(self, monkeypatch, good_secret):
        monkeypatch.setattr(cluster_auth.settings, "cluster_enabled", True)
        assert audit_cluster_auth_config() == []

    def test_audit_never_echoes_the_secret(self, monkeypatch):
        monkeypatch.setattr(cluster_auth.settings, "cluster_enabled", True)
        monkeypatch.setattr(cluster_auth.settings, "cluster_sync_secret", "hunter2short")
        for problem in audit_cluster_auth_config():
            assert "hunter2short" not in problem
