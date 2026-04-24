"""
Multi-vendor OAuth passthrough capture (v2.5.0, re-packaged 2026-04-24).

This package replaces the former monolithic ``app/api/oauth_capture.py``.
The split isolates four cohesive concerns:

    presets.py       — CapturePreset + PRESETS table (known-CLI defaults)
    serializers.py   — header filters + row → JSON dict + _safe_text
    profiles.py      — /_presets + /_profiles/... CRUD endpoints
    logs.py          — /_log + /_log/stream + /_log/export endpoints
    passthrough.py   — the /{profile}/{path:path} forwarding catch-all

A single top-level ``router`` is exposed here (merged from the four
sub-routers) so ``main.py`` and any existing import continues to work
unchanged: ``from app.api.oauth_capture import router``.

Helpers previously imported from ``app.api.oauth_capture`` (used by
the unit test suite) are also re-exported: ``_filter_req_headers``,
``_filter_resp_headers``, ``_safe_text``, ``_HOP_BY_HOP``.
"""
from fastapi import APIRouter

from app.api.oauth_capture.profiles import router as _profiles_router
from app.api.oauth_capture.logs import router as _logs_router
from app.api.oauth_capture.passthrough import router as _passthrough_router

# Re-exports for tests and external callers
from app.api.oauth_capture.presets import CapturePreset, PRESETS
from app.api.oauth_capture.serializers import (
    _filter_req_headers, _filter_resp_headers, _safe_text,
    _HOP_BY_HOP,
    _serialize_profile, _serialize_log_summary, _serialize_log_full,
)

# Merge the four routers into one. FastAPI allows this because each
# sub-router declares the same prefix + tags.
router = APIRouter()
router.include_router(_profiles_router)
router.include_router(_logs_router)
# passthrough LAST so /_presets, /_profiles, /_log routes take precedence
# over the catch-all (/{profile_name}/{path:path})
router.include_router(_passthrough_router)

__all__ = [
    "router",
    "CapturePreset", "PRESETS",
    "_filter_req_headers", "_filter_resp_headers", "_safe_text",
    "_HOP_BY_HOP",
    "_serialize_profile", "_serialize_log_summary", "_serialize_log_full",
]
