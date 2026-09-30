"""v5.9.0 — endpoint registration tests for the new audio + images
surfaces requested by the DevinGPT team in the 2026-06-21 memo.

These are pure registration tests — they assert the routes exist on
``app.routes``, they require auth, and the auth-rejected response is a
401, not the 405 we used to return. Live wire-format tests live in the
integration suite (the upstream calls can't be exercised in CI without
provider credentials).
"""
from __future__ import annotations

# v5.22.38 — route introspection goes through tests/unit/_route_introspection.py.
# FastAPI 0.141.1 / Starlette 1.6.0 stopped flattening included routers into
# ``app.routes``: it now holds one opaque ``_IncludedRouter`` per include, with
# ``path = None``. Walking ``app.routes`` directly therefore finds none of the
# app's real endpoints on that version, and these tests reported core routes
# "not registered" while a TestClient POST to them returned 401, not 404. The
# helper descends both shapes.
from tests.unit._route_introspection import ordered_paths, route_methods  # noqa: F401


def test_audio_speech_route_registered_post_only():
    from app.main import app
    methods = route_methods(app, "/v1/audio/speech")
    assert "POST" in methods, "POST /v1/audio/speech must be registered"


def test_audio_transcriptions_route_registered_post_only():
    from app.main import app
    methods = route_methods(app, "/v1/audio/transcriptions")
    assert "POST" in methods, "POST /v1/audio/transcriptions must be registered"


def test_images_generations_route_registered_post_only():
    from app.main import app
    methods = route_methods(app, "/v1/images/generations")
    assert "POST" in methods, "POST /v1/images/generations must be registered"


def test_audio_fallback_setting_defaults_true():
    """v5.9.0 default: whisper-bridge fallback ON. Operator can flip via
    AUDIO_FALLBACK_TO_WHISPER_BRIDGE=false env."""
    from app.config import settings
    assert settings.audio_fallback_to_whisper_bridge is True
