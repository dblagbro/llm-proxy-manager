"""Unit tests for the OAuth passthrough capture endpoint."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.api.oauth_capture import (
    _filter_req_headers, _filter_resp_headers, _safe_text, _HOP_BY_HOP,
)


class TestFilterHeaders:
    def test_strips_host(self):
        out = _filter_req_headers({"host": "example.com", "x-custom": "v"})
        assert "host" not in out
        assert out["x-custom"] == "v"

    def test_strips_content_length(self):
        out = _filter_req_headers({"Content-Length": "42", "accept": "*/*"})
        assert "Content-Length" not in out
        assert out["accept"] == "*/*"

    def test_case_insensitive_filter(self):
        out = _filter_req_headers({"HOST": "a", "Host": "b", "host": "c"})
        assert len(out) == 0

    def test_strips_transfer_encoding(self):
        out = _filter_resp_headers({"Transfer-Encoding": "chunked", "x-good": "v"})
        assert "Transfer-Encoding" not in out
        assert out["x-good"] == "v"

    def test_strips_hop_by_hop_connection(self):
        out = _filter_resp_headers({"Connection": "keep-alive", "date": "now"})
        assert "Connection" not in out
        assert out["date"] == "now"

    def test_preserves_authorization(self):
        """Authorization header must be forwarded to upstream — this is OAuth."""
        out = _filter_req_headers({"authorization": "Bearer abc123", "host": "x"})
        assert out["authorization"] == "Bearer abc123"


class TestSafeText:
    def test_utf8_decoded(self):
        assert _safe_text(b"hello world") == "hello world"

    def test_empty(self):
        assert _safe_text(b"") == ""

    def test_json(self):
        assert _safe_text(b'{"a":1}') == '{"a":1}'

    def test_none(self):
        assert _safe_text(None) is None

    def test_binary_base64_encoded(self):
        """Non-utf8 bytes are represented so the capture log still persists."""
        raw = bytes([0x80, 0x81, 0xff, 0xfe])  # invalid utf-8
        out = _safe_text(raw)
        assert out.startswith("[binary:")
        assert out.endswith("]")


class TestHopByHopSet:
    def test_includes_expected_headers(self):
        expected = {
            "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailer", "transfer-encoding",
            "upgrade", "content-length", "host",
        }
        assert expected <= _HOP_BY_HOP

    def test_all_lowercase(self):
        """Filter matches case-insensitively; the canonical set is lowercase."""
        for h in _HOP_BY_HOP:
            assert h == h.lower()
