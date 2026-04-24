"""
Verification-pass helpers for CoT-E.

Extracted from ``app/cot/pipeline.py`` in the 2026-04-24 refactor.
The two helpers here together answer "should we verify?" and "run the
verify pass" — both called once by ``run_cot_pipeline`` near the end of
its flow. Moving them out trims pipeline.py and gives verification its
own home when we eventually add more heuristics.
"""
from __future__ import annotations

import logging

from app.config import settings
from app.cot.critique import should_verify as _should_verify
from app.cot.prompts import VERIFY_SYSTEM

logger = logging.getLogger(__name__)


def resolve_verify(force_verify: bool | None, answer: str) -> bool:
    """Decide whether to run the verification pass for this response."""
    if force_verify is True:
        return True
    if force_verify is False:
        return False
    # None → consult global settings + optional auto-detection
    if not settings.cot_verify_enabled:
        return False
    if settings.cot_verify_auto_detect:
        return _should_verify(answer)
    return True  # enabled globally with auto-detect off → always verify


async def run_verify_pass(
    model: str,
    user_text: str,
    answer: str,
    extra_kwargs: dict,
    *,
    call_fn,
) -> str:
    """Call the model to generate verification steps for the given answer.

    `call_fn` is injected so pipeline.py's existing private _call helper
    stays authoritative — we don't duplicate litellm plumbing here.
    """
    verify_messages = [
        {"role": "user", "content": f"Question:\n{user_text}\n\nAnswer:\n{answer}"},
    ]
    try:
        cot_kw = {k: v for k, v in extra_kwargs.items() if k not in ("max_tokens", "system", "stream")}
        return await call_fn(
            model,
            verify_messages,
            VERIFY_SYSTEM,
            settings.cot_verify_max_tokens,
            **cot_kw,
        )
    except Exception as e:
        logger.warning("cot_verify_pass_failed error=%s", str(e))
        return f"(verification pass failed: {e})"
