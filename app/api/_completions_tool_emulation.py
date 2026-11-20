"""Completion response dispatch; dependencies remain patchable at the endpoint."""


async def dispatch_tool_emulation(
    *, body, tools, route, messages_list, extra, db, key_record, resp_headers, stream
):
    from app.api import completions as handler

    JSONResponse = handler.JSONResponse
    StreamingResponse = handler.StreamingResponse
    build_openai_tool_prompt = handler.build_openai_tool_prompt
    call_with_tool_prompt = handler.call_with_tool_prompt
    normalize_openai_messages = handler.normalize_openai_messages
    openai_text_response = handler.openai_text_response
    openai_text_sse = handler.openai_text_sse
    openai_tool_response = handler.openai_tool_response
    openai_tool_sse = handler.openai_tool_sse
    openai_tools_response = handler.openai_tools_response
    openai_tools_sse = handler.openai_tools_sse
    parse_tool_calls = handler.parse_tool_calls
    record_outcome = handler.record_outcome
    strip_thinking = handler.strip_thinking
    time = handler.time

    allow_parallel = body.get("parallel_tool_calls", True) is not False
    # v4.1.1 — co-emulation: reasoning-prefix the tool prompt when
    # CoT-E is also engaged so tools + reasoning are served together.
    tool_prompt = build_openai_tool_prompt(
        tools or [],
        allow_parallel=allow_parallel,
        with_reasoning=route.cot_engaged,
    )
    norm_msgs = normalize_openai_messages(messages_list)
    if norm_msgs and norm_msgs[0]["role"] == "system":
        norm_msgs[0]["content"] = tool_prompt + "\n\n" + norm_msgs[0]["content"]
    else:
        norm_msgs = [{"role": "system", "content": tool_prompt}] + norm_msgs
    emul_extra = {k: v for k, v in extra.items() if k != "tools"}
    # v5.22.41 (BUG-095) — capture the upstream usage. Without this the
    # emulated-response builders below fill in zeros and every cost
    # derived from this request computes as $0.
    _emul_usage: dict = {}
    response_text = await call_with_tool_prompt(
        route.litellm_model, norm_msgs, None, emul_extra, usage_out=_emul_usage
    )
    tool_calls = parse_tool_calls(response_text)
    if route.cot_engaged:
        response_text = strip_thinking(response_text)
    # v3.8.3 (#263) — emit telemetry with a synthetic OpenAI-shape
    # response body so the meta extractor walks the same path it
    # walks for native callers on /v1/chat/completions.
    import json as _json

    _emul_resp_body = (
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": tc.get("name", ""),
                                    "arguments": _json.dumps(tc.get("input", {})),
                                }
                            }
                            for tc in tool_calls
                        ],
                    },
                }
            ],
        }
        if tool_calls
        else {"choices": [{"message": {"tool_calls": []}}]}
    )
    await record_outcome(
        db,
        route.provider.id,
        route.litellm_model,
        endpoint="completions",
        success=True,
        t0=time.monotonic(),
        key_record_id=key_record.id,
        response_body=_emul_resp_body,
        tool_call_format="emulated",
    )
    if not allow_parallel and len(tool_calls) > 1:
        tool_calls = tool_calls[:1]
    if tool_calls:
        resp_headers["X-Tool-Calls-Emitted"] = str(len(tool_calls))
    if stream:
        if len(tool_calls) >= 2:
            gen = openai_tools_sse(tool_calls)
        elif len(tool_calls) == 1:
            gen = openai_tool_sse(tool_calls[0]["name"], tool_calls[0]["input"])
        else:
            gen = openai_text_sse(response_text)
        return StreamingResponse(gen, media_type="text/event-stream", headers=resp_headers)
    else:
        if len(tool_calls) >= 2:
            content = openai_tools_response(
                tool_calls,
                route.litellm_model,
                usage=_emul_usage,
            )
        elif len(tool_calls) == 1:
            content = openai_tool_response(
                tool_calls[0]["name"],
                tool_calls[0]["input"],
                route.litellm_model,
                usage=_emul_usage,
            )
        else:
            content = openai_text_response(
                response_text,
                route.litellm_model,
                usage=_emul_usage,
            )
        # v3.6.1 — X-Quality-Hint for tool-emulation path
        from app.api._quality_hint import merge_into_headers

        merge_into_headers(resp_headers, content, endpoint="completions")
        return JSONResponse(content=content, headers=resp_headers)
