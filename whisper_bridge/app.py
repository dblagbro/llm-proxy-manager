"""whisper-bridge — speech-to-text sidecar for AIRI voice input (v4.2).

Self-hosted faster-whisper. Receives an audio blob from llm-proxy2 and
returns the transcript. No persistence, no external network — the audio
lives only for the duration of the request. See docs/4.2-voice-design.md.
"""
from __future__ import annotations

import logging
import os
import tempfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("whisper-bridge")

MODEL_NAME = os.environ.get("WHISPER_MODEL", "small")
BRIDGE_TOKEN = os.environ.get("WHISPER_BRIDGE_TOKEN", "")
MAX_BYTES = int(os.environ.get("WHISPER_MAX_BYTES", str(25 * 1024 * 1024)))
# v4.2 hands-free — the Vosk wake-word model, baked in at build time.
VOSK_MODEL_PATH = "/models/vosk-model-small-en-us-0.15.tar.gz"

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
            "vosk_model": os.path.exists(VOSK_MODEL_PATH)}


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
