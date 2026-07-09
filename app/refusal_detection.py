"""v5.20.0 — Response-level refusal detection.

**Problem class.** LLMs sometimes return HTTP 200 with valid content that
does NOT answer what was asked — e.g., asked to write "a Rolling Stones
song," the model says "I can't write that particular song, but here's
something similar" and produces adjacent lyrics. The response is
syntactically valid, structurally normal, and cost-billable. The
caller can't tell from headers or status codes that the model substituted
the task. Operator's 2026-07-05 report on DevinGPT was the trigger.

**Design constraints.**

1. Pure function — no DB, no I/O, no LLM call for grading. The
   response-tail already runs `_extract_text_from_anthropic_response`
   from capability_scout; we reuse that hot path.
2. Per-API-key opt-in. Detection fires on EVERY /v1/messages response
   only if the key opts in via `refusal_detection_enabled`. Default
   OFF so this is invisible to keys that don't want it.
3. Broader patterns than `app/capability_scout/scout.py`. That module
   is scoped to MCP-tool-triggered refusals (file/URL/realtime data).
   This one covers the general task-substitution class: "I can't do
   X, but here's Y."
4. First-class detection for the `REFUSED:` marker. When the key also
   opts into `refusal_prompt_hardening`, the system prompt is
   augmented with an instruction telling the model to reply with
   exactly `REFUSED: <reason>` — machine-detectable, unambiguous.

**What we DON'T do here.** No retry. No cross-provider re-dispatch.
Those live in the caller (v5.20.1 candidate). This module just
detects and returns metadata. The caller decides whether to log,
retry, or ignore.

**Prior art surveyed** (2026-07-05):
- LiteLLM `content_filter_retries` — only on 400-class content-filter
  errors, not soft refusals.
- Portkey guardrails — supports content-based routing but requires
  their gateway.
- Aider — retries on "I can't" self-detection; single-user tool.
- Guardrails.ai — validators require structured output.
- NVIDIA NeMo Guardrails — heavyweight rails DSL.

None of these do per-API-key opt-in for a per-request-tier decision.
That's the gap we're filling.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, List, Optional


@dataclass(frozen=True)
class RefusalMatch:
    """One refusal-pattern hit."""
    pattern_name: str        # stable id, safe for headers + activity_log
    category: str            # broad class: "task_substitution" | "capability_deny" | "explicit_refused"
    matched_snippet: str     # ±40 chars around the hit
    span: tuple[int, int]    # (start, end) in the scanned text


def _r(p: str) -> re.Pattern[str]:
    # MULTILINE so ``^`` matches at start of any line — some models
    # emit a boilerplate ack line before the REFUSED marker.
    return re.compile(p, re.IGNORECASE | re.MULTILINE)


# Order matters: explicit_refused first (unambiguous), then broad
# task_substitution patterns, then capability_deny.
#
# task_substitution = model refused the ask but offered an alternative
#   ("I can't write that specific song, but here's similar…").
#   These are the operator's primary target: HTTP 200 + valid content
#   + wrong task = the silent-refusal-into-substitution class.
#
# capability_deny = model said it can't do the task at all, no
#   substitute offered ("I'm not able to do that"). Also worth
#   surfacing, but less costly to the caller since the "I can't"
#   signal is more findable in-band.
#
# explicit_refused = the `REFUSED:` marker the prompt-hardening
#   injection asks for. Highest confidence.
REFUSAL_PATTERNS: List[tuple[str, str, re.Pattern[str]]] = [
    # ---- explicit_refused: highest confidence, matched first ----
    (
        "explicit_refused_marker",
        "explicit_refused",
        _r(r"^\s*REFUSED\s*:\s*"),
    ),
    # ---- task_substitution: refusal + offered alternative ----
    (
        "cant_but_here_is",
        "task_substitution",
        _r(r"\b(can'?t|cannot|won'?t|unable to|not able to)\s+(?:write|create|generate|produce|reproduce|provide|give you|help with)[^.]{0,60}[,.]\s*(?:but|however|instead)[^.]{0,20}\b(?:here|let me|i can|i'?ll|here'?s)"),
    ),
    (
        "not_that_but_similar",
        "task_substitution",
        _r(r"\b(?:i can'?t|i won'?t|cannot)\s+(?:write|create|generate)\s+(?:that|the|those)\s+(?:specific|particular|exact)[^.]{0,40}\b(?:but|however|instead|here'?s)"),
    ),
    (
        "copyright_style_deflect",
        "task_substitution",
        _r(r"\b(copyrighted|copyright[- ]protected|owned by)[^.]{0,80}\b(?:but|however|instead|let me|here'?s)\s+(?:i can|write|provide|create|give)"),
    ),
    (
        "here_is_original_or_alternative",
        "task_substitution",
        _r(r"\bhere'?s\s+(?:an?|some)\s+(?:original|alternative|different|similar|inspired[- ]by|in[- ]the[- ]style)"),
    ),
    # ---- capability_deny: refusal, no offered alternative ----
    (
        "as_an_ai_language_model",
        "capability_deny",
        _r(r"\bas an ai (?:language )?model\b[^.]{0,60}\b(?:i (?:can'?t|cannot|don'?t have|am not able)|i'?m not able)"),
    ),
    (
        "im_not_able_to",
        "capability_deny",
        _r(r"\bi'?m (?:not able|unable) to (?:write|create|generate|reproduce|provide|share|help)"),
    ),
    (
        "outside_my_capabilities",
        "capability_deny",
        _r(r"\b(?:that'?s|this is)?\s*(?:outside|beyond|not within)\s+(?:my|the ai'?s)\s+capabilities\b"),
    ),
]


def _window(text: str, span: tuple[int, int], radius: int = 40) -> str:
    """Return a short context window around the matched span. Trimmed
    to avoid dumping half the response into a header."""
    start, end = span
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    prefix = "…" if lo > 0 else ""
    suffix = "…" if hi < len(text) else ""
    return f"{prefix}{text[lo:hi].strip()}{suffix}"


def detect_refusal(text: str) -> Optional[RefusalMatch]:
    """Scan ``text`` for refusal patterns. Return the FIRST match
    (patterns ordered by confidence). Empty/non-string input returns
    None.

    First-match-wins (rather than all-matches) because we only need
    ONE signal to decide whether to log/retry. Scanning all patterns
    would let us report richer categories but at 10x the wall-clock;
    the current shape is ~25 µs on a 4KB response.
    """
    if not text or not isinstance(text, str):
        return None
    for name, category, pat in REFUSAL_PATTERNS:
        m = pat.search(text)
        if m:
            return RefusalMatch(
                pattern_name=name,
                category=category,
                matched_snippet=_window(text, m.span()),
                span=m.span(),
            )
    return None


def extract_text_from_anthropic_response(resp: Any) -> str:
    """Pull all text content out of an Anthropic-shape response.

    Copy of the same logic in ``app.capability_scout.scout`` — we
    duplicate rather than import to keep this module dependency-free
    (capability_scout depends on DB access via ``is_enabled``).
    """
    if not isinstance(resp, dict):
        return ""
    content = resp.get("content") or []
    if not isinstance(content, list):
        return ""
    chunks: List[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            t = block.get("text") or ""
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks)


# Public: exposed for the prompt-hardening path in messages.py so
# both sides use the same wording. Keep the marker `REFUSED:` in
# sync with `explicit_refused_marker` above.
REFUSAL_HARDENING_INSTRUCTION = (
    "If you cannot fulfill the user's request exactly as stated, "
    "respond with ONLY \"REFUSED: <one-line reason>\" and nothing else. "
    "Do not offer alternatives, substitute the task, "
    "or provide adjacent content. The proxy will retry with a "
    "different upstream model on your behalf."
)
