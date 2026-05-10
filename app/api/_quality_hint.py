"""v3.6.1 — response-quality hint detection.

Defense-in-depth for callers whose upstream content pipeline (scrapers,
file ingestion, etc.) sometimes feeds the LLM thin or junk content
that the model correctly refuses. The model's polite refusal still
costs tokens, and the caller usually doesn't notice — they store the
"response" thinking it's useful KB content.

Endorsed by the coordinator-hub team in their 2026-05-09 reply (after
the operator-forwarded 2-memo round on the Avaya KB cookie-banner
issue): "the X-Quality-Hint header is still useful as a second-layer
detector for sites we haven't pre-emptively guarded."

This module emits a single response header:

    X-Quality-Hint: thin-content; reason=<short>

when the model's response matches a pattern indicating it didn't get
useful content to work with. Callers MAY look at this header to skip
storing the response and re-queue the source for a better-quality
fetch attempt. Absent header == response was a normal one.

We deliberately tune for high specificity (low false positive) over
recall — false positives would cause callers to discard real refusals
that they SHOULD have seen. If the regex doesn't fire, the caller
gets exactly the response they would have pre-v3.6.1.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# Phrases that strongly indicate the model is refusing because the
# input it received is thin / corrupted / banner-only. All matched
# case-insensitively against the FIRST 600 characters of the response
# text (refusals lead with the apology). Avoid matching mid-paragraph
# in long real responses by anchoring on the lead-in.
_THIN_PHRASES = (
    # Cookie / consent / footer scraping garbage
    ("cookie_banner", re.compile(r"\b(only the (cookie|footer)|cookie (consent|banner)|cookie consent section)\b", re.I)),
    ("incomplete", re.compile(r"\b(incomplete or corrupted|appears to be incomplete|appears to be corrupted)\b", re.I)),
    # Generic "the content you sent is empty/garbage" refusal patterns
    ("empty_content", re.compile(r"\bcontent (you|provided) (sent|provided|appears) (?:to be |is )?(empty|incomplete|garbage|truncated)\b", re.I)),
    # Polite-refusal lead-in COMBINED with a length signal — see is_thin()
)

# Lead-ins that suggest the model is starting a refusal. ALONE these
# don't mean thin content (a real article summary can also start
# with "I appreciate"), so we only flag if the FULL response is short
# and the lead-in is present. Tuned conservative.
_REFUSAL_LEAD_INS = re.compile(
    r"^\s*(I appreciate|I need to (flag|inform|point out)|I must inform you|"
    r"Unfortunately|I cannot (summarize|process)|I'm unable to)",
    re.I,
)

# A "real" response is generally >800 chars when the model's actually
# summarizing real content. Below this AND with a refusal lead-in =
# almost certainly a thin-content refusal.
_REFUSAL_SHORT_THRESHOLD_CHARS = 800


def detect_thin_content(response_text: str) -> Optional[str]:
    """If the response text matches a thin-content pattern, return a
    short reason string for the ``X-Quality-Hint`` header. Else None.

    Returns one of:
      ``"cookie_banner"`` — strong match on cookie/footer/banner phrasing
      ``"incomplete"``    — model said the input was incomplete/corrupted
      ``"empty_content"`` — model said the content was empty/garbage
      ``"short_refusal"`` — short response with a refusal lead-in
      ``None``           — looks like a normal response

    Defensive: returns None on any malformed input rather than raising.
    """
    if not isinstance(response_text, str) or not response_text:
        return None
    head = response_text[:600]
    for reason, pat in _THIN_PHRASES:
        if pat.search(head):
            return reason
    # Length-gated refusal — only flag if response is short
    if len(response_text) < _REFUSAL_SHORT_THRESHOLD_CHARS:
        if _REFUSAL_LEAD_INS.search(head):
            return "short_refusal"
    return None


def extract_response_text_anthropic(body: Any) -> str:
    """Extract the assistant text from an Anthropic ``/v1/messages``
    response body. Returns empty string on any unexpected shape.
    """
    try:
        if not isinstance(body, dict):
            return ""
        parts = body.get("content")
        if not isinstance(parts, list):
            return ""
        out: list[str] = []
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    except Exception:
        return ""


def extract_response_text_openai(body: Any) -> str:
    """Extract assistant text from an OpenAI ``/v1/chat/completions``
    response body. Returns empty string on any unexpected shape.
    """
    try:
        if not isinstance(body, dict):
            return ""
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        msg = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(msg, dict):
            return ""
        content = msg.get("content")
        if isinstance(content, str):
            return content
        # Tool calls / null content / list-of-parts → not thin-content
        # detection territory; just return ""
        return ""
    except Exception:
        return ""


def quality_hint_header(reason: Optional[str]) -> dict:
    """Build the header dict to merge into the response. Empty dict
    when the response looks normal (no header emitted).
    """
    if not reason:
        return {}
    return {"X-Quality-Hint": f"thin-content; reason={reason}"}


def merge_into_headers(headers: dict, body: Any, *, endpoint: str) -> dict:
    """Detect thin-content on the response body and add the
    ``X-Quality-Hint`` key to ``headers`` (in place). Returns
    ``headers`` for chaining. Safe no-op on unknown body shapes
    or unknown endpoints — defensive throughout, never raises.
    """
    try:
        if endpoint == "messages":
            text = extract_response_text_anthropic(body)
        elif endpoint in ("completions", "chat"):
            text = extract_response_text_openai(body)
        else:
            return headers
        reason = detect_thin_content(text)
        if reason:
            headers["X-Quality-Hint"] = f"thin-content; reason={reason}"
    except Exception:
        # Defensive — don't let quality-hint detection break the
        # response path. Better to return the response without the
        # hint than to 500 the caller.
        pass
    return headers
