"""v3.6.1 — quality-hint detector + 412 ETag-header fix tests."""
from __future__ import annotations

from app.api._quality_hint import (
    detect_thin_content,
    extract_response_text_anthropic,
    extract_response_text_openai,
    quality_hint_header,
    merge_into_headers,
)


# ── detect_thin_content ────────────────────────────────────────────


def test_detect_cookie_banner():
    text = "I appreciate the request, but the content provided appears to be only the cookie consent section of the page."
    assert detect_thin_content(text) == "cookie_banner"


def test_detect_only_the_footer():
    text = "I must inform you that the document content provided appears to be only the footer/cookie consent section."
    assert detect_thin_content(text) == "cookie_banner"


def test_detect_incomplete():
    text = "The content provided appears to be incomplete or corrupted; I cannot summarize it."
    assert detect_thin_content(text) == "incomplete"


def test_detect_short_refusal():
    text = "I appreciate your detailed instructions, but I cannot proceed with this request."
    assert detect_thin_content(text) == "short_refusal"


def test_no_match_on_normal_response():
    text = "The article describes Avaya Analytics backup procedures including step-by-step migration instructions, prerequisites, and configuration parameters. " * 10
    assert detect_thin_content(text) is None


def test_no_match_on_long_response_even_with_apology_lead():
    """A 1000+ char response that starts with 'I appreciate' but is
    actually a real article summary should NOT trigger short_refusal."""
    text = "I appreciate the question. " + ("This is real article content. " * 50)
    assert detect_thin_content(text) is None


def test_empty_input():
    assert detect_thin_content("") is None
    assert detect_thin_content(None) is None  # type: ignore[arg-type]


def test_non_string_input():
    assert detect_thin_content(123) is None  # type: ignore[arg-type]
    assert detect_thin_content({}) is None  # type: ignore[arg-type]


# ── extract_response_text_* ────────────────────────────────────────


def test_extract_anthropic_text_block():
    body = {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "text", "text": "hello world"}],
        "model": "claude-haiku",
    }
    assert extract_response_text_anthropic(body) == "hello world"


def test_extract_anthropic_multiple_blocks():
    body = {
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
        ]
    }
    assert extract_response_text_anthropic(body) == "first\nsecond"


def test_extract_anthropic_skips_non_text():
    body = {
        "content": [
            {"type": "tool_use", "name": "x"},
            {"type": "text", "text": "real"},
        ]
    }
    assert extract_response_text_anthropic(body) == "real"


def test_extract_anthropic_unexpected_shapes():
    assert extract_response_text_anthropic(None) == ""
    assert extract_response_text_anthropic({}) == ""
    assert extract_response_text_anthropic({"content": "string-not-list"}) == ""
    assert extract_response_text_anthropic([]) == ""


def test_extract_openai_message_content():
    body = {
        "id": "x", "object": "chat.completion",
        "choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
    }
    assert extract_response_text_openai(body) == "hi"


def test_extract_openai_handles_no_choices():
    assert extract_response_text_openai({"choices": []}) == ""
    assert extract_response_text_openai({}) == ""


def test_extract_openai_handles_null_content():
    """Tool-call-only response has content=null. Should not raise."""
    body = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [{}]}}]}
    assert extract_response_text_openai(body) == ""


# ── quality_hint_header ────────────────────────────────────────────


def test_header_built_for_reason():
    h = quality_hint_header("cookie_banner")
    assert h == {"X-Quality-Hint": "thin-content; reason=cookie_banner"}


def test_header_empty_for_none():
    assert quality_hint_header(None) == {}
    assert quality_hint_header("") == {}


# ── merge_into_headers ─────────────────────────────────────────────


def test_merge_anthropic_thin_content():
    headers: dict = {"X-Provider": "test"}
    body = {
        "content": [{"type": "text", "text": "I appreciate the request, but the content provided appears to be only the cookie consent section."}]
    }
    merge_into_headers(headers, body, endpoint="messages")
    assert headers["X-Quality-Hint"] == "thin-content; reason=cookie_banner"
    assert headers["X-Provider"] == "test"  # didn't disturb existing


def test_merge_no_hint_for_normal_response():
    headers: dict = {"X-Provider": "test"}
    body = {"content": [{"type": "text", "text": "The article explains the backup procedure step by step. " * 30}]}
    merge_into_headers(headers, body, endpoint="messages")
    assert "X-Quality-Hint" not in headers


def test_merge_openai_thin_content():
    headers: dict = {}
    body = {"choices": [{"message": {"role": "assistant", "content": "I appreciate the request, but the content appears incomplete or corrupted."}}]}
    merge_into_headers(headers, body, endpoint="completions")
    assert headers.get("X-Quality-Hint") == "thin-content; reason=incomplete"


def test_merge_safe_on_unknown_endpoint():
    """Defensive: unknown endpoint string should be a no-op, not a crash."""
    headers: dict = {}
    merge_into_headers(headers, {}, endpoint="bogus")
    assert headers == {}


def test_merge_safe_on_malformed_body():
    """Defensive: malformed body shouldn't break the response."""
    headers: dict = {"X-Provider": "test"}
    merge_into_headers(headers, "not-a-dict", endpoint="messages")
    merge_into_headers(headers, None, endpoint="messages")
    merge_into_headers(headers, [], endpoint="completions")
    assert "X-Quality-Hint" not in headers
    assert headers["X-Provider"] == "test"


# ── 412 ETag-header fix (v3.6.1 BUG-016) ───────────────────────────


def test_412_returns_jsonresponse_with_etag():
    """v3.6.1 fix: PUT /api/llm/models with mismatched If-Match must
    return 412 with the fresh ETag in the response headers (not just
    the body). Pre-fix raise HTTPException(412, ...) stripped the
    headers entirely."""
    import inspect
    from app.api import llm_models
    src = inspect.getsource(llm_models.update_model_identity)
    # The fix returns JSONResponse(status_code=412, ...) instead of raising
    assert "JSONResponse" in src and "status_code=412" in src
    assert 'headers={"ETag": expected}' in src
    # Old buggy pattern should be gone
    assert "raise HTTPException(\n            412," not in src
