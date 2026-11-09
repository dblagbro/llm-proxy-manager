# Reuse the exact installed speech assets; never fetch mutable models at runtime.
FROM dblagbro/whisper-bridge@sha256:051b41e7e7339527826b730d4ec323d12870ee314363d79f0a1e82d501a5b549 AS speech_assets
FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONUNBUFFERED=1 WHISPER_MODEL=small PIPER_VOICE=en_US-amy-medium HF_HUB_OFFLINE=1
# Keep installed Debian packages patched when intentionally rebuilding this recipe.
RUN apt-get update && apt-get upgrade -y \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=speech_assets /models /models
COPY --from=speech_assets /voices /voices
COPY --from=speech_assets /opt/piper /opt/piper
COPY --from=speech_assets /root/.cache/huggingface /root/.cache/huggingface
WORKDIR /app
COPY requirements.txt requirements.lock ./
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.lock \
    && python -m pip check
COPY app.py ./
EXPOSE 9000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000"]
