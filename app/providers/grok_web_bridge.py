"""grok-web bridge-mode dispatch — extracted from ``grok_web.py`` 2026-06-02.

First step of the manual/bridge axial split (the v3.10.9 "split along
the manual/bridge axis" target). The bridge-mode dispatch (``_bridge_chat``)
goes through the Playwright sidecar at the URL stored in
``Provider.extra_config.bridge_url``; the manual-mode dispatch (still in
``grok_web.py``) does a direct HTTP replay against grok.com using pasted
cookies. The two modes share constants + errors + body/header builders;
in v4.4.38 only the bridge dispatcher itself moved. A follow-up will
complete the package conversion if/when manual mode accumulates further
complexity.

Re-exported from ``app.providers.grok_web`` for back-compat — existing
test imports (``from app.providers.grok_web import _bridge_chat``) keep
working unchanged."""
from __future__ import annotations

import httpx


async def _bridge_chat(
    provider_extra_config: dict,
    messages: list[dict],
    model: str,
    stream: bool,
    timeout: float,
) -> dict:
    """Forward an OpenAI-shape body to the Playwright bridge sidecar.

    Bridge handles cookie maintenance + 401/403 retry-after-refresh; we
    just POST the structured request and return whatever the bridge
    returns. ``bridge_token`` from extra_config rides as ``X-Bridge-Token``.
    Stream support is pass-through-only for v1 — the bridge buffers the
    full NDJSON internally.
    """
    # Lazy import — avoids a circular at module load time. grok_web.py
    # re-exports _bridge_chat from this module, so the load order is
    # grok_web → grok_web_bridge → grok_web (errors), which Python's
    # import system handles fine when the inner import happens at call
    # time rather than module-eval time.
    from app.providers.grok_web import (
        GrokWebError, GrokWebAuthError,
        _map_upstream_status, _pick_conversation_id,
    )
    bridge_url = provider_extra_config["bridge_url"].rstrip("/")
    conv_id = _pick_conversation_id(provider_extra_config)
    statsig_id = provider_extra_config.get("x_statsig_id") or None
    bridge_token = provider_extra_config.get("bridge_token") or ""
    body = {
        "messages": messages,
        "model": model,
        "conversation_id": conv_id,
        "stream": stream,
    }
    if statsig_id:
        body["statsig_id"] = statsig_id
    headers = {"content-type": "application/json"}
    if bridge_token:
        headers["x-bridge-token"] = bridge_token
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            r = await client.post(f"{bridge_url}/api/chat", json=body, headers=headers)
        except httpx.TimeoutException as e:
            # 2026-08-18: split from the generic handler below. httpx.TimeoutException
            # subclasses TransportError -> HTTPError, so a SLOW bridge used to be
            # reported as "unreachable" — identical wording to a dead host. That cost
            # a full debugging session chasing DNS/nginx/hairpin routing while the
            # bridge was healthy and merely answering slower than the timeout.
            # grok-web scrapes a real browser: 30-50s replies are normal, not an outage.
            raise GrokWebError(
                f"grok-web bridge timed out after {timeout}s (the bridge was reachable; "
                f"grok.com had not finished replying). This is a latency problem, not a "
                f"connectivity one — raise the provider timeout or BRIDGE_APPEAR_TIMEOUT_SEC "
                f"before investigating the network. Underlying: {e!r}"
            )
        except httpx.HTTPError as e:
            raise GrokWebError(f"grok-web bridge unreachable: {e}")
    if r.status_code == 401:
        raise GrokWebAuthError(
            f"grok-web bridge auth: {r.text[:200]}. The bridge's "
            "Playwright session may need re-login — open the bridge "
            "/login page in a browser and sign in to grok.com again."
        )
    if r.status_code != 200:
        raise GrokWebError(
            f"grok-web bridge {r.status_code}: {r.text[:200]}",
            status_code=_map_upstream_status(r.status_code),
        )
    return r.json()
