"""Completion response dispatch; dependencies remain patchable at the endpoint."""


async def dispatch_stream(
    *,
    route,
    hint,
    x_hedge,
    db,
    has_tools,
    has_images,
    key_record,
    messages_list,
    extra,
    budget_total,
    cache_decision,
    _compliance_disclosure,
    _compliance_wants_sse_prelude,
    tools,
    body,
    resp_headers,
    alias,
    is_auto,
    requested_model,
):
    from app.api import completions as handler

    HTTPException = handler.HTTPException
    StreamingResponse = handler.StreamingResponse
    _cfg_settings = handler._cfg_settings
    _stream_openai = handler._stream_openai
    buffer_sse_until_content = handler.buffer_sse_until_content
    clamp_thinking_budget = handler.clamp_thinking_budget
    http_status_for_stream_error = handler.http_status_for_stream_error
    logger = handler.logger
    observe_hedge_attempt = handler.observe_hedge_attempt
    observe_hedge_bucket_reject = handler.observe_hedge_bucket_reject
    observe_hedge_win = handler.observe_hedge_win
    preflight_sse = handler.preflight_sse
    race_streams = handler.race_streams
    select_provider = handler.select_provider
    should_hedge_header = handler.should_hedge_header
    stream_with_empty_guard = handler.stream_with_empty_guard
    time = handler.time
    try_acquire_hedge = handler.try_acquire_hedge
    wait_budget_ms = handler.wait_budget_ms

    lmrh_hedge = hint.get("hedge").value if (hint and hint.get("hedge")) else None
    wants_hedge = _cfg_settings.hedge_enabled and should_hedge_header(x_hedge, lmrh_hedge)
    wait_ms = wait_budget_ms(route.provider.id) if wants_hedge else None

    if wait_ms is not None and await try_acquire_hedge():
        try:
            backup_route = await select_provider(
                db,
                hint,
                has_tools=has_tools,
                has_images=has_images,
                key_type=key_record.key_type,
                exclude_provider_id=route.provider.id,
                excluded_provider_types={"claude-oauth"},
            )
        except Exception:
            backup_route = None
        # v5.22.4 — release the connection this backup re-select opened
        # before the hedged streams run (K1, symmetric to messages.py).
        try:
            await db.commit()
        except Exception:
            pass

        if backup_route is not None:
            observe_hedge_attempt(route.provider.id, backup_route.provider.id)

            def _primary():
                return _stream_openai(
                    route.litellm_model,
                    messages_list,
                    extra,
                    route.provider.id,
                    db,
                    key_record.id,
                    time.monotonic(),
                    budget_total,
                    cache_decision=cache_decision,
                    compliance_disclosure=_compliance_disclosure,
                    accept_compliance_events=_compliance_wants_sse_prelude,
                )

            def _backup():
                b_extra = {**backup_route.litellm_kwargs}
                if tools:
                    b_extra["tools"] = tools
                if body.get("max_tokens"):
                    b_extra["max_tokens"] = body["max_tokens"]
                if body.get("temperature") is not None:
                    b_extra["temperature"] = body["temperature"]
                if backup_route.native_thinking_params:
                    b_extra.update(backup_route.native_thinking_params)
                    clamp_thinking_budget(b_extra)
                return _stream_openai(
                    backup_route.litellm_model,
                    messages_list,
                    b_extra,
                    backup_route.provider.id,
                    db,
                    key_record.id,
                    time.monotonic(),
                    budget_total,
                    cache_decision=None,
                    compliance_disclosure=_compliance_disclosure,
                    accept_compliance_events=_compliance_wants_sse_prelude,
                )

            racer, winner = await race_streams(_primary, _backup, wait_ms)
            observe_hedge_win(winner)
            resp_headers["X-Hedged-Winner"] = winner
            # v3.10.16 BUG-001 — pre-flight the hedged stream so a
            # pre-stream upstream failure on the winning branch
            # surfaces as a real HTTP status, not a 200 + SSE
            # error frame (parity with the non-hedged path).
            _hfirst, _herr, racer = await preflight_sse(racer)
            if _herr is not None:
                await racer.aclose()
                raise HTTPException(
                    http_status_for_stream_error(_herr),
                    f"Upstream error before streaming began: {_herr}",
                )

            # v5.3.9 — empty-success guard on the hedged winner.
            # On a content-free 200 stream: record the breaker
            # failure and fall through to the guarded non-hedged
            # path below (which fails over across providers).
            _hframes, _h_has_content, racer = await buffer_sse_until_content(_hfirst, racer)
            if _h_has_content:

                async def _replay_hedged_stream(_fs=_hframes, _g=racer):
                    for _f in _fs:
                        yield _f
                    async for _c in _g:
                        yield _c

                return StreamingResponse(
                    _replay_hedged_stream(),
                    media_type="text/event-stream",
                    headers=resp_headers,
                )
            await racer.aclose()
            from app.routing.circuit_breaker import record_failure as _rec_fail

            _dead_id = route.provider.id if winner == "primary" else backup_route.provider.id
            await _rec_fail(_dead_id, billing_error=False)
            logger.warning(
                "hedged stream empty-success (winner=%s provider=%s) — "
                "falling through to guarded non-hedged path",
                winner,
                _dead_id,
            )
    elif wait_ms is not None:
        observe_hedge_bucket_reject()

    # v3.10.13 BUG-001 — pre-flight so a pre-stream upstream
    # failure surfaces as a real HTTP status, not a 200 + a
    # terminal SSE error frame. (Mirrors the /v1/messages path.)
    # v5.3.9 — wrapped in stream_with_empty_guard: an upstream that
    # answers 200 + a content-free SSE stream (observed: dead
    # cursor-bridge sessions — 0 output tokens on 400+ requests)
    # now records a breaker failure and fails over to the next
    # provider instead of piping the emptiness to the caller.
    def _start_openai_stream(_r):
        if _r is route:
            _e, _cd = extra, cache_decision
        else:
            _e = {**_r.litellm_kwargs}
            if tools:
                _e["tools"] = tools
            if body.get("max_tokens"):
                _e["max_tokens"] = body["max_tokens"]
            if body.get("temperature") is not None:
                _e["temperature"] = body["temperature"]
            if _r.native_thinking_params:
                _e.update(_r.native_thinking_params)
                clamp_thinking_budget(_e)
            _cd = None  # don't store failover output under primary's key
        return _stream_openai(
            _r.litellm_model,
            messages_list,
            _e,
            _r.provider.id,
            db,
            key_record.id,
            time.monotonic(),
            budget_total,
            cache_decision=_cd,
            compliance_disclosure=_compliance_disclosure,
            accept_compliance_events=_compliance_wants_sse_prelude,
        )

    from app.routing.aliases import is_logical_alias as _is_logical_alias

    _frames, _gen, _served_route = await stream_with_empty_guard(
        start_stream=_start_openai_stream,
        route=route,
        db=db,
        hint=hint,
        has_tools=has_tools,
        has_images=has_images,
        key_type=key_record.key_type,
        api_key_id=key_record.id,
        model_override=None
        if (is_auto or (alias is None and _is_logical_alias(requested_model)))
        else requested_model,
    )
    if _served_route is not route:
        resp_headers["X-Empty-Stream-Failover"] = "true"
        resp_headers["X-Empty-Stream-Failover-Target"] = _served_route.provider.provider_type

    async def _replay_openai_stream(_fs=_frames, _g=_gen):
        for _f in _fs:
            yield _f
        async for _c in _g:
            yield _c

    return StreamingResponse(
        _replay_openai_stream(),
        media_type="text/event-stream",
        headers=resp_headers,
    )
