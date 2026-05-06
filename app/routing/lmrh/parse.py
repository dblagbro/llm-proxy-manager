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
    """Legacy comma-tolerant parser for clients not emitting strict RFC 8941.

    v3.0.68: handles value-internal commas for list-valued dims (e.g.
    ``provider-hint=claude-oauth,codex-oauth`` or ``region=us,ca``).
    Previously the naive ``header_value.split(",")`` ate every comma —
    so multi-value dims silently degraded to first-value-only and the
    rest of the list became unkeyed bare tokens that got dropped, while
    surfacing as ``unknown-dim:<value>`` warnings to the caller.
    DevinGPT v2.74.x flagged the spec/impl gap on 2026-05-06.

    Algorithm: split on commas, then merge any chunk that doesn't
    contain ``=`` (after modifier-stripping) back into the previous
    dim's value. ``task=reasoning, exclude=foo,bar`` parses as
    ``task=reasoning`` + ``exclude=foo,bar`` rather than three pieces
    with ``bar`` orphaned.

    RFC 8941 InnerList form (``provider-hint=(a b c)``) still works
    via the http_sfv path when that library is installed.
    """
    chunks = [c.strip() for c in header_value.split(",")]
    chunks = [c for c in chunks if c]

    # Pre-scan: peel ;modifiers off chunks BEFORE deciding key vs continuation,
    # because a chunk like ``foo;require`` with no ``=`` is still a continuation
    # of the previous dim's value, not its own dim.
    processed: list[tuple[str, bool, bool]] = []
    for chunk in chunks:
        sov = bool(_SOVEREIGN_RE.search(chunk))
        if sov:
            chunk = _SOVEREIGN_RE.sub("", chunk).strip()
        req = bool(_REQUIRE_RE.search(chunk)) or sov
        if req:
            chunk = _REQUIRE_RE.sub("", chunk).strip()
        processed.append((chunk, req, sov))

    # Merge continuation chunks (no ``=``) into the previous dim's value.
    # If the FIRST chunk has no ``=`` it's a malformed header — drop it.
    merged: list[tuple[str, str, bool, bool]] = []  # (key, value, required, sovereign)
    for chunk, req, sov in processed:
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            merged.append((key.strip(), value.strip(), req, sov))
        elif merged:
            # Continuation of previous dim's value. The ;require/;sovereign
            # modifier on the continuation chunk applies to the WHOLE dim,
            # so OR it into the previous dim's flags.
            prev_key, prev_value, prev_req, prev_sov = merged[-1]
            merged[-1] = (
                prev_key,
                f"{prev_value},{chunk}" if chunk else prev_value,
                prev_req or req,
                prev_sov or sov,
            )
        # else: orphaned chunk at start with no ``=`` → drop silently

    hint = LMRHHint(raw=header_value)
    for key, value, req, sov in merged:
        hint.dimensions.append(HintDimension(
            key, value, required=req, sovereign=sov,
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
