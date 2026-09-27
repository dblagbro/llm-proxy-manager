"""The non-streaming arm of /v1/messages dispatch.

v5.22.37 — split from ``_messages_response_dispatch.py``, which reached 692 LOC
against the 700 ceiling in ``test_v52237_handler_split_ceilings``. Eight lines of
headroom is not headroom, and that module's own docstring says to split this arm
rather than raise the number, so this is that split.

The two arms are roughly 1:2 in size, so separating them leaves both files well
clear of the ceiling. ``MessagesDispatchCtx`` stays in
``_messages_response_dispatch``, which re-exports ``dispatch_non_streaming`` — so
``messages.py`` imports all three names from one place and did not change.

The body below is **byte-identical** to the block it came from in messages.py,
preceded by an unpacking prologue that restores the local names it ran under.

Do NOT run ``ruff --fix`` over this file. The first attempt at this split did,
and it reached inside the function to reorder the local imports and delete
``grade_answer`` from one of them — breaking byte-identity and 14 tests. The
function-local imports here are part of the moved code, not header hygiene.
"""
from __future__ import annotations

from app.api._messages_response_dispatch import MessagesDispatchCtx
import logging
import time
from fastapi.responses import JSONResponse, StreamingResponse
from app.api._messages_dispatch import (
    try_cascade_dispatch,
)
from app.cache.middleware import maybe_store
from app.config import settings
from app.cot.sse import (
    anthropic_text_sse,
    anthropic_tool_sse,
    anthropic_tools_sse,
    to_anthropic_response,
)
from app.monitoring.helpers import record_outcome
from app.routing.litellm_binding import clamp_thinking_budget
from app.routing.retry import acompletion_with_retry

logger = logging.getLogger(__name__)


