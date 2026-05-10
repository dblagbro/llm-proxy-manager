"""v3.6.0 — ETag helpers for the model-identity edit API.

The ETag is a deterministic hash of the editable identity fields
+ ``updated_at``. When a canonical model_id maps to multiple
``ModelCapability`` rows (same upstream model served by multiple
providers), the ETag covers the merged state across all matching
rows so a stale write to ANY of them triggers 412.

See ``docs/rfc/2026-05-model-identity-put-spec.md`` §6.
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from app.models.db import ModelCapability


def etag_for_capability(cap: ModelCapability) -> str:
    """ETag for a single ``ModelCapability`` row's identity state.

    Uses sorted aliases so that two rows with logically identical
    aliases (regardless of insertion order) produce the same ETag.
    """
    state = {
        "model_id": cap.model_id,
        "aliases": sorted(cap.aliases or []),
        "family": cap.model_family,
        "variant": cap.model_variant,
        "updated_at": cap.updated_at.isoformat() if cap.updated_at else "",
    }
    h = hashlib.sha256(
        json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return f'"{h}"'


def etag_for_canonical_model(rows: Iterable[ModelCapability]) -> str:
    """ETag covering the merged state of all rows representing the
    same canonical model_id. Computed by hashing the sorted list of
    per-row ETags — so a change to ANY row flips the canonical ETag.
    """
    parts = sorted(etag_for_capability(r) for r in rows)
    h = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f'"{h}"'


def parse_if_match(header: str | None) -> str | None:
    """Strip the W/ weak-validator prefix and surrounding quotes
    from an ``If-Match`` header value, returning a normalized form
    that can be compared to ``etag_for_canonical_model`` output.

    None / empty → None (caller treats as missing).
    """
    if not header:
        return None
    s = header.strip()
    # Spec allows If-Match: * for "exists at all" semantics — we
    # don't need that here; treat as missing.
    if s == "*":
        return None
    if s.startswith("W/"):
        s = s[2:].strip()
    if not s.startswith('"'):
        s = f'"{s}"'
    if not s.endswith('"'):
        s = f'{s}"'
    return s
