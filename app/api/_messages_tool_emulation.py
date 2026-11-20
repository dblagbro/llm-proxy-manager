"""Anthropic tool-emulation response dispatch."""


async def dispatch_tool_emulation(
    *, body, tools, route, messages_list, system, extra, db, key_record, resp_headers, stream
):
    from app.api import messages as handler

    JSONResponse = handler.JSONResponse
    StreamingResponse = handler.StreamingResponse
    anthropic_text_response = handler.anthropic_text_response
    anthropic_text_sse = handler.anthropic_text_sse
    anthropic_tool_response = handler.anthropic_tool_response
    anthropic_tool_sse = handler.anthropic_tool_sse
    anthropic_tools_response = handler.anthropic_tools_response
    anthropic_tools_sse = handler.anthropic_tools_sse
    build_anthropic_tool_prompt = handler.build_anthropic_tool_prompt
    call_with_tool_prompt = handler.call_with_tool_prompt
    normalize_anthropic_messages = handler.normalize_anthropic_messages
    parse_tool_calls = handler.parse_tool_calls
    record_outcome = handler.record_outcome
    strip_thinking = handler.strip_thinking
    time = handler.time

    tool_choice = body.get("tool_choice") or {}
    allow_parallel = True
    if isinstance(tool_choice, dict) and tool_choice.get("disable_parallel_tool_use"):
        allow_parallel = False
    # v4.1.1 — co-emulation: when CoT-E is also engaged on this request
    # the tool prompt is reasoning-prefixed, so the model thinks step
    # by step before emitting tool calls (tools + reasoning together).
    tool_system = build_anthropic_tool_prompt(
        tools or [],
        allow_parallel=allow_parallel,
        with_reasoning=route.cot_engaged,
    )
    merged_system = tool_system + ("\n\n" + system if system else "")
    norm_msgs = normalize_anthropic_messages(messages_list)
    emul_extra = {k: v for k, v in extra.items() if k not in ("tools", "system")}
    # v5.22.41 (BUG-095) — capture the upstream usage. Without this the
    # emulated-response builders below fill in zeros and every cost
    # derived from this request computes as $0.
    _emul_usage: dict = {}
    response_text = await call_with_tool_prompt(
        route.litellm_model, norm_msgs, merged_system, emul_extra, usage_out=_emul_usage
    )
    tool_calls = parse_tool_calls(response_text)
    if route.cot_engaged:
        # drop the <thinking> block from the plain-text fallback
        response_text = strip_thinking(response_text)
    # v3.8.3 (#263) — emit telemetry BEFORE response building so
    # the activity_log row carries the count + validation flag
    # for this emulated tool-call request. Note: response_body
    # is constructed below from tool_calls; we re-derive a
    # synthetic Anthropic-shape response for the meta extractor
    # so success-rate aggregation across native + emulated paths
    # uses the same response_body schema.
    _emul_resp_body = (
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": tc.get("name", ""),
                    "input": tc.get("input", {}) if isinstance(tc.get("input"), dict) else {},
                }
                for tc in tool_calls
            ],
        }
        if tool_calls
        else {"content": []}
    )
    await record_outcome(
        db,
        route.provider.id,
        route.litellm_model,
        success=True,
        t0=time.monotonic(),
        key_record_id=key_record.id,
        response_body=_emul_resp_body,
        tool_call_format="emulated",
    )
    # Enforce serial when parallel is disabled
    if not allow_parallel and len(tool_calls) > 1:
        tool_calls = tool_calls[:1]
    if tool_calls:
        resp_headers["X-Tool-Calls-Emitted"] = str(len(tool_calls))
    if stream:
        if len(tool_calls) >= 2:
            gen = anthropic_tools_sse(tool_calls, usage=_emul_usage, model=route.litellm_model)
        elif len(tool_calls) == 1:
            gen = anthropic_tool_sse(
                tool_calls[0]["name"],
                tool_calls[0]["input"],
                usage=_emul_usage,
                model=route.litellm_model,
            )
        else:
            gen = anthropic_text_sse(
                response_text,
                usage=_emul_usage,
                model=route.litellm_model,
            )
        return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
    else:
        if len(tool_calls) >= 2:
            content = anthropic_tools_response(
                tool_calls,
                route.litellm_model,
                usage=_emul_usage,
            )
        elif len(tool_calls) == 1:
            content = anthropic_tool_response(
                tool_calls[0]["name"],
                tool_calls[0]["input"],
                route.litellm_model,
                usage=_emul_usage,
            )
        else:
            content = anthropic_text_response(
                response_text,
                route.litellm_model,
                usage=_emul_usage,
            )
        # v3.6.1 — merge X-Quality-Hint for tool-emulation path
        from app.api._quality_hint import merge_into_headers

        merge_into_headers(resp_headers, content, endpoint="messages")
        return JSONResponse(content=content, headers=resp_headers)
