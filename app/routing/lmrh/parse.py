"""LMRH-Hint header parser.

Primary path: RFC 8941 Structured Fields Dictionary via http-sfv. Handles
quoted strings, numeric types, and parameter syntax correctly.

Legacy fallback: a forgiving comma-split parser that keeps backwards
compatibility with clients that send ``task=reasoning,safety-min=3;require``
(not strict 8941).

Split out from the monolithic ``routing/lmrh.py`` in the 2026-04-23
refactor.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from app.routing.lmrh.types import HintDimension, LMRHHint

logger = logging.getLogger(__name__)


def parse_hint(header_value: Optional[str]) -> Optional[LMRHHint]:
    """Parse an LLM-Hint header value into structured dimensions.

    Returns None if the header is empty or yields no recognisable dims.
    """
    if not header_value:
        return None

    parsed = _parse_hint_rfc8941(header_value)
    if parsed is not None:
        return parsed

    # Legacy fallback — preserves backwards compat with clients that send
    # ``task=reasoning,safety-min=3;require`` (not strict 8941).
    return _parse_hint_legacy(header_value)


_REQUIRE_RE = re.compile(r"\s*;\s*require\s*", re.IGNORECASE)
# v3.0.52 (LMRH 1.2 §E3): ``;sovereign`` modifier — implies ;require, plus
# rejects providers with unconfigured / unknown regions on the region dim.
_SOVEREIGN_RE = re.compile(r"\s*;\s*sovereign\s*", re.IGNORECASE)


def _parse_hint_legacy(header_value: str) -> Optional[LMRHHint]:
    hint = LMRHHint(raw=header_value)
    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue
        sovereign = bool(_SOVEREIGN_RE.search(part))
        if sovereign:
            part = _SOVEREIGN_RE.sub("", part).strip()
        required = bool(_REQUIRE_RE.search(part)) or sovereign
        if required:
            part = _REQUIRE_RE.sub("", part).strip()
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        hint.dimensions.append(HintDimension(
            key.strip(), value.strip(), required=required, sovereign=sovereign,
        ))
    return hint if hint.dimensions else None


def _parse_hint_rfc8941(header_value: str) -> Optional[LMRHHint]:
    """RFC 8941 Dictionary parser. Returns None if http-sfv is unavailable
    or the input isn't valid 8941."""
    try:
        import http_sfv
    except ImportError:
        return None

    try:
        d = http_sfv.Dictionary()
        d.parse(header_value.encode())
    except Exception:
        return None

    hint = LMRHHint(raw=header_value)
    for key, item in d.items():
        value_part = item.value if hasattr(item, "value") else item
        if isinstance(value_part, list):
            # InnerList — join values (rare for LMRH, preserve for forward compat)
            value_str = ",".join(_coerce_sfv_value(v) for v in value_part)
        else:
            value_str = _coerce_sfv_value(value_part)
        params = getattr(item, "params", {}) or {}
        sovereign = bool(params.get("sovereign", False))
        required = bool(params.get("require", False)) or sovereign
        hint.dimensions.append(HintDimension(
            key, value_str, required=required, sovereign=sovereign,
        ))
    return hint if hint.dimensions else None


def _coerce_sfv_value(v) -> str:
    """Coerce any RFC 8941 Item value (Token, String, Integer, etc.) to str."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v
    try:
        return v.value if hasattr(v, "value") else str(v)
    except Exception:
        return str(v)
