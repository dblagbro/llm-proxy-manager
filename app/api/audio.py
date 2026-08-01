"""
v5.9.0 — OpenAI-compatible /v1/audio/* dispatch.

Two endpoints, both shaped exactly like OpenAI's audio API so callers can
swap base_url and get parity:

    POST /v1/audio/speech         (TTS)
    POST /v1/audio/transcriptions (Whisper STT)

Routing: same ``select_provider`` machinery as chat completions. Models
must be advertised by at least one provider's scanned capabilities.
Subscription-OAuth providers (claude-oauth, ChatGPT-oauth-plan) are
excluded — neither subscription tier exposes audio.

Local-CPU fallback: the ``whisper-bridge`` sidecar (Piper for TTS, Whisper
for STT) is already wired in compose. When the upstream provider call
errors AND ``settings.audio_fallback_to_whisper_bridge`` is True (default),
we transparently retry against the bridge. Operator opt-out via
``audio_fallback_to_whisper_bridge=false`` system setting if the bridge
is unavailable or undesirable for billing-audit reasons.

DevinGPT memo 2026-06-21 — drops their direct-OpenAI usage in favor of
unified billing + audit + fallback through the proxy.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import httpx
import litellm
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import resolve_api_key_dep
from app.config import settings
from app.models.database import get_db
from app.routing.router import select_provider, build_litellm_kwargs, build_litellm_model
from app.utils.disconnect_watchdog import watch_for_disconnect

logger = logging.getLogger(__name__)
router = APIRouter(tags=["audio"])


_AUTH = resolve_api_key_dep()


_EXCLUDED_TYPES = {"claude-oauth", "ChatGPT-oauth-plan", "grok-web", "cursor-oauth"}
_AUDIO_TIMEOUT_SEC = 60.0
_TTS_BRIDGE_MAX_CHARS = 4000


def _bridge_url() -> str:
    return (getattr(settings, "airi_whisper_bridge_url", "") or "").rstrip("/")


def _bridge_headers() -> dict:
    tok = getattr(settings, "airi_whisper_bridge_token", "") or ""
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def _fallback_enabled() -> bool:
    return bool(getattr(settings, "audio_fallback_to_whisper_bridge", True))


async def _bridge_tts(text: str) -> Response:
    bridge = _bridge_url()
    if not bridge:
        raise HTTPException(503, "Audio TTS unavailable and whisper-bridge is not configured.")
    if len(text) > _TTS_BRIDGE_MAX_CHARS:
        text = text[:_TTS_BRIDGE_MAX_CHARS]
    async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT_SEC) as cli:
        r = await cli.post(f"{bridge}/speak", json={"text": text}, headers=_bridge_headers())
    r.raise_for_status()
    return Response(
        content=r.content,
        media_type="audio/wav",
        headers={
            "X-Audio-Source": "whisper-bridge-fallback",
            "X-Audio-Engine": "piper",
        },
    )


async def _bridge_stt(file_bytes: bytes, filename: str, language: Optional[str]) -> dict:
    bridge = _bridge_url()
    if not bridge:
        raise HTTPException(503, "Audio STT unavailable and whisper-bridge is not configured.")
    files = {"file": (filename, file_bytes)}
    data = {}
    if language:
        data["language"] = language
    async with httpx.AsyncClient(timeout=_AUDIO_TIMEOUT_SEC) as cli:
        r = await cli.post(
            f"{bridge}/transcribe", files=files, data=data, headers=_bridge_headers(),
        )
    r.raise_for_status()
    out = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}
    return out


@router.post("/v1/audio/speech")
async def audio_speech(
    request: Request,
    # v5.21.14 — db BEFORE _watchdog (LIFO cleanup closes get_db last). See cluster.py v5.21.12.
    db: AsyncSession = Depends(get_db),
    _watchdog: None = Depends(watch_for_disconnect),  # v5.9.9 — see messages.py
    key_record=Depends(_AUTH),
):
    """OpenAI-compatible TTS.

    Body: ``{model, voice, input, response_format?, speed?}``.
    Response: ``audio/mpeg`` (or whatever the upstream returns; the
    whisper-bridge fallback returns ``audio/wav``).
    """
    body = await request.json()
    model = body.get("model")
    voice = body.get("voice")
    text_input = body.get("input")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "Missing required field: model")
    if not isinstance(text_input, str) or not text_input.strip():
        raise HTTPException(400, "Missing required field: input")
    response_format = body.get("response_format") or "mp3"
    speed = body.get("speed")

    upstream_err: Optional[str] = None
    try:
        route = await select_provider(
            db, hint=None, has_tools=False, has_images=False,
            key_type=key_record.key_type,
            pinned_provider_id=None, model_override=model,
            sort_mode=None,
            excluded_provider_types=_EXCLUDED_TYPES,
        )
    except RuntimeError as e:
        upstream_err = str(e)
        route = None

    if route is not None:
        provider = route.provider
        kwargs = build_litellm_kwargs(provider)
        litellm_model = build_litellm_model(provider, model_override=model)
        extra: dict = {}
        if voice:
            extra["voice"] = voice
        if response_format:
            extra["response_format"] = response_format
        if speed is not None:
            extra["speed"] = speed
        t0 = time.monotonic()
        try:
            result = await litellm.aspeech(
                model=litellm_model, input=text_input, **kwargs, **extra,
            )
            from app.routing.circuit_breaker import record_success
            await record_success(provider.id)
            audio_bytes = getattr(result, "content", None) or getattr(result, "read", lambda: b"")()
            if isinstance(audio_bytes, bytes) and audio_bytes:
                media = "audio/mpeg" if response_format in ("mp3", None) else f"audio/{response_format}"
                headers = {
                    "X-Provider-Type": provider.provider_type,
                    "X-Resolved-Provider": provider.name,
                    "X-Resolved-Model": litellm_model,
                    "X-Audio-Source": "upstream",
                    "X-Audio-Latency-Ms": f"{(time.monotonic() - t0) * 1000.0:.1f}",
                }
                # v5.14.1 — response-shaping hook runner.
                try:
                    from app.api._response_hook_runner import apply_response_hooks, HookContext
                    await apply_response_hooks(
                        handler_id="audio.speech",
                        resp_headers=headers,
                        context=HookContext(
                            requested_model=body.get("model") if isinstance(body, dict) else None,
                            served_model=litellm_model,
                            api_key_id=getattr(key_record, "id", None),
                            provider_id=getattr(provider, "id", None),
                            key_record=key_record,
                            request=request,
                        ),
                    )
                except Exception:
                    pass
                return Response(content=audio_bytes, media_type=media, headers=headers)
            upstream_err = "empty audio body from upstream"
        except Exception as e:
            from app.routing.circuit_breaker import record_failure, is_billing_error
            err_str = str(e)
            await record_failure(provider.id, billing_error=is_billing_error(err_str))
            upstream_err = err_str.split("\nTraceback", 1)[0].strip()[:400]

    # Fallback
    if _fallback_enabled():
        logger.info("audio.speech.fallback_to_bridge upstream_err=%s", upstream_err)
        return await _bridge_tts(text_input)
    raise HTTPException(502, f"Audio TTS upstream error: {upstream_err}. "
                              "Fallback to whisper-bridge is disabled "
                              "(audio_fallback_to_whisper_bridge=false).")


@router.post("/v1/audio/transcriptions")
async def audio_transcriptions(
    request: Request,
    # v5.21.14 — db BEFORE _watchdog (LIFO cleanup closes get_db last). See cluster.py v5.21.12.
    db: AsyncSession = Depends(get_db),
    _watchdog: None = Depends(watch_for_disconnect),  # v5.9.9 — see messages.py
    key_record=Depends(_AUTH),
    file: UploadFile = File(...),
    model: str = Form(...),
    language: Optional[str] = Form(None),
    prompt: Optional[str] = Form(None),
    response_format: Optional[str] = Form(None),
    temperature: Optional[float] = Form(None),
):
    """OpenAI-compatible Whisper STT.

    Multipart form: ``file=@audio``, ``model=whisper-1``, optional
    ``language``, ``prompt``, ``response_format``, ``temperature``.
    Response: ``{text}`` (or richer shape when ``response_format=verbose_json``).
    """
    if not model.strip():
        raise HTTPException(400, "Missing required field: model")
    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(400, "Empty audio file")

    upstream_err: Optional[str] = None
    try:
        route = await select_provider(
            db, hint=None, has_tools=False, has_images=False,
            key_type=key_record.key_type,
            pinned_provider_id=None, model_override=model,
            sort_mode=None,
            excluded_provider_types=_EXCLUDED_TYPES,
        )
    except RuntimeError as e:
        upstream_err = str(e)
        route = None

    if route is not None:
        provider = route.provider
        kwargs = build_litellm_kwargs(provider)
        litellm_model = build_litellm_model(provider, model_override=model)
        extra: dict = {}
        if language:
            extra["language"] = language
        if prompt:
            extra["prompt"] = prompt
        if response_format:
            extra["response_format"] = response_format
        if temperature is not None:
            extra["temperature"] = temperature
        t0 = time.monotonic()
        try:
            # litellm.atranscription takes a file-like object
            import io
            buf = io.BytesIO(file_bytes)
            buf.name = file.filename or "audio.wav"
            result = await litellm.atranscription(
                model=litellm_model, file=buf, **kwargs, **extra,
            )
            from app.routing.circuit_breaker import record_success
            await record_success(provider.id)
            if hasattr(result, "model_dump"):
                body_out = result.model_dump()
            elif hasattr(result, "dict"):
                body_out = result.dict()
            else:
                body_out = dict(result) if not isinstance(result, dict) else result
            elapsed_ms = (time.monotonic() - t0) * 1000.0
            headers = {
                "X-Provider-Type": provider.provider_type,
                "X-Resolved-Provider": provider.name,
                "X-Resolved-Model": litellm_model,
                "X-Audio-Source": "upstream",
                "X-Audio-Latency-Ms": f"{elapsed_ms:.1f}",
            }
            # v5.14.1 — response-shaping hook runner.
            try:
                from app.api._response_hook_runner import apply_response_hooks, HookContext
                await apply_response_hooks(
                    handler_id="audio.transcriptions",
                    resp_headers=headers,
                    context=HookContext(
                        served_model=litellm_model,
                        api_key_id=getattr(key_record, "id", None),
                        provider_id=getattr(provider, "id", None),
                        key_record=key_record,
                        request=request,
                    ),
                )
            except Exception:
                pass
            return JSONResponse(content=body_out, headers=headers)
        except Exception as e:
            from app.routing.circuit_breaker import record_failure, is_billing_error
            err_str = str(e)
            await record_failure(provider.id, billing_error=is_billing_error(err_str))
            upstream_err = err_str.split("\nTraceback", 1)[0].strip()[:400]

    if _fallback_enabled():
        logger.info("audio.transcribe.fallback_to_bridge upstream_err=%s", upstream_err)
        body_out = await _bridge_stt(file_bytes, file.filename or "audio.wav", language)
        return JSONResponse(content=body_out, headers={
            "X-Audio-Source": "whisper-bridge-fallback",
            "X-Audio-Engine": "whisper",
        })
    raise HTTPException(502, f"Audio STT upstream error: {upstream_err}. "
                              "Fallback to whisper-bridge is disabled.")