async def dispatch_non_streaming(ctx: MessagesDispatchCtx):
    """The ``else:`` arm, verbatim from messages.py:997-1347."""
    _buffered_cascade_stream = ctx._buffered_cascade_stream
    _proxy_tools_injected = ctx._proxy_tools_injected
    alias = ctx.alias
    anthropic_beta = ctx.anthropic_beta
    background_tasks = ctx.background_tasks
    body = ctx.body
    cache_decision = ctx.cache_decision
    db = ctx.db
    extra = ctx.extra
    has_images = ctx.has_images
    has_tools = ctx.has_tools
    hint = ctx.hint
    key_record = ctx.key_record
    litellm_tools = ctx.litellm_tools
    llm_hint = ctx.llm_hint
    max_tokens = ctx.max_tokens
    messages_list = ctx.messages_list
    request = ctx.request
    resp_headers = ctx.resp_headers
    route = ctx.route
    system = ctx.system
    thinking = ctx.thinking
    x_conversation_id = ctx.x_conversation_id
    x_cot_cascade = ctx.x_cot_cascade
    x_memory_tag = ctx.x_memory_tag

    t0 = time.monotonic()
    # Wave 3 #17 — ordered fallback across ranked providers
    from app.routing.fallback import try_ranked_non_streaming
    # Wave 3 #14 — cascade routing (cheap → grade → escalate)
    from app.routing.cascade import cascade_requested, grade_answer
    lmrh_cascade = hint.get("cascade").value if (hint and hint.get("cascade")) else None
    do_cascade = cascade_requested(lmrh_cascade, x_cot_cascade)

    async def _call_with_route(r):
        # Rebuild extra kwargs for THIS route's provider (api_key, etc.)
        local_extra = {**r.litellm_kwargs, "max_tokens": max_tokens}
        if system:
            local_extra["system"] = system
        if litellm_tools:
            local_extra["tools"] = litellm_tools
        if r.native_thinking_params:
            local_extra.update(r.native_thinking_params)
            # v5.3.7 — keep Gemini thinking budget below max_tokens (empty-success fix)
            clamp_thinking_budget(local_extra)
        elif thinking and r.profile.provider_type == "anthropic":
            local_extra["thinking"] = thinking
        if anthropic_beta and r.profile.provider_type == "anthropic":
            local_extra["extra_headers"] = {"anthropic-beta": anthropic_beta}
        # v5.15.1 (#508 Phase 2) — per-account OAuth fan-out. Swap
        # ``local_extra['api_key']`` to the picked account's token
        # when the provider is OAuth-flavored (cursor-oauth today,
        # codex-oauth + claude-oauth same code path once operator
        # seeds accounts). No-op for non-OAuth providers.
        from app.providers.oauth_account_selector import apply_fanout_to_kwargs
        _oauth_account_id = await apply_fanout_to_kwargs(
            local_extra, r.provider, db,
        )
        if _oauth_account_id:
            resp_headers["X-OAuth-Account"] = _oauth_account_id
        # v5.22.4 — release the connection before the non-streaming
        # upstream call (K4: apply_fanout's read was pinned across it).
        try:
            await db.commit()
        except Exception:
            pass
        return await acompletion_with_retry(
            model=r.litellm_model, messages=messages_list,
            stream=False, **local_extra,
        )

    # Cascade: cheap first, grade, escalate only on reject.
    # v4.4.38: cascade orchestration extracted to
    # ``_messages_dispatch.try_cascade_dispatch`` — returns a
    # JSONResponse (accept) or None (escalate / error → fall
    # through to the regular non-streaming path below). resp_headers
    # are mutated in place to reflect X-Cascade-* attribution.
    if do_cascade and not has_tools and not route.cot_engaged:
        cascade_resp = await try_cascade_dispatch(
            route,
            db=db, key_record=key_record, hint=hint,
            has_images=has_images, messages_list=messages_list,
            cache_decision=cache_decision, resp_headers=resp_headers,
            max_tokens=max_tokens, t0=t0,
            call_with_route=_call_with_route,
        )
        if cascade_resp is not None:
            return cascade_resp

    if settings.fallback_enabled:
        result, final_route, chain = await try_ranked_non_streaming(
            db, hint,
            has_tools=has_tools, has_images=has_images,
            key_type=key_record.key_type,
            pinned_provider_id=alias.provider_id if alias else None,
            model_override=alias.model_id if alias else None,
            primary_route=route, call_fn=_call_with_route,
        )
        if len(chain.attempts) > 1:
            resp_headers["X-Fallback-Chain"] = chain.as_header()
            resp_headers["X-Provider"] = final_route.provider.name
            resp_headers["X-Resolved-Model"] = final_route.litellm_model
            route = final_route  # for record_outcome below
    else:
        result = await acompletion_with_retry(
            model=route.litellm_model,
            messages=messages_list,
            stream=False,
            **extra,
        )
    in_tok = getattr(result.usage, "prompt_tokens", 0)
    out_tok = getattr(result.usage, "completion_tokens", 0)
    from app.cot.sse import extract_cache_tokens
    cache_creation, cache_read = extract_cache_tokens(result.usage)
    await record_outcome(
        db, route.provider.id, route.litellm_model, success=True,
        in_tok=in_tok, out_tok=out_tok, t0=t0,
        key_record_id=key_record.id,
        cache_creation=cache_creation, cache_read=cache_read,
        provider_name=route.provider.name,
        # v3.0.35: body capture + diagnostic fields. Anthropic-shape
        # response is converted via to_anthropic_response for activity
        # log so the captured shape matches what the client received.
        request_body=body,
        response_body=to_anthropic_response(result),
        requested_model=body.get("model") if isinstance(body, dict) else None,
        had_lmrh_hint=bool(llm_hint),
        lmrh_hint_raw=llm_hint or None,
        # v3.8.3 (#263) — stamp tool_call_format when the
        # request carried tools=[]. The to_anthropic_response
        # body is what the meta extractor walks.
        tool_call_format=("native" if has_tools else None),
    )
    # Store in semantic cache (fire-and-forget; won't affect response latency)
    try:
        answer_text = result.choices[0].message.content or ""
        await maybe_store(cache_decision, answer_text)
    except Exception:
        pass

    # Wave 3 #16 — shadow traffic (sampled, fire-and-forget)
    if (settings.shadow_traffic_rate > 0
        and settings.shadow_candidate_provider_id
        and settings.shadow_candidate_provider_id != route.provider.id
        and not has_tools):
        from app.routing.shadow import should_shadow, run_shadow_compare
        if should_shadow(settings.shadow_traffic_rate):
            from app.models.database import AsyncSessionLocal
            background_tasks.add_task(
                run_shadow_compare,
                AsyncSessionLocal,
                settings.shadow_candidate_provider_id,
                messages_list,
                answer_text,
                route.litellm_model,
                {"max_tokens": max_tokens,
                 **({"system": system} if system else {})},
                settings.semantic_cache_embedding_model,
                settings.semantic_cache_embedding_dims,
                key_record.id,
            )
            resp_headers["X-Shadow-Queued"] = settings.shadow_candidate_provider_id

    remaining = max(0, max_tokens - out_tok)
    resp_headers["X-Token-Budget-Remaining"] = str(remaining)
    anthropic_result = to_anthropic_response(result)
    # v5.6.0 / v5.7.1 — proxy-injected-tool interception. If
    # the model invoked one of our injected tools (Excel,
    # markitdown, any MCP-aggregator tool), run it in-process
    # and re-call the model with the tool_result so the
    # assistant turn the CALLER sees has the file content
    # already incorporated. Capped at 3 hops to prevent a
    # runaway loop if the model keeps calling the tool.
    #
    # v5.7.1: switched to ``find_proxy_tool_use_async`` so
    # both static-registry tools AND MCP-aggregator-bridge
    # tools are recognized.
    if _proxy_tools_injected:
        from app.proxy_tools import (
            find_proxy_tool_use_async, run_tool, build_tool_result_message,
        )
        _proxy_hops = 0
        while _proxy_hops < 3:
            match = await find_proxy_tool_use_async(
                anthropic_result.get("content") or []
            )
            if not match:
                break
            _proxy_hops += 1
            proxy_tool, input_obj, tool_use_id = match
            tool_output = await run_tool(proxy_tool, input_obj)
            messages_list = list(messages_list) + [
                {
                    "role": "assistant",
                    "content": anthropic_result.get("content") or [],
                },
                build_tool_result_message(tool_use_id, tool_output),
            ]
            result = await acompletion_with_retry(
                model=route.litellm_model,
                messages=messages_list,
                stream=False,
                **extra,
            )
            anthropic_result = to_anthropic_response(result)
        resp_headers["X-Proxy-Tool-Hops"] = str(_proxy_hops)
    # v3.9.0 (#267) Phase 5 — memory-tool write-back on litellm path.
    from app.memory.extract import maybe_extract_memory_writes
    mem_writes = await maybe_extract_memory_writes(
        db, response_dict=anthropic_result,
        api_key_id=key_record.id,
        conversation_id=x_conversation_id,
        memory_tag_default=x_memory_tag,
        source_provider_id=route.provider.id,
    )
    if mem_writes:
        resp_headers["X-Caller-Memory-Writes"] = str(mem_writes)
    # v5.20.1 — refusal cascade. When ``refusal_retry_enabled``
    # is on for this key AND detection fires on the initial
    # response, walk alternate providers (excluding those
    # already tried) until one produces a clean response or
    # max_attempts is exhausted. Non-streaming path only in
    # v5.20.1; streaming cascade is v5.20.2+. Emits
    # X-Refusal-Retry-* response headers + activity_log rows
    # for every attempt so the operator has full attribution.
    try:
        from app.api._refusal_cascade import maybe_cascade_on_refusal

        async def _cascade_dispatch(alt_route):
            # Rebuild the litellm dispatch for a different route.
            # Uses ``acompletion_with_retry`` — same primitive
            # the initial call used. Doesn't re-run privacy /
            # budget filters (those already ran on the initial
            # dispatch). Uses the same messages_list so the model
            # sees the same request.
            _extra = dict(extra)
            if system:
                _extra["system"] = system
            return await acompletion_with_retry(
                model=alt_route.litellm_model,
                messages=messages_list,
                stream=False,
                **_extra,
            )

        _cascade = await maybe_cascade_on_refusal(
            db=db,
            key_record=key_record,
            initial_route=route,
            initial_result=result,
            initial_anthropic=anthropic_result,
            hint=hint,
            has_images=has_images,
            messages_list=messages_list,
            max_tokens=max_tokens,
            system=system,
            extra=extra,
            dispatch=_cascade_dispatch,
            to_anthropic_response=to_anthropic_response,
            resp_headers=resp_headers,
            body=body,
        )
        if _cascade.swapped:
            route = _cascade.final_route
            result = _cascade.final_result
            anthropic_result = _cascade.final_anthropic
    except Exception as exc:
        logger.warning("refusal_cascade.wrapper_failed err=%s", exc)

    # v5.7.6 — capability scout. Off by default; flips on via
    # the capability_scout.enabled system_setting. Fire-and-
    # forget; never blocks the response.
    try:
        from app.capability_scout.scout import scan_and_emit_for_response
        _n = await scan_and_emit_for_response(
            db=db,
            api_key_id=key_record.id,
            provider_id=route.provider.id,
            anthropic_response=anthropic_result,
        )
        if _n:
            resp_headers["X-Capability-Scout-Suggestions"] = str(_n)
    except Exception:
        pass
    # v5.10.0 Ship 1 — emit X-Proxy-MCP-Suggestion when this
    # caller's accumulated score crosses the threshold. Score
    # was just bumped above by scan_and_emit_for_response when
    # a refusal pattern hit; this read sees the fresh value.
    # v5.19.0 — response-tail extracted to _messages_response_tail.
    # Runs the four post-dispatch header/hook blocks:
    # (1) capability-scout suggestion header
    # (2) accept-MCP handler (X-Proxy-Accept-MCP + x-llmproxy-config blob)
    # (3) x-llmproxy-config echo
    # (4) response hooks runner (substitution header + outbound callback)
    # Preserves per-block Exception-swallow posture so downstream
    # ships that add more tail blocks don't need to re-derive the
    # wiring in-place.
    from app.api._messages_response_tail import apply_response_tail
    await apply_response_tail(
        request=request,
        route=route,
        key_record=key_record,
        resp_headers=resp_headers,
        body=body,
        db=db,
        anthropic_result=anthropic_result,
    )
    # v5.20.11 — buffered-cascade streaming: convert the final
    # ``anthropic_result`` to SSE frames and return a
    # StreamingResponse. Text-only path uses ``anthropic_text_sse``
    # (extracts concatenated text blocks); if the result contains
    # tool_use blocks, fall back to a synthesized SSE stream that
    # emits each block. Everything else — image blocks etc — is
    # dropped to text-summary since streaming those in Anthropic's
    # SSE format is out of scope for the cascade shim.
    if _buffered_cascade_stream:
        try:
            # v5.21.1 bugfix — the redundant local `from app.cot.sse
            # import anthropic_text_sse, ...` here made those names
            # LOCAL to the whole `messages()` function. Line 592 then
            # accessed them BEFORE this line ran → UnboundLocalError
            # on every non-buffered-cascade request. Names are
            # already imported at module level (lines 27-29).
            content_blocks = anthropic_result.get("content") or []
            tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
            if len(tool_uses) >= 2:
                gen = anthropic_tools_sse([
                    {"name": t["name"], "input": t.get("input", {})}
                    for t in tool_uses
                ])
            elif len(tool_uses) == 1:
                gen = anthropic_tool_sse(
                    tool_uses[0]["name"], tool_uses[0].get("input", {}),
                )
            else:
                text = "".join(
                    b.get("text", "") for b in content_blocks
                    if b.get("type") == "text"
                )
                gen = anthropic_text_sse(text)
            return StreamingResponse(
                gen, media_type="text/event-stream",
                headers=resp_headers,
            )
        except Exception:
            # Fall through to JSON response if SSE conversion breaks.
            # Caller's SSE parser will fail; header
            # X-Refusal-Cascade-Mode still marks this as buffered.
            pass
    # v5.21.15 — empty-completion guard (cross-family / litellm path).
    # A provider (e.g. an expired cursor-oauth bridge) can return a
    # 200 with no usable content — that must NOT reach the caller as
    # a valid empty completion (CamReview 2026-08-05). Surface a real
    # 502 they can retry instead. The expired-token exclusion in
    # select_provider prevents the known dead-cursor case up front;
    # this is the belt-and-suspenders catch for ANY empty provider.
    # ``return`` (not ``raise``) so the ``except`` below doesn't
    # reclassify it.
    from app.api._messages_dispatch import _is_empty_completion
    if _is_empty_completion(anthropic_result):
        logger.warning(
            "provider %s returned an EMPTY completion on the litellm "
            "path — 502 instead of a silent empty 200",
            getattr(route.provider, "id", "?"),
        )
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {
                "type": "api_error",
                "message": "Upstream provider returned an empty completion",
            }},
            headers=resp_headers,
        )
    return JSONResponse(
        content=anthropic_result,
        headers=resp_headers,
    )
