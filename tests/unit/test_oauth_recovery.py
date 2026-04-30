"""Unit tests for cluster OAuth refresh-race recovery (v3.0.18)."""
import time
import pytest

from app.cluster.oauth_recovery import (
    PeerOAuthState,
    pull_oauth_state_from_peers,
    adopt_peer_state,
)


class _FakeProvider:
    def __init__(self, **kw):
        self.id = kw.get("id", "p-1")
        self.api_key = kw.get("api_key", "old-token")
        self.oauth_refresh_token = kw.get("oauth_refresh_token", "old-refresh")
        self.oauth_expires_at = kw.get("oauth_expires_at", time.time() - 60)
        self.extra_config = kw.get("extra_config", {})


class _FakeDB:
    async def commit(self):
        return None


class TestAdoptPeerState:
    @pytest.mark.asyncio
    async def test_adopt_writes_fresh_tokens(self):
        p = _FakeProvider()
        state = PeerOAuthState(
            api_key="new-token",
            oauth_refresh_token="new-refresh",
            oauth_expires_at=time.time() + 3600,
            last_user_edit_at=None,
            updated_at_iso=None,
            extra_config={"chatgpt_account_id": "abc-123"},
            source_peer_id="peer-www2",
        )
        await adopt_peer_state(p, _FakeDB(), state)
        assert p.api_key == "new-token"
        assert p.oauth_refresh_token == "new-refresh"
        assert p.oauth_expires_at == state.oauth_expires_at
        assert p.extra_config["chatgpt_account_id"] == "abc-123"

    @pytest.mark.asyncio
    async def test_adopt_preserves_extra_config_keys(self):
        p = _FakeProvider(extra_config={"unrelated_key": "keep-me"})
        state = PeerOAuthState(
            api_key="new-token",
            oauth_refresh_token="new-refresh",
            oauth_expires_at=time.time() + 3600,
            last_user_edit_at=None,
            updated_at_iso=None,
            extra_config={"chatgpt_account_id": "abc-123"},
            source_peer_id="peer-www2",
        )
        await adopt_peer_state(p, _FakeDB(), state)
        assert p.extra_config["unrelated_key"] == "keep-me"
        assert p.extra_config["chatgpt_account_id"] == "abc-123"


class TestPullFromPeers:
    @pytest.mark.asyncio
    async def test_returns_none_when_cluster_disabled(self, monkeypatch):
        from app.config import settings as _s
        monkeypatch.setattr(_s, "cluster_enabled", False)
        result = await pull_oauth_state_from_peers("any")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_peers(self, monkeypatch):
        from app.config import settings as _s
        from app.cluster import oauth_recovery as _r
        monkeypatch.setattr(_s, "cluster_enabled", True)
        monkeypatch.setattr(_r, "_parse_peers", lambda: [])
        assert await pull_oauth_state_from_peers("any") is None

    @pytest.mark.asyncio
    async def test_picks_freshest_expires_at_across_peers(self, monkeypatch):
        from app.config import settings as _s
        from app.cluster import oauth_recovery as _r

        class _MockPeer:
            def __init__(self, pid, url): self.id = pid; self.url = url

        monkeypatch.setattr(_s, "cluster_enabled", True)
        monkeypatch.setattr(_s, "cluster_node_id", "test-node")
        monkeypatch.setattr(_r, "_parse_peers", lambda: [
            _MockPeer("peer-a", "https://a.example"),
            _MockPeer("peer-b", "https://b.example"),
        ])

        now = time.time()
        responses_by_url = {
            "https://a.example/cluster/oauth-pull/p1": (200, {
                "api_key": "tok-a", "oauth_refresh_token": "ref-a",
                "oauth_expires_at": now + 1000, "extra_config": {},
            }),
            "https://b.example/cluster/oauth-pull/p1": (200, {
                "api_key": "tok-b", "oauth_refresh_token": "ref-b",
                "oauth_expires_at": now + 5000, "extra_config": {"chatgpt_account_id": "xyz"},
            }),
        }

        class _MockClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, headers):
                status, body = responses_by_url[url]
                class _R:
                    status_code = status
                    def json(self_): return body
                return _R()

        monkeypatch.setattr(_r.httpx, "AsyncClient", _MockClient)
        result = await pull_oauth_state_from_peers("p1")
        assert result is not None
        assert result.source_peer_id == "peer-b"   # later expires_at
        assert result.api_key == "tok-b"
        assert result.extra_config == {"chatgpt_account_id": "xyz"}

    @pytest.mark.asyncio
    async def test_skips_expired_peer_state(self, monkeypatch):
        from app.config import settings as _s
        from app.cluster import oauth_recovery as _r

        class _MockPeer:
            def __init__(self, pid, url): self.id = pid; self.url = url

        monkeypatch.setattr(_s, "cluster_enabled", True)
        monkeypatch.setattr(_s, "cluster_node_id", "test-node")
        monkeypatch.setattr(_r, "_parse_peers", lambda: [_MockPeer("peer-a", "https://a.example")])

        class _MockClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, headers):
                class _R:
                    status_code = 200
                    def json(self_): return {
                        "api_key": "tok-a",
                        "oauth_refresh_token": "ref-a",
                        "oauth_expires_at": time.time() - 100,  # already expired
                        "extra_config": {},
                    }
                return _R()

        monkeypatch.setattr(_r.httpx, "AsyncClient", _MockClient)
        # Only peer's state is expired → no candidate → None
        assert await pull_oauth_state_from_peers("p1") is None

    @pytest.mark.asyncio
    async def test_skips_404_and_unreachable_peers(self, monkeypatch):
        from app.config import settings as _s
        from app.cluster import oauth_recovery as _r

        class _MockPeer:
            def __init__(self, pid, url): self.id = pid; self.url = url

        monkeypatch.setattr(_s, "cluster_enabled", True)
        monkeypatch.setattr(_s, "cluster_node_id", "test-node")
        monkeypatch.setattr(_r, "_parse_peers", lambda: [
            _MockPeer("peer-404", "https://x.example"),
            _MockPeer("peer-down", "https://y.example"),
        ])

        class _MockClient:
            def __init__(self, *a, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, headers):
                if "x.example" in url:
                    class _R:
                        status_code = 404
                        def json(self_): return {}
                    return _R()
                raise ConnectionError("unreachable")

        monkeypatch.setattr(_r.httpx, "AsyncClient", _MockClient)
        assert await pull_oauth_state_from_peers("p1") is None
