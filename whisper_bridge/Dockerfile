# whisper-bridge — speech-to-text sidecar for AIRI voice input (v4.2).
#
# Self-hosted faster-whisper. llm-proxy2 POSTs audio to /transcribe over the
# docker network; the bridge returns the transcript. Not exposed via nginx.
# See docs/4.2-voice-design.md.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    WHISPER_MODEL=small

# ffmpeg — faster-whisper decodes the browser's webm/opus (and Safari mp4)
# through it.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

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
