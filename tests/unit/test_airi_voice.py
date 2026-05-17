"""AIRI v4.2 milestone 1 — voice STT backend (config + wiring).

The /transcribe endpoint is a thin auth-gated proxy to the whisper-bridge
sidecar, and the sidecar pulls in faster-whisper (not a main-app dep), so —
as with the other AIRI endpoints — behaviour is verified by the live smoke;
these tests lock the config flags and the wiring.
"""
from __future__ import annotations

from pathlib import Path

from app.config import settings


def test_voice_flags_default_off_and_configured():
    assert settings.airi_voice_enabled is False           # off until v4.2.0 ships
    assert settings.airi_whisper_bridge_url                # has a default URL
    assert isinstance(settings.airi_whisper_bridge_token, str)


def test_status_endpoint_exposes_voice_enabled():
    src = Path("app/api/airi.py").read_text()
    assert '"voice_enabled"' in src


def test_transcribe_endpoint_exists_and_double_gated():
    src = Path("app/api/airi.py").read_text()
    assert '@router.post("/transcribe")' in src
    # gated on AIRI overall AND the voice flag
    assert "settings.airi_enabled" in src and "settings.airi_voice_enabled" in src
    # forwarded to the sidecar; a size cap is enforced
    assert "airi_whisper_bridge_url" in src
    assert "_MAX_AUDIO_BYTES" in src


def test_whisper_bridge_sidecar_present():
    base = Path("whisper_bridge")
    assert (base / "app.py").exists()
    assert (base / "Dockerfile").exists()
    assert (base / "requirements.txt").exists()
    appsrc = (base / "app.py").read_text()
    assert "/transcribe" in appsrc and "/health" in appsrc
    assert "faster_whisper" in appsrc
    # bearer-token guarded
    assert "BRIDGE_TOKEN" in appsrc and "Bearer" in appsrc
    # no persistence — audio lives only in a temp file for the request
    assert "NamedTemporaryFile" in appsrc


def test_whisper_bridge_dockerfile_self_hosted_offline():
    df = Path("whisper_bridge/Dockerfile").read_text()
    assert "faster-whisper" in df.lower()
    assert "ffmpeg" in df
    # the model is baked in at build time; the runtime never fetches it
    assert "HF_HUB_OFFLINE" in df
