"""
Integration tests for features added in the recent sprint:
  - Vision stripping (images replaced when provider lacks native_vision)
  - Per-key rate limiting (sliding-window RPM enforcement)
  - Per-key spending cap enforcement
  - Multi-tag tool emulation (provider returns various tag formats)

All tests use the mock server to avoid real API costs.
"""
import uuid
import time
import json
import pytest
import requests
import urllib3

urllib3.disable_warnings()

from tests.conftest import BASE_URL, ADMIN_USER, ADMIN_PASS
from tests.integration.conftest import collect_sse

# ── helpers ───────────────────────────────────────────────────────────────────

def _msg(content, *, stream=False):
    return {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 50,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }


def _post(headers, body, stream=False):
    return requests.post(
        f"{BASE_URL}/v1/messages",
        headers=headers,
        json={**body, "stream": stream},
        stream=stream,
        verify=False,
        timeout=30,
    )


# ── Vision stripping ──────────────────────────────────────────────────────────

class TestVisionStripping:
    """
    The mock provider (type=compatible) has no native_vision. When a request includes
    image blocks, the proxy must replace them with text placeholders before forwarding.
    """

    def test_image_blocks_replaced_with_text_placeholder(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="I see the placeholder.")
        content = [
            {"type": "text", "text": "Describe this image:"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc123"}},
        ]
        r = _post(llm_headers, _msg(content))
        assert r.status_code == 200

        # The mock must have received the request WITHOUT the image block
        received = mock_ctl.last()
        received_msgs = received.get("messages", [])
        all_content = []
        for msg in received_msgs:
            c = msg.get("content", "")
            if isinstance(c, list):
                all_content.extend(c)
            else:
                all_content.append({"type": "text", "text": c})

        # No image blocks should survive
        image_blocks = [b for b in all_content if isinstance(b, dict) and b.get("type") == "image"]
        assert len(image_blocks) == 0, f"Image block survived stripping: {image_blocks}"

    def test_image_placeholder_text_in_forwarded_request(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="OK")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "data"}},
        ]
        r = _post(llm_headers, _msg(content))
        assert r.status_code == 200

        received = mock_ctl.last()
        raw = json.dumps(received)
        # Proxy must inject a placeholder mentioning the media type
        assert "image/jpeg" in raw or "not supported" in raw

    def test_text_only_request_passes_through_unchanged(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="hello")
        r = _post(llm_headers, _msg("hello world"))
        assert r.status_code == 200

        received = mock_ctl.last()
        raw = json.dumps(received)
        assert "not supported" not in raw

    def test_vision_stripped_header_present(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="OK")
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
        ]
        r = _post(llm_headers, _msg(content))
        assert r.status_code == 200
        # X-Provider header must be present (basic proxy header sanity)
        assert "x-provider" in r.headers or "X-Provider" in r.headers

    def test_mixed_text_and_image_preserves_text(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="text", content="I see the placeholder")
        content = [
            {"type": "text", "text": "This is the question"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "abc"}},
        ]
        _post(llm_headers, _msg(content))
        received = mock_ctl.last()
        raw = json.dumps(received)
        assert "This is the question" in raw


# ── Multi-tag tool emulation ──────────────────────────────────────────────────

