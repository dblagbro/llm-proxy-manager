"""v5.22.18 — two gaps found in the second security sweep.

**1. `/api/integration/chat` had no attempt limiting.** `/announce` is public
and advertises that endpoint by path, naming "shared passphrase in request
body". The passphrase is 192 bits (`token_urlsafe(24)`), so guessing is not
the concern — unbounded attempts are. Every accepted call drives an LLM, so an
advertised, unthrottled endpoint is a cost and availability vector. Attempts
are counted under an `integ:` key prefix so a failed integration attempt can
never contribute to an admin-login lockout, or the reverse.

**2. `base_url` was unvalidated.** It is admin-set and the proxy then makes
server-side requests to it. The guard is deliberately narrow: private, LAN and
loopback addresses are legitimate here — the cursor sidecar default is an
internal address and ollama/grok-bridge sit on the docker network — so
blocking RFC1918 would break working configuration. What has no legitimate
use as an LLM endpoint is a cloud instance-metadata service, which hands IAM
credentials to any unauthenticated local GET. Those are the SSRF prize, and
those are what is blocked.
"""

from pathlib import Path

import pytest

from app.api.providers import ProviderCreate, ProviderUpdate, validate_base_url


class TestSsrfMetadataGuard:
    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
            "http://[fd00:ec2::254]/latest/meta-data/",
            "http://metadata.google.internal/computeMetadata/v1/instance/",
            "http://100.100.100.200/latest/meta-data/",
            "http://192.0.0.192/opc/v1/instance/",
            "HTTP://169.254.169.254/",
        ],
    )
    def test_metadata_endpoints_are_refused(self, url):
        with pytest.raises(ValueError):
            validate_base_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.openai.com/v1",
            "https://api.cohere.com",
            # These are real, working configurations in this deployment. A
            # broad "block private IPs" rule would have broken them.
            "http://llm-proxy2-cursor-bridge:3010/v1",
            "http://ollama:11434",
            "http://127.0.0.1:11434",
            "http://192.168.1.11:3000/v1",
        ],
    )
    def test_legitimate_endpoints_still_accepted(self, url):
        assert validate_base_url(url) == url

    def test_empty_and_none_pass_through(self):
        assert validate_base_url(None) is None
        assert validate_base_url("") == ""

    def test_guard_is_wired_into_the_create_model(self):
        with pytest.raises(Exception):
            ProviderCreate(
                name="evil", provider_type="openai",
                base_url="http://169.254.169.254/latest/meta-data/",
            )

    def test_guard_covers_the_update_model_too(self):
        """ProviderUpdate subclasses ProviderCreate; if that ever changes,
        the update path would silently lose the guard."""
        assert issubclass(ProviderUpdate, ProviderCreate)
        with pytest.raises(Exception):
            ProviderUpdate(
                name="evil", provider_type="openai",
                base_url="http://metadata.google.internal/",
            )

    def test_a_benign_provider_still_constructs(self):
        p = ProviderCreate(
            name="ok", provider_type="openai", base_url="https://api.openai.com/v1"
        )
        assert p.base_url == "https://api.openai.com/v1"


class TestIntegrationChatThrottle:
    def _src(self) -> str:
        return Path("app/api/integration.py").read_text()

    def test_endpoint_checks_the_throttle_before_doing_work(self):
        src = self._src()
        body = src.split('@router.post("/api/integration/chat")', 1)[1]
        check = body.index("login_throttle.seconds_remaining")
        work = body.index("handle_chat(")
        assert check < work, "throttle is checked after the LLM call"

    def test_returns_429_with_retry_after(self):
        src = self._src()
        assert "429" in src and "Retry-After" in src

    def test_only_401s_are_counted_as_failures(self):
        """A 500 from the chat handler is not an authentication failure and
        must not push a legitimate caller toward a lockout."""
        src = self._src()
        assert "exc.status_code == 401" in src
        assert "login_throttle.record_failure" in src

    def test_success_clears_the_counter(self):
        assert "login_throttle.record_success" in self._src()

    def test_key_namespace_is_separate_from_admin_login(self):
        """Sharing the namespace would let integration attempts lock the
        operator out of the admin UI, and vice versa."""
        src = self._src()
        assert 'f"integ:{' in src, "integration attempts are not namespaced"

    def test_namespaces_do_not_collide(self):
        from app.auth import login_throttle as lt

        lt.reset()
        for _ in range(lt.MAX_FAILURES):
            lt.record_failure("integ:203.0.113.9")
        assert lt.seconds_remaining("integ:203.0.113.9") > 0
        assert lt.seconds_remaining("203.0.113.9") == 0, (
            "an integration lockout also locked the bare-IP (login) key"
        )
        lt.reset()
