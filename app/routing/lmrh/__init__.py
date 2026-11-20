"""
LMRH — LLM Model Routing Hint Protocol
Reference implementation of draft-blagbrough-lmrh-00.

This package replaces the former monolithic ``routing/lmrh.py`` module.
The split (2026-04-23 refactor) separates four cohesive concerns:

  types.py     — HintDimension, LMRHHint, CapabilityProfile +
                   WEIGHTS / TASK_ALIASES / LATENCY_RANK / COST_RANK
                   / _REFUSAL_RATE_TO_SAFETY_CEIL
  parse.py     — parse_hint: RFC 8941 parser w/ legacy fallback
  score.py     — score_candidate, rank_candidates,
                   rank_candidates_with_scores
  headers.py   — build_hint_set_header, build_capability_header

Public imports remain unchanged:

    from app.routing.lmrh import parse_hint, rank_candidates, ...

Nothing outside this package should reach into the submodules directly.
"""

from app.routing.lmrh.headers import (
    build_capability_header,
    build_hint_set_header,
)
from app.routing.lmrh.parse import (
    _coerce_sfv_value as _coerce_sfv_value,
)
from app.routing.lmrh.parse import (
    _parse_hint_legacy as _parse_hint_legacy,
)
from app.routing.lmrh.parse import (
    _parse_hint_rfc8941 as _parse_hint_rfc8941,
)
from app.routing.lmrh.parse import (
    parse_hint,
)
from app.routing.lmrh.score import (
    rank_candidates,
    rank_candidates_with_scores,
    score_candidate,
)
from app.routing.lmrh.types import (
    _REFUSAL_RATE_TO_SAFETY_CEIL as _REFUSAL_RATE_TO_SAFETY_CEIL,
)
from app.routing.lmrh.types import (
    _REFUSAL_RATE_TO_SAFETY_FLOOR as _REFUSAL_RATE_TO_SAFETY_FLOOR,
)
from app.routing.lmrh.types import (
    COST_RANK,
    LATENCY_RANK,
    TASK_ALIASES,
    WEIGHTS,
    CapabilityProfile,
    HintDimension,
    LMRHHint,
)

__all__ = [
    # types
    "HintDimension",
    "LMRHHint",
    "CapabilityProfile",
    "WEIGHTS",
    "TASK_ALIASES",
    "LATENCY_RANK",
    "COST_RANK",
    # parser
    "parse_hint",
    # scoring
    "score_candidate",
    "rank_candidates",
    "rank_candidates_with_scores",
    # headers
    "build_hint_set_header",
    "build_capability_header",
]
