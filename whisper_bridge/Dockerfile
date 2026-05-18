# whisper-bridge — voice sidecar for AIRI (v4.2 + v4.3).
#
# Self-hosted, all on our own infrastructure, not exposed via nginx:
#   - faster-whisper speech-to-text           POST /transcribe   (v4.2)
#   - the Vosk wake-word model for hands-free  GET  /vosk-model   (v4.2)
#   - Piper text-to-speech                     POST /speak        (v4.3)
# See docs/4.2-voice-design.md and docs/4.3-tts-design.md.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    WHISPER_MODEL=small \
    PIPER_VOICE=en_US-amy-medium

# ffmpeg — faster-whisper decodes the browser's webm/opus (and Safari mp4).
# curl + unzip — fetch and repackage the Vosk wake-word model.
# ca-certificates — the slim base ships without a CA store, so curl's HTTPS
# fetch of the Vosk model fails (exit 60) without it.
# libgomp1 — the OpenMP runtime the Piper binary's onnxruntime links against.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg curl unzip ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# v4.2 hands-free — bake the Vosk small English model into the image, as a
# .tar.gz the browser-side vosk-browser loads. Build-time fetch only; the
# running container serves it locally with no external call.
#
# alphacephei.com's TLS certificate is expired upstream, so curl cannot
# verify the chain. We skip TLS verification (-k) but PIN the SHA256: a real
# tampered download would not match the hash, so content integrity is
# preserved independently of the broken certificate. Update VOSK_SHA256 if
# the upstream model is ever re-released.
ARG VOSK_SHA256=30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498
RUN mkdir -p /models && cd /models \
    && curl -k -fsSL -o vosk.zip \
        https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip \
    && echo "${VOSK_SHA256}  vosk.zip" | sha256sum -c - \
    && unzip -q vosk.zip && rm vosk.zip \
    && tar czf vosk-model-small-en-us-0.15.tar.gz vosk-model-small-en-us-0.15 \
    && rm -rf vosk-model-small-en-us-0.15

# v4.3 TTS — Piper. The standalone prebuilt binary is self-contained (it
# bundles its own onnxruntime, phonemizer and espeak-ng data), which sidesteps
# any Python wheel-compatibility concern on the 3.13 base. Binary and the
# "Airy" voice model are baked in at build time — no external call at runtime.
ARG PIPER_VERSION=2023.11.14-2
RUN cd /opt \
    && curl -fsSL -o piper.tar.gz \
        https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz \
    && tar xzf piper.tar.gz && rm piper.tar.gz
# The "Airy" voice — Piper en_US-amy-medium (HuggingFace serves a valid TLS
# cert, so normal verification applies). The .onnx model + its .onnx.json.
RUN mkdir -p /voices && cd /voices \
    && base=https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium \
    && curl -fsSL -o en_US-amy-medium.onnx      "$base/en_US-amy-medium.onnx" \
    && curl -fsSL -o en_US-amy-medium.onnx.json "$base/en_US-amy-medium.onnx.json"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the model into the image at build time so there is no model download
# (no external fetch) at runtime — own the dependency chain. This RUN is the
# only step that reaches HuggingFace; HF_HUB_OFFLINE is set AFTER it so the
# running container never makes an external call.
RUN python -c "from faster_whisper import WhisperModel; \
WhisperModel('small', device='cpu', compute_type='int8')"
ENV HF_HUB_OFFLINE=1

COPY app.py .

EXPOSE 9000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
