"""Cascade routing (Wave 3 #14) — FrugalGPT-style cheap → grade → escalate.

Opt-in via LMRH `cascade=auto` dim or the X-Cot-Cascade request header.
Flow:

    1. Pick the cheapest capable provider (cheapest among candidates that
       satisfy hard LMRH constraints).
    2. Run the draft against it.
    3. Run a grader pass on a DIFFERENT cheap provider with a strict
       binary rubric: is this answer acceptable to return to the user?
    4. If grader says YES → return the cheap answer.
       If grader says NO  → escalate to the top-ranked candidate
       (usually the premium model) and return that answer.

Published FrugalGPT result: up to ~90% cost reduction at matched quality
when the task distribution is dominated by easy-to-answer prompts.
"""
import json
import logging
from dataclasses import dataclass
from typing import Optional

from app.routing.retry import acompletion_with_retry

logger = logging.getLogger(__name__)


GRADER_SYSTEM = (
    "You are an automated acceptance grader. Given a user question and a "
    "candidate answer, decide whether the answer is acceptable to return to "
    "the user as-is. Be strict: reject if the answer is factually wrong, "
    "incomplete, off-topic, or dodges the question.\n\n"
    "Reply with ONLY a JSON object, no prose, no markdown fences:\n"
    '{"acceptable": true|false, "reason": "short sentence"}\n'
    "Max 80 tokens. Output MUST be valid JSON."
)


@dataclass
class CascadeVerdict:
    acceptable: bool
    reason: str = ""


def parse_verdict(text: str) -> CascadeVerdict:
    """Robust parser; falls back to acceptable=False if JSON is malformed."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # strip a markdown code fence
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        else:
            cleaned = cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(cleaned[start:end + 1])
            return CascadeVerdict(
                acceptable=bool(obj.get("acceptable", False)),
                reason=str(obj.get("reason", ""))[:200],
            )
        except (ValueError, TypeError):
            pass
    # Lenient fallback: look for yes/no words
    lower = cleaned.lower()
    if "\"acceptable\": true" in lower or "\"acceptable\":true" in lower:
        return CascadeVerdict(acceptable=True, reason="lenient-yes")
    if "acceptable: true" in lower and "false" not in lower:
        return CascadeVerdict(acceptable=True, reason="lenient-yes")
    # Default to reject so we escalate when the grader misbehaves
    return CascadeVerdict(acceptable=False, reason="parse-failed")


async def grade_answer(
    grader_model: str,
    grader_kwargs: dict,
    user_text: str,
    candidate_answer: str,
    max_tokens: int = 100,
) -> CascadeVerdict:
    """Invoke the grader and return a verdict."""
    kwargs = {k: v for k, v in grader_kwargs.items() if k not in ("max_tokens", "system", "stream")}
    try:
        resp = await acompletion_with_retry(
            model=grader_model,
            messages=[
                {"role": "system", "content": GRADER_SYSTEM},
                {"role": "user", "content": (
                    f"Question:\n{user_text}\n\n"
                    f"Candidate answer:\n{candidate_answer}"
                )},
            ],
            max_tokens=max_tokens,
            stream=False,
            **kwargs,
        )
        text = resp.choices[0].message.content or ""
        return parse_verdict(text)
    except Exception as exc:
        logger.warning("cascade.grader_failed %s", exc)
        # On grader error, be conservative — escalate.
        return CascadeVerdict(acceptable=False, reason=f"grader-error:{type(exc).__name__}")


def cascade_requested(lmrh_cascade: Optional[str], x_cascade_header: Optional[str]) -> bool:
    """Opt-in detection: LMRH cascade=auto OR X-Cot-Cascade: on."""
    if (lmrh_cascade or "").lower() == "auto":
        return True
    if (x_cascade_header or "").lower() in ("on", "true", "1", "auto"):
        return True
    return False
