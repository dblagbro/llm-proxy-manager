"""Unit tests for PII masking (Wave 6)."""
import sys
import types
import pytest

_stub = types.ModuleType("litellm")
_stub.RateLimitError = type("RateLimitError", (Exception,), {})
sys.modules.setdefault("litellm", _stub)
if not hasattr(sys.modules["litellm"], "RateLimitError"):
    sys.modules["litellm"].RateLimitError = type("RateLimitError", (Exception,), {})

from app.privacy.pii import mask_text, mask_messages


class TestMaskText:
    def test_empty_string_returns_empty(self):
        r = mask_text("")
        assert r.text == ""
        assert r.count == 0

    def test_none_returns_empty(self):
        r = mask_text(None)
        assert r.text == ""
        assert r.count == 0

    def test_no_pii_untouched(self):
        r = mask_text("this is a perfectly clean sentence")
        assert r.count == 0
        assert r.text == "this is a perfectly clean sentence"

    def test_email_masked(self):
        r = mask_text("contact me at alice@example.com please")
        assert "[EMAIL_REDACTED]" in r.text
        assert "alice@example.com" not in r.text
        assert r.count == 1

    def test_multiple_emails(self):
        r = mask_text("from a@b.com to c@d.org and e@f.net")
        assert r.count == 3
        assert r.text.count("[EMAIL_REDACTED]") == 3

    def test_ssn_masked(self):
        r = mask_text("SSN: 123-45-6789")
        assert "[SSN_REDACTED]" in r.text
        assert "123-45-6789" not in r.text

    def test_ssn_with_spaces(self):
        r = mask_text("SSN 123 45 6789")
        assert "[SSN_REDACTED]" in r.text

    def test_credit_card_masked(self):
        r = mask_text("CC: 4111 1111 1111 1111")
        # CC pattern runs first; could match as CC
        assert "[CC_REDACTED]" in r.text or "[PHONE_REDACTED]" in r.text
        assert "4111 1111 1111 1111" not in r.text

    def test_credit_card_with_dashes(self):
        r = mask_text("card 4111-1111-1111-1111 on file")
        assert "4111-1111-1111-1111" not in r.text

    def test_us_phone_dashes(self):
        r = mask_text("Call 555-123-4567 today")
        assert "[PHONE_REDACTED]" in r.text
        assert "555-123-4567" not in r.text

    def test_us_phone_parens(self):
        r = mask_text("Call (555) 123-4567 today")
        assert "[PHONE_REDACTED]" in r.text
        assert "(555) 123-4567" not in r.text

    def test_us_phone_with_country_code(self):
        r = mask_text("+1-555-123-4567")
        assert "[PHONE_REDACTED]" in r.text

    def test_ipv4_masked(self):
        r = mask_text("connect to 10.0.0.5 on port 443")
        assert "[IP_REDACTED]" in r.text
        assert "10.0.0.5" not in r.text

    def test_ipv4_boundary(self):
        """255-range IPs are valid; 999-range should NOT match."""
        r = mask_text("bad ip: 999.999.999.999")
        assert "[IP_REDACTED]" not in r.text

    def test_mixed_pii(self):
        r = mask_text("email alice@corp.com or call 555-123-4567")
        assert r.count == 2
        assert "[EMAIL_REDACTED]" in r.text
        assert "[PHONE_REDACTED]" in r.text


class TestMaskMessages:
    def test_empty_list_unchanged(self):
        msgs, n = mask_messages([])
        assert msgs == []
        assert n == 0

    def test_string_content_masked(self):
        input_msgs = [{"role": "user", "content": "email me at x@y.com"}]
        msgs, n = mask_messages(input_msgs)
        assert n == 1
        assert "[EMAIL_REDACTED]" in msgs[0]["content"]

    def test_list_content_text_part_masked(self):
        input_msgs = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "my SSN is 123-45-6789"},
                {"type": "image", "source": {"data": "base64..."}},
            ],
        }]
        msgs, n = mask_messages(input_msgs)
        assert n == 1
        assert "[SSN_REDACTED]" in msgs[0]["content"][0]["text"]
        # Image block untouched
        assert msgs[0]["content"][1]["type"] == "image"
        assert msgs[0]["content"][1]["source"]["data"] == "base64..."

    def test_image_url_part_untouched(self):
        """image_url parts should not be mutated even if they contain PII-looking strings."""
        input_msgs = [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "https://example.com/foo.png"}},
            ],
        }]
        msgs, n = mask_messages(input_msgs)
        assert n == 0
        assert msgs[0]["content"][0]["type"] == "image_url"

    def test_per_message_counts_aggregated(self):
        input_msgs = [
            {"role": "user", "content": "a@b.com"},
            {"role": "assistant", "content": "c@d.com and e@f.com"},
        ]
        msgs, n = mask_messages(input_msgs)
        assert n == 3

    def test_preserves_role_field(self):
        input_msgs = [
            {"role": "system", "content": "system msg"},
            {"role": "user", "content": "user msg"},
        ]
        msgs, _ = mask_messages(input_msgs)
        assert msgs[0]["role"] == "system"
        assert msgs[1]["role"] == "user"

    def test_preserves_non_content_fields(self):
        input_msgs = [{"role": "user", "content": "hi", "name": "alice", "tool_call_id": "x"}]
        msgs, _ = mask_messages(input_msgs)
        assert msgs[0]["name"] == "alice"
        assert msgs[0]["tool_call_id"] == "x"
