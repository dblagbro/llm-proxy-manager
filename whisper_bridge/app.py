"""whisper-bridge — voice sidecar for AIRI (v4.2 + v4.3).

Self-hosted, on our own infrastructure: faster-whisper speech-to-text
(POST /transcribe), the Vosk wake-word model for hands-free (GET /vosk-model),
and Piper text-to-speech (POST /speak). No persistence, no external network —
audio and text live only for the request. See docs/4.2-voice-design.md and
docs/4.3-tts-design.md.
"""
from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from faster_whisper import WhisperModel
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whisper-bridge")

MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
BRIDGE_TOKEN = os.environ.get("WHISPER_BRIDGE_TOKEN", "")
MAX_BYTES = int(os.environ.get("WHISPER_MAX_BYTES", str(25 * 1024 * 1024)))
# v4.2 hands-free — the Vosk wake-word model, baked in at build time.
VOSK_MODEL_PATH = "/models/vosk-model-small-en-us-0.15.tar.gz"
# v4.3 TTS — Piper: the standalone binary + the "Airy" voice, baked in.
PIPER_BIN = "/opt/piper/piper"
PIPER_VOICE = os.environ.get("PIPER_VOICE", "en_US-amy-medium")
PIPER_VOICE_PATH = f"/voices/{PIPER_VOICE}.onnx"
MAX_TTS_CHARS = int(os.environ.get("PIPER_MAX_CHARS", "6000"))

_model: "WhisperModel | None" = None


def _get_model() -> WhisperModel:
    """Lazy, process-wide singleton — the model loads once."""
    global _model
    if _model is None:
        logger.info("loading faster-whisper model=%s (cpu/int8)", MODEL_NAME)
        _model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
        logger.info("model loaded")
    return _model


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the model at startup so the first transcription is not slow.
    _get_model()
    yield


app = FastAPI(title="whisper-bridge", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "model": MODEL_NAME, "loaded": _model is not None,
            "vosk_model": os.path.exists(VOSK_MODEL_PATH),
            "tts": os.path.exists(PIPER_BIN) and os.path.exists(PIPER_VOICE_PATH)}


@app.get("/vosk-model")
async def vosk_model(authorization: str = Header(None)):
    """Serve the Vosk wake-word model (.tar.gz) for browser-side hands-free
    detection. Bearer-token guarded — only llm-proxy2's voice-model proxy
    calls this. The model is static; no external fetch at runtime."""
    if BRIDGE_TOKEN and authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid bridge token")
    if not os.path.exists(VOSK_MODEL_PATH):
        raise HTTPException(status_code=404, detail="vosk model not available")
    return FileResponse(VOSK_MODEL_PATH, media_type="application/gzip",
                        filename="vosk-model.tar.gz")


@app.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    authorization: str = Header(None),
) -> dict:
    """Transcribe one audio clip. Bearer-token guarded. The audio is read
    into a temp file (faster-whisper decodes via ffmpeg), transcribed, and
    the temp file is deleted on context exit — nothing is persisted."""
    if BRIDGE_TOKEN and authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid bridge token")

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="empty audio")
    if len(data) > MAX_BYTES:
        raise HTTPException(status_code=413, detail="audio too large")

    suffix = os.path.splitext(file.filename or "")[1] or ".webm"
    with tempfile.NamedTemporaryFile(suffix=suffix) as tf:
        tf.write(data)
        tf.flush()
        try:
            segments, audio_info = _get_model().transcribe(tf.name, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as e:
            logger.warning("transcription failed: %r", e)
            raise HTTPException(status_code=502, detail="transcription failed")

    return {
        "text": text,
        "language": getattr(audio_info, "language", None),
        "duration_ms": int((getattr(audio_info, "duration", 0) or 0) * 1000),
    }


class SpeakRequest(BaseModel):
    text: str


@app.post("/speak")
async def speak(req: SpeakRequest, authorization: str = Header(None)):
    """Synthesize text to speech with Piper (v4.3). Bearer-token guarded.
    Piper writes a WAV to a temp file, which is read back and returned, then
    deleted on context exit — nothing is persisted."""
    if BRIDGE_TOKEN and authorization != f"Bearer {BRIDGE_TOKEN}":
        raise HTTPException(status_code=401, detail="invalid bridge token")

    # Collapse to a single line — Piper treats each input line as its own
    # utterance, and an AIRI answer may contain newlines.
    text = " ".join((req.text or "").split())
    if not text:
        raise HTTPException(status_code=400, detail="empty text")
    if len(text) > MAX_TTS_CHARS:
        raise HTTPException(status_code=413, detail="text too long")
    if not (os.path.exists(PIPER_BIN) and os.path.exists(PIPER_VOICE_PATH)):
        raise HTTPException(status_code=503, detail="tts is not available")

    with tempfile.NamedTemporaryFile(suffix=".wav") as tf:
        try:
            subprocess.run(
                [PIPER_BIN, "--model", PIPER_VOICE_PATH, "--output_file", tf.name],
                input=text.encode("utf-8"),
                capture_output=True, timeout=60, check=True, cwd="/opt/piper",
            )
        except Exception as e:
            logger.warning("piper synthesis failed: %r", e)
            raise HTTPException(status_code=502, detail="tts synthesis failed")
        tf.seek(0)
        audio = tf.read()

    if not audio:
        raise HTTPException(status_code=502, detail="tts produced no audio")
    return Response(content=audio, media_type="audio/wav")
