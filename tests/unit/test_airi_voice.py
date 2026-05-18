"""AIRI v4.2 milestone 1 — voice STT backend (config + wiring).

The /transcribe endpoint is a thin auth-gated proxy to the whisper-bridge
sidecar, and the sidecar pulls in faster-whisper (not a main-app dep), so —
as with the other AIRI endpoints — behaviour is verified by the live smoke;
these tests lock the config flags and the wiring.
"""
from __future__ import annotations

import json
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


# --- v4.2.1 milestone 3: hands-free "Airy" wake word -----------------------

def test_voice_model_endpoint_exists_and_gated():
    """The proxy serves the Vosk model to the browser, double-gated like
    /transcribe and sourced from the whisper-bridge sidecar."""
    src = Path("app/api/airi.py").read_text()
    assert '@router.get("/voice-model")' in src
    assert "settings.airi_enabled" in src and "settings.airi_voice_enabled" in src
    assert "/vosk-model" in src                 # proxied from the sidecar
    assert "airi_whisper_bridge_url" in src


def test_whisper_bridge_serves_vosk_model():
    appsrc = Path("whisper_bridge/app.py").read_text()
    assert '@app.get("/vosk-model")' in appsrc
    assert "VOSK_MODEL_PATH" in appsrc
    # same bearer-token guard as /transcribe
    assert "BRIDGE_TOKEN" in appsrc


def test_whisper_bridge_dockerfile_bakes_vosk_model():
    df = Path("whisper_bridge/Dockerfile").read_text()
    assert "vosk-model-small-en-us" in df
    # the upstream TLS cert is expired, so a pinned SHA256 — not the cert —
    # is what verifies the download's integrity
    assert "VOSK_SHA256" in df and "sha256sum -c" in df


def test_handsfree_component_present_and_wired():
    base = Path("frontend/src/components/airi")
    hf = base / "AiriHandsFree.tsx"
    assert hf.exists()
    src = hf.read_text()
    # in-browser ASR — vosk-browser, dynamically imported so its WASM payload
    # only loads when hands-free is turned on
    assert "vosk-browser" in src and "import('vosk-browser')" in src
    # the wake word, and review-before-send (never auto-sends — matches M2)
    assert "airy" in src.lower()
    assert "onTranscript" in src
    # rendered by the chat panel alongside the push-to-talk mic
    assert "AiriHandsFree" in (base / "AiriChatPanel.tsx").read_text()


def test_handsfree_uses_grammar_and_whisper_for_command():
    """v4.2.2 fix — a free Vosk recognizer mis-hears 'Airy' as 'every', so
    wake detection is grammar-constrained, and the command is transcribed by
    Whisper (a grammar recognizer cannot transcribe open-ended speech)."""
    src = Path("frontend/src/components/airi/AiriHandsFree.tsx").read_text()
    # grammar-constrained wake recognizer — only emits "airy" or "[unk]"
    assert "WAKE_GRAMMAR" in src
    assert '[unk]' in src
    # the command is recorded and sent to Whisper via the transcribe endpoint
    assert "MediaRecorder" in src
    assert "/api/airi/transcribe" in src


def test_vosk_browser_dependency_declared():
    pkg = json.loads(Path("frontend/package.json").read_text())
    assert "vosk-browser" in pkg.get("dependencies", {})


# --- v4.3 milestone 1: text-to-speech (Piper) ------------------------------

def test_tts_flag_default_off():
    assert settings.airi_tts_enabled is False           # off until v4.3.0 ships


def test_status_exposes_tts_enabled():
    src = Path("app/api/airi.py").read_text()
    assert '"tts_enabled"' in src


def test_speak_endpoint_exists_and_gated():
    """The /speak proxy is admin- and double-flag-gated, forwards to the
    whisper-bridge sidecar, and caps the text length."""
    src = Path("app/api/airi.py").read_text()
    assert '@router.post("/speak")' in src
    assert "settings.airi_enabled" in src and "settings.airi_tts_enabled" in src
    assert "airi_whisper_bridge_url" in src
    assert "_MAX_TTS_CHARS" in src


def test_whisper_bridge_speak_route():
    appsrc = Path("whisper_bridge/app.py").read_text()
    assert '@app.post("/speak")' in appsrc
    assert "PIPER_BIN" in appsrc
    # bearer-token guarded, like /transcribe
    assert "BRIDGE_TOKEN" in appsrc
    # synthesised audio is never persisted — temp file, deleted on exit
    assert "NamedTemporaryFile" in appsrc


def test_whisper_bridge_dockerfile_bakes_piper():
    df = Path("whisper_bridge/Dockerfile").read_text()
    assert "piper" in df.lower()
    # the "Airy" voice and the pinned Piper binary version are baked in
    assert "en_US-amy-medium" in df
    assert "PIPER_VERSION" in df