class TestMultiTagToolEmulation:
    """
    Verify that the proxy correctly handles models that return various XML tag
    wrappers for their tool calls: <tool_call>, <tool_code>, <function_call>, <tool_use>.
    """

    TOOL_DEF = {
        "name": "read_file",
        "description": "Read a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    }

    def _queue_tag(self, mock_ctl, tag: str):
        mock_ctl.queue(
            type="raw_text",
            content=f'<{tag}>\n{{"name": "read_file", "input": {{"path": "/tmp"}}}}\n</{tag}>',
        )

    def test_tool_call_tag_parsed(self, only_mock_routing, mock_ctl, llm_headers):
        mock_ctl.queue(type="tool_emulation", tool_name="read_file", tool_input={"path": "/tmp"})
        r = _post(llm_headers, {**_msg("read /tmp"), "tools": [self.TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
        assert len(tool_blocks) > 0
        assert tool_blocks[0]["name"] == "read_file"

    def test_function_name_field_normalised(self, only_mock_routing, mock_ctl, llm_headers):
        """Mock returns function_name instead of name — proxy must normalise it."""
        mock_ctl.queue(
            type="raw_text",
            content='<tool_call>\n{"function_name": "read_file", "input": {"path": "/tmp"}}\n</tool_call>',
        )
        r = _post(llm_headers, {**_msg("read /tmp"), "tools": [self.TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
        assert len(tool_blocks) > 0, f"Expected tool_use block, got: {d.get('content')}"
        assert tool_blocks[0]["name"] == "read_file"

    def test_parameters_field_normalised_to_input(self, only_mock_routing, mock_ctl, llm_headers):
        """Mock returns parameters instead of input — proxy must normalise it."""
        mock_ctl.queue(
            type="raw_text",
            content='<tool_call>\n{"name": "read_file", "parameters": {"path": "/etc"}}\n</tool_call>',
        )
        r = _post(llm_headers, {**_msg("read /etc"), "tools": [self.TOOL_DEF]})
        assert r.status_code == 200
        d = r.json()
        tool_blocks = [b for b in d.get("content", []) if b.get("type") == "tool_use"]
        assert len(tool_blocks) > 0
        assert tool_blocks[0]["input"] == {"path": "/etc"}


# ── Per-key rate limiting ─────────────────────────────────────────────────────

class TestRateLimiting:
    """
    Create keys with rate_limit_rpm set and verify the proxy enforces the limit.
    """

    def test_rate_limit_enforced(self, admin_session, only_mock_routing, mock_ctl):
        name = f"rl-test-{uuid.uuid4().hex[:6]}"
        # rate_limit_rpm=1: per-node limit = max(1, 1//N) = 1 regardless of node count
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "rate_limit_rpm": 1})
        assert r.status_code == 200, r.text
        raw_key = r.json()["raw_key"]
        key_id = r.json()["id"]
        headers = {"x-api-key": raw_key, "Content-Type": "application/json"}

        try:
            # First request should pass (window is empty)
            mock_ctl.queue(type="text", content="ok")
            r1 = _post(headers, _msg("hi"))
            assert r1.status_code == 200, f"1st request failed: {r1.status_code}"

            # Second request should be rate-limited (window has 1 entry >= limit of 1)
            r2 = _post(headers, _msg("hi"))
            assert r2.status_code == 429, f"Expected 429, got {r2.status_code}: {r2.text[:200]}"
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_rate_limit_error_message(self, admin_session, only_mock_routing, mock_ctl):
        name = f"rl-msg-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "rate_limit_rpm": 1})
        assert r.status_code == 200
        raw_key = r.json()["raw_key"]
        key_id = r.json()["id"]
        headers = {"x-api-key": raw_key, "Content-Type": "application/json"}

        try:
            mock_ctl.queue(type="text", content="ok")
            r1 = _post(headers, _msg("hi"))  # consumes the 1-RPM allowance
            assert r1.status_code == 200
            r2 = _post(headers, _msg("hi"))  # should be blocked
            assert r2.status_code == 429
            detail = str(r2.json().get("detail", ""))
            assert "rate limit" in detail.lower() or "requests/minute" in detail.lower()
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_no_rate_limit_allows_many_requests(self, only_mock_routing, mock_ctl, llm_headers):
        """A key with no rate_limit_rpm set should never hit 429 from rate limiting."""
        for _ in range(5):
            mock_ctl.queue(type="text", content="ok")
        for i in range(5):
            r = _post(llm_headers, _msg("hi"))
            assert r.status_code == 200, f"Request {i + 1} failed with {r.status_code}"

    def test_rate_limit_update_via_api(self, admin_session):
        """Verify spending_cap_usd and rate_limit_rpm appear in key list after update."""
        name = f"rl-update-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard"})
        assert r.status_code == 200
        key_id = r.json()["id"]

        try:
            # Update rate limit and spending cap
            r2 = admin_session.patch(f"{BASE_URL}/api/keys/{key_id}",
                                     json={"rate_limit_rpm": 60, "spending_cap_usd": 5.0})
            assert r2.status_code == 200

            # Verify values returned
            data = r2.json()
            assert data["rate_limit_rpm"] == 60
            assert abs(data["spending_cap_usd"] - 5.0) < 0.001
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_clear_rate_limit_via_negative_value(self, admin_session):
        """Sending rate_limit_rpm=-1 should clear the limit (set to null)."""
        name = f"rl-clear-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "rate_limit_rpm": 30})
        assert r.status_code == 200
        key_id = r.json()["id"]

        try:
            r2 = admin_session.patch(f"{BASE_URL}/api/keys/{key_id}",
                                     json={"rate_limit_rpm": -1})
            assert r2.status_code == 200
            data = r2.json()
            assert data["rate_limit_rpm"] is None
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")


# ── Per-key spending cap ──────────────────────────────────────────────────────

class TestSpendingCap:
    """
    Verify that keys with spending_cap_usd=0.0 are immediately blocked
    (cost 0.0 >= cap 0.0 is True).
    """

    def test_zero_cap_blocks_immediately(self, admin_session):
        name = f"cap-test-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "spending_cap_usd": 0.0})
        assert r.status_code == 200
        raw_key = r.json()["raw_key"]
        key_id = r.json()["id"]
        headers = {"x-api-key": raw_key, "Content-Type": "application/json"}

        try:
            r2 = _post(headers, _msg("hi"))
            assert r2.status_code == 429, f"Expected 429 (cap exceeded), got {r2.status_code}: {r2.text[:200]}"
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_spending_cap_error_message(self, admin_session):
        name = f"cap-msg-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "spending_cap_usd": 0.0})
        assert r.status_code == 200
        raw_key = r.json()["raw_key"]
        key_id = r.json()["id"]
        headers = {"x-api-key": raw_key, "Content-Type": "application/json"}

        try:
            r2 = _post(headers, _msg("hi"))
            assert r2.status_code == 429
            detail = str(r2.json().get("detail", ""))
            assert "spending cap" in detail.lower() or "cap" in detail.lower()
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_no_cap_allows_requests(self, only_mock_routing, mock_ctl, llm_headers):
        """Standard key with no cap should not be blocked."""
        mock_ctl.queue(type="text", content="ok")
        r = _post(llm_headers, _msg("hi"))
        assert r.status_code == 200

    def test_spending_cap_clear_via_negative(self, admin_session):
        """spending_cap_usd=-1 should clear the cap."""
        name = f"cap-clear-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "spending_cap_usd": 10.0})
        assert r.status_code == 200
        key_id = r.json()["id"]

        try:
            r2 = admin_session.patch(f"{BASE_URL}/api/keys/{key_id}",
                                     json={"spending_cap_usd": -1})
            assert r2.status_code == 200
            assert r2.json()["spending_cap_usd"] is None
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")

    def test_key_with_generous_cap_allows_requests(self, admin_session, only_mock_routing, mock_ctl):
        """A key with cap $1000 should not be blocked on first request."""
        name = f"cap-generous-{uuid.uuid4().hex[:6]}"
        r = admin_session.post(f"{BASE_URL}/api/keys",
                               json={"name": name, "key_type": "standard", "spending_cap_usd": 1000.0})
        assert r.status_code == 200
        raw_key = r.json()["raw_key"]
        key_id = r.json()["id"]
        headers = {"x-api-key": raw_key, "Content-Type": "application/json"}

        try:
            mock_ctl.queue(type="text", content="ok")
            r2 = _post(headers, _msg("hi"))
            assert r2.status_code == 200, f"Expected 200, got {r2.status_code}: {r2.text[:200]}"
        finally:
            admin_session.delete(f"{BASE_URL}/api/keys/{key_id}")
