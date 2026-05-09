"""
Shared grok-web dispatch — used by both /v1/messages (Anthropic shape)
and /v1/chat/completions (OpenAI shape).

v3.2.x parked near-identical 50-line dispatch blocks in both
``messages.py`` and ``completions.py``. The grok-web flow is the same
in either direction (resolve provider → shape request → forward to
manual or bridge dispatcher → wrap response in caller's wire format),
so the duplication was pure copy-paste. v3.2.9 extracts it here:

- ``dispatch_grok_web_openai()``  — used by completions.py (returns the
  OpenAI-shape result directly; non-stream JSON or stream SSE).
- ``dispatch_grok_web_anthropic()`` — used by messages.py (translates
  the OpenAI-shape result into Anthropic shape; SSE for streaming).

Both helpers raise the same ``HTTPException`` flavors that the inline
blocks did, so call-site error handling didn't need to change.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse


async def dispatch_grok_web_openai(
    *,
    route: Any,
    body: dict,
    stream: bool,
    resp_headers: dict[str, str],
) -> Any:
    """Run a grok-web request and return an OpenAI-shape FastAPI response.

    Used by ``/v1/chat/completions``. Non-streaming returns
    ``JSONResponse``; streaming returns ``StreamingResponse`` carrying
    SSE chunks (``data: {chunk}\\n\\n`` … ``data: [DONE]``).

    Behavior matches the v3.2.0 inline block in completions.py exactly
    — only thing that changed is the call site. Errors get re-raised as
    HTTPException with the appropriate status (401 for auth-style
    failures, ``e.status_code`` for general bridge/upstream errors).
    """
    from app.providers.grok_web import (
        complete_grok_web,
        stream_grok_web,
        GrokWebError,
        GrokWebAuthError,
    )

    msgs = list(body.get("messages") or [])
    requested_model = body.get("model") or route.profile.model_id

    if stream:
        stream_gen = stream_grok_web(
            route.provider.extra_config or {},
            messages=msgs,
            model=requested_model,
        )
        try:
            first_chunk = await stream_gen.__anext__()
        except GrokWebAuthError as e:
            raise HTTPException(401, str(e))
        except GrokWebError as e:
            raise HTTPException(e.status_code, str(e))
        except StopAsyncIteration:
            raise HTTPException(502, "grok-web upstream: empty stream")

        async def _replay():
            yield first_chunk
            async for c in stream_gen:
                yield c

        resp_headers["X-Cache-Status"] = "bypass"
        return StreamingResponse(
            _replay(),
            media_type="text/event-stream",
            headers=resp_headers,
        )

    try:
        result = await complete_grok_web(
            route.provider.extra_config or {},
            messages=msgs,
            model=requested_model,
        )
    except GrokWebAuthError as e:
        raise HTTPException(401, str(e))
    except GrokWebError as e:
        raise HTTPException(e.status_code, str(e))
    resp_headers["X-Cache-Status"] = "bypass"
    return JSONResponse(content=result, headers=resp_headers)


def _flatten_anthropic_system(system: Any) -> Optional[str]:
    """Anthropic's ``system`` can be a string OR a list of content
    blocks; flatten to a single string for the grok-web dispatcher."""
    if not system:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n".join(
            b.get("text", "") for b in system if isinstance(b, dict)
        )
    return str(system)


async def dispatch_grok_web_anthropic(
    *,
    route: Any,
    body: dict,
    stream: bool,
    resp_headers: dict[str, str],
) -> Any:
    """Run a grok-web request and return an Anthropic-shape FastAPI response.

    Used by ``/v1/messages``. Non-streaming returns ``JSONResponse``
    with the Anthropic ``message`` shape; streaming returns
    ``StreamingResponse`` emitting Anthropic events
    (``message_start``, ``content_block_delta``, ``message_stop``).

    Behavior matches the v3.2.0 inline block in messages.py exactly.
    """
    from app.providers.grok_web import (
        complete_grok_web,
        stream_grok_web_anthropic,
        anthropic_response_from_openai,
        GrokWebError,
        GrokWebAuthError,
    )

    msgs_for_grok = list(body.get("messages") or [])
    sys_for_grok = _flatten_anthropic_system(body.get("system"))
    requested_model = body.get("model") or route.profile.model_id

    if stream:
        stream_gen = stream_grok_web_anthropic(
            route.provider.extra_config or {},
            messages=msgs_for_grok,
            system=sys_for_grok,
            model=requested_model,
        )
        try:
            first_chunk = await stream_gen.__anext__()
        except GrokWebAuthError as e:
            raise HTTPException(401, str(e))
        except GrokWebError as e:
            raise HTTPException(e.status_code, str(e))
        except StopAsyncIteration:
            raise HTTPException(502, "grok-web upstream: empty stream")

        async def _replay():
            yield first_chunk
            async for c in stream_gen:
                yield c

        resp_headers["X-Cache-Status"] = "bypass"
        return StreamingResponse(
            _replay(),
            media_type="text/event-stream",
            headers=resp_headers,
        )

    # Non-streaming: complete_grok_web returns OpenAI shape regardless of
    # caller; we translate to Anthropic /v1/messages shape here.
    msgs_with_system = (
        [{"role": "system", "content": sys_for_grok}] + msgs_for_grok
        if sys_for_grok else msgs_for_grok
    )
    try:
        openai_result = await complete_grok_web(
            route.provider.extra_config or {},
            messages=msgs_with_system,
            model=requested_model,
        )
    except GrokWebAuthError as e:
        raise HTTPException(401, str(e))
    except GrokWebError as e:
        raise HTTPException(e.status_code, str(e))
    anth_result = anthropic_response_from_openai(openai_result)
    resp_headers["X-Cache-Status"] = "bypass"
    return JSONResponse(content=anth_result, headers=resp_headers)
