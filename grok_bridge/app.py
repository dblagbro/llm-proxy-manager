"""
grok-bridge — Playwright-backed sidecar for llm-proxy2 grok-web provider.

Architecture:
    [llm-proxy2 grok-web dispatcher] → POST /api/chat → [bridge]
        → if cookies fresh: HTTP replay against grok.com
        → if 401/403:        Playwright page.reload() → wait → retry once
        → return OpenAI-shape JSON

Boot:
    Xvfb + x11vnc + noVNC come up via supervisord (start.sh).
    FastAPI launches Chromium with launch_persistent_context() pointed at
    /data/playwright-state — survives container restarts. Operator signs
    in once via /vnc/ noVNC tab; from then on cookies refresh passively
    every time the background loop touches a grok.com page.

Endpoints:
    GET  /healthz           — alive check (no auth)
    GET  /api/status        — login state, cookie freshness, last refresh
    POST /api/login/start   — navigates the Playwright tab to grok.com so
                              the operator's noVNC session sees a login form
    POST /api/login/save    — persists the current state to state.json
    POST /api/chat          — bridge-as-proxy: takes an OpenAI-shape body,
                              executes against grok.com, returns response.
                              Authenticated via X-Bridge-Token header.
    /vnc/                   — proxies to websockify@6080; serves the live
                              noVNC viewer for the running Chromium
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from playwright.async_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)


logger = logging.getLogger("grok-bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATE_DIR = Path(os.environ.get("BRIDGE_STATE_DIR", "/data/playwright-state"))
BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "").strip()
# When the bridge is reached through nginx at e.g. /grok-bridge/, set this
# to ``/grok-bridge`` so the /login HTML emits absolute URLs that nginx
# will route correctly. Empty for direct-port deployments.
BRIDGE_PUBLIC_PATH = os.environ.get("BRIDGE_PUBLIC_PATH", "").rstrip("/")
GROK_BASE = "https://grok.com"
NOVNC_PORT = 6080
COOKIE_REFRESH_INTERVAL_SEC = 25 * 60   # well under __cf_bm's 30-min lifetime
INFERENCE_RETRY_AFTER_REFRESH = True
DEFAULT_MODE_ID = "fast"

# v3.3.3 (bridge): cool-off after a 429 from grok.com. When grok.com
# rate-limits us, hammering again within seconds just guarantees more
# 429s and burns the proxy's outer timeout budget. Cache the timestamp;
# subsequent /api/chat calls within the cool-off window short-circuit
# with a synthetic 429 instead of round-tripping. Affects both probes
# and real user calls — if grok-side is throttling NOW, the user
# request is going to 429 anyway, and falling through to the next
# provider via the proxy router is faster than waiting for grok.com
# to refuse us a second time.
GROK_429_COOLDOWN_SEC = int(os.environ.get("GROK_429_COOLDOWN_SEC", "60"))


# ── Globals — single live Chromium per container ────────────────────────
_playwright: Optional[Playwright] = None
_context: Optional[BrowserContext] = None
_page: Optional[Page] = None
_lock = asyncio.Lock()
_last_refresh_at: float = 0.0
_last_refresh_status: str = "never"
_last_login_url: str = ""
# v3.3.3: 429 cool-off state. Set when _post_to_grok observes 429 in
# grok.com's HTTP status; cleared by elapsed time. Read by /api/chat
# before issuing a new request.
_last_429_at: float = 0.0
_last_429_body: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot Playwright on startup; close cleanly on shutdown."""
    global _playwright, _context, _page
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.info("starting playwright with state_dir=%s", STATE_DIR)
    _playwright = await async_playwright().start()
    # launch_persistent_context keeps cookies + localStorage on disk so a
    # container restart doesn't lose the login. Headed mode + Xvfb display
    # so the operator's noVNC tab sees the actual browser.
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(STATE_DIR),
        headless=False,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",  # less obvious
            "--disable-dev-shm-usage",
            "--start-maximized",
            # Force the window to fill the Xvfb display so the operator's
            # noVNC view shows the full grok.com layout (sign-in buttons
            # live on the right edge — viewport-truncation hides them).
            "--window-position=0,0",
            "--window-size=1920,1080",
        ],
        # No fixed viewport: let the window size match the Xvfb display
        # (1920x1080). Playwright defaults to 1280x720 if viewport is
        # explicit, which causes right-side clipping on wide layouts.
        no_viewport=True,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
    )
    # Reuse the first page that the persistent context spawns; if none,
    # open a fresh blank one.
    pages = _context.pages
    _page = pages[0] if pages else await _context.new_page()
    # Boot directly to grok.com so the operator's noVNC view shows
    # something useful immediately. The persistent state under
    # /data/playwright-state preserves the signed-in session across
    # container restarts; landing on grok.com causes Cloudflare to
    # validate cookies passively (refreshing them if near expiry).
    try:
        await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=20_000)
    except Exception as e:
        logger.warning("startup navigation to grok.com failed: %s", e)
    # Kick off the cookie-refresh background task.
    refresh_task = asyncio.create_task(_cookie_refresh_loop())
    logger.info("playwright ready; bridge listening")
    try:
        yield
    finally:
        refresh_task.cancel()
        try:
            await refresh_task
        except asyncio.CancelledError:
            pass
        if _context:
            await _context.close()
        if _playwright:
            await _playwright.stop()


app = FastAPI(title="grok-bridge", version="1.0.0", lifespan=lifespan)


# ── Auth dependency ─────────────────────────────────────────────────────
def require_bridge_token(x_bridge_token: Optional[str] = Header(default=None)) -> None:
    """Internal endpoints use a shared HMAC-style token. Empty BRIDGE_TOKEN
    disables the check (dev only). llm-proxy2 sends ``X-Bridge-Token`` on
    every /api/chat call; nginx terminates TLS so the token rides over a
    plaintext internal hop."""
    if not BRIDGE_TOKEN:
        return
    if x_bridge_token != BRIDGE_TOKEN:
        raise HTTPException(401, "invalid bridge token")


# ── Cookie / header capture ──────────────────────────────────────────────
async def _cookies_dict() -> dict[str, str]:
    """Snapshot the live BrowserContext cookies as a name→value map.

    Only covers cookies for grok.com hosts so we don't leak unrelated
    cookies from any other tab the operator may have open.
    """
    if _context is None:
        return {}
    cookies = await _context.cookies(["https://grok.com", "https://www.grok.com"])
    out: dict[str, str] = {}
    for c in cookies:
        name = c.get("name")
        val = c.get("value")
        if name and val is not None:
            out[name] = val
    return out


async def _capture_request_headers() -> dict[str, str]:
    """Capture the headers a real grok.com request would carry by
    intercepting one inflight call. Cheaper alternative: read directly
    from the page's runtime via JS evaluation.

    For the v1 we hard-code the headers we know matter (user-agent,
    sec-ch-ua-*) and pull x-statsig-id by intercepting one /responses
    request via Playwright's request listener.
    """
    # Headers we always need — the rest is statsig-id which we observe
    # on the wire.
    base = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "origin": GROK_BASE,
        "sec-ch-ua": '"Google Chrome";v="147", "Not.A/Brand";v="8", "Chromium";v="147"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/147.0.0.0 Safari/537.36"
        ),
    }
    return base


async def _capture_statsig_id(timeout_sec: float = 10.0) -> Optional[str]:
    """Listen for the next outgoing grok.com /responses request and pull
    its ``x-statsig-id`` header. We trigger a request by sending a tiny
    ping message via the bridge's own machinery if no organic traffic is
    in flight — but simplest is to just navigate the page once and watch
    for any request to grok.com that carries the header.
    """
    if _page is None:
        return None
    found: dict[str, str] = {}
    fut: asyncio.Future[Optional[str]] = asyncio.get_event_loop().create_future()

    def _on_request(req):  # type: ignore[no-untyped-def]
        try:
            url = req.url
            if "grok.com" in url and req.headers.get("x-statsig-id"):
                if not fut.done():
                    fut.set_result(req.headers.get("x-statsig-id"))
        except Exception:
            pass

    _page.on("request", _on_request)
    try:
        # Kick a navigation to provoke a request burst.
        try:
            await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=timeout_sec * 1000)
        except PlaywrightTimeout:
            pass
        try:
            return await asyncio.wait_for(fut, timeout=timeout_sec)
        except asyncio.TimeoutError:
            return None
    finally:
        try:
            _page.remove_listener("request", _on_request)
        except Exception:
            pass


# ── Background cookie refresh ───────────────────────────────────────────
async def _cookie_refresh_loop():
    """Periodically poke grok.com so __cf_bm and cf_clearance stay fresh.

    We don't care about the response — visiting any grok.com page makes
    Cloudflare reissue the cookies if they're nearing expiry. Runs every
    25 minutes (under __cf_bm's 30-min lifetime).
    """
    global _last_refresh_at, _last_refresh_status
    while True:
        try:
            await asyncio.sleep(COOKIE_REFRESH_INTERVAL_SEC)
            if _page is None:
                continue
            try:
                await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=20_000)
                _last_refresh_at = time.time()
                _last_refresh_status = "ok"
                logger.info("cookie-refresh tick: %s", _last_refresh_status)
            except Exception as e:
                _last_refresh_status = f"error: {e}"
                logger.warning("cookie-refresh failed: %s", e)
        except asyncio.CancelledError:
            return


async def _force_refresh() -> bool:
    """On 401/403 we kick the page to re-up Cloudflare cookies. Returns
    True if the refresh succeeded (page navigated, no exception)."""
    global _last_refresh_at, _last_refresh_status
    if _page is None:
        return False
    try:
        await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=20_000)
        _last_refresh_at = time.time()
        _last_refresh_status = "force-refresh-ok"
        return True
    except Exception as e:
        _last_refresh_status = f"force-refresh-error: {e}"
        logger.warning("force refresh failed: %s", e)
        return False


# ── Health + status ──────────────────────────────────────────────────────
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "version": "1.0.0", "ts": int(time.time())}


def _conv_id_from_url(url: Optional[str]) -> Optional[str]:
    """Extract the conversation UUID from a grok.com URL.

    Matches ``grok.com/c/<uuid>`` and ``www.grok.com/c/<uuid>``. Anything
    else (e.g. ``grok.com/`` root, OAuth redirects, login pages) returns
    None so the wizard knows the bridge isn't currently sitting on a
    valid conversation tab.
    """
    if not url:
        return None
    import re as _re
    m = _re.search(r"grok\.com/c/([0-9a-f-]{32,40})", url)
    return m.group(1) if m else None


@app.get("/api/status")
async def status():
    """Public — exposes login state + cookie freshness so the operator-
    facing /login page can render without leaking BRIDGE_TOKEN to the
    browser. Doesn't return any sensitive values (no cookie contents,
    no statsig-id), just booleans and a count."""
    cookies = await _cookies_dict()
    needed = ["cf_clearance", "__cf_bm", "sso", "x-userid"]
    present = {k: (k in cookies) for k in needed}
    cur_url = _page.url if _page is not None else None
    # v3.3.3: surface the 429 cool-off state so the operator can see at
    # a glance whether the bridge is currently short-circuiting due to
    # grok.com rate-limit pressure (vs e.g. genuinely down).
    cooldown_remaining = 0
    if GROK_429_COOLDOWN_SEC > 0 and _last_429_at > 0:
        elapsed = time.time() - _last_429_at
        if elapsed < GROK_429_COOLDOWN_SEC:
            cooldown_remaining = int(GROK_429_COOLDOWN_SEC - elapsed)
    return {
        "logged_in": all(present[k] for k in ("sso", "x-userid")),
        "cookies_present": present,
        "cookie_count": len(cookies),
        "last_refresh_at": _last_refresh_at,
        "last_refresh_status": _last_refresh_status,
        "url": cur_url,
        "current_conversation_id": _conv_id_from_url(cur_url),
        "vnc_url": "/vnc/vnc.html?path=vnc/websockify&autoconnect=true&resize=remote",
        "rate_limit_429": {
            "last_429_at": _last_429_at if _last_429_at > 0 else None,
            "cooldown_remaining_sec": cooldown_remaining,
            "cooldown_active": cooldown_remaining > 0,
        },
    }


@app.post("/api/conversation/new")
async def create_new_conversation():
    """Create a fresh conversation in the operator's grok.com account
    and return its UUID. v1.1.0 (2026-05-09): drives the SPA itself
    rather than relying on a server-side POST /conversations/new
    (which Cloudflare anti-bot blocks from server IPs even with
    valid cookies).

    Strategy: in the bridge's logged-in Chromium, send a one-token
    "hi" message to grok.com via the page's own ``fetch()`` so the
    request rides the real browser TLS/UA fingerprint that Cloudflare
    has already cleared. We hit the same ``/responses`` endpoint the
    SPA uses for normal chat — passing an empty conversation_id or
    a special marker produces a fresh conversation, depending on what
    the SPA itself does. If that fails, fall back to UI automation:
    type into the textarea + press Enter, wait for URL to navigate
    to /c/<uuid>.

    Returns:
        {"conversation_id": "<uuid>" | null, "method": "..."}
    """
    if _page is None:
        raise HTTPException(503, "playwright not ready")

    async with _lock:
        # Always start from grok.com root so we know URL state is
        # predictable. If we're already on /c/<uuid>, navigate away
        # so we don't accidentally claim that conversation as "new".
        try:
            await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=20_000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(1.5)  # let SPA hydrate

        # ── Strategy 0: direct API call (2026-06-05 fix) ────────────────
        # Use the same shape chat() does, but POST to /conversations/new
        # with a real first message + the full feature-flag payload.
        # This is what the SPA itself does when the user clicks "New
        # chat" + sends the first message; we just skip the SPA UI
        # driving entirely.
        try:
            statsig = await _capture_statsig_id(timeout_sec=6.0)
        except Exception:
            statsig = None

        try:
            cookies = await _cookies_dict()
            cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
            req_headers = await _capture_request_headers()
            if statsig:
                req_headers["x-statsig-id"] = statsig
            req_headers["x-xai-request-id"] = str(uuid.uuid4())
            if "x-userid" in cookies:
                req_headers["x-userid"] = cookies["x-userid"]
            req_headers["referer"] = f"{GROK_BASE}/"
            req_headers["cookie"] = cookie_str
            create_url = f"{GROK_BASE}/rest/app-chat/conversations/new"
            create_body = _build_grok_body("hi", "MODE_FAST")
            async with httpx.AsyncClient(timeout=45.0) as client:
                cresp = await client.post(create_url, json=create_body, headers=req_headers)
            logger.info("conversation/new direct API status=%d (statsig=%s)",
                        cresp.status_code, "yes" if statsig else "no")
            if cresp.status_code in (200, 201):
                # The response is streaming NDJSON (the first response
                # chunk(s) of the new conversation). The conversation_id
                # is in the metadata fields of the first chunks. Parse
                # only the first ~5 chunks for it.
                cid: Optional[str] = None
                body_text = cresp.text
                for line in body_text.splitlines()[:20]:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except Exception:
                        continue
                    # Walk common location shapes for the conversation id.
                    def _walk(obj):
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k in ("conversationId", "conversation_id") and isinstance(v, str) and len(v) >= 32:
                                    return v
                                found = _walk(v)
                                if found:
                                    return found
                        elif isinstance(obj, list):
                            for item in obj:
                                found = _walk(item)
                                if found:
                                    return found
                        return None
                    cid = _walk(chunk)
                    if cid:
                        break
                if cid:
                    return {
                        "conversation_id": cid,
                        "method": "direct_api",
                        "url": _page.url,
                    }
                # 200 but couldn't parse — log a preview for the next
                # diagnostic round.
                logger.info("direct API 200 but no conversation_id found; body preview=%s",
                            body_text[:400])
            else:
                logger.info("direct API non-2xx (%d); body preview=%s",
                            cresp.status_code, cresp.text[:300])
        except Exception as e:
            logger.warning("direct API attempt failed: %s", e)

        # ── Strategy 1 REMOVED 2026-06-05 (operator-blocking) ───────────
        # Pre-fix, Strategy 1 did:
        #     POST https://grok.com/rest/app-chat/conversations/new
        #         body: "{}"
        # Once the statsig-id capture got us past the bot-detection 403,
        # the same call started returning HTTP 400 with body:
        #     {"error":{"code":3,"message":"Cannot generate response to
        #      empty conversation."}}
        # i.e. Grok's API contract now wants the create call to include
        # the first message. The bridge no longer knows the exact shape
        # (operator's DevTools capture is the proper fix path). Until
        # the SPA's request shape is known, Strategy 1 always fails;
        # skipping it removes a 5-8 second wasted detour and gets us
        # to UI-send faster.

        # ── Strategy 2: UI automation with network observation ───────────
        # Type a tiny message via the SPA's own textarea + click the Send
        # button (not Enter — grok's SPA no longer reacts to Enter as of
        # 2026-06-05). The SPA fires its own POST /responses with the
        # full statsig + auth shape; we watch the request listener for
        # that POST to capture the auto-generated conversation_id from
        # the URL path.
        try:
            try:
                await _page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass

            # Set up a request observer BEFORE typing. We want to know
            # if the SPA fires its POST at all — that disambiguates
            # "send button never clicked" from "send was clicked but
            # request failed silently".
            observed: dict = {"requests": [], "responses": []}

            def _obs_req(req):
                try:
                    if "grok.com" in req.url and req.method == "POST":
                        observed["requests"].append({
                            "url": req.url,
                            "method": req.method,
                        })
                except Exception:
                    pass

            def _obs_resp(resp):
                try:
                    if "grok.com" in resp.url and resp.request.method == "POST":
                        observed["responses"].append({
                            "url": resp.url,
                            "status": resp.status,
                        })
                except Exception:
                    pass

            _page.on("request", _obs_req)
            _page.on("response", _obs_resp)

            tried_textareas: list[str] = []
            typed_in: Optional[str] = None
            text_landed: bool = False
            for selector in (
                'textarea[placeholder*="What"]',
                'textarea[placeholder*="Ask"]',
                'textarea[aria-label*="prompt" i]',
                'textarea[aria-label*="message" i]',
                'textarea[name="prompt"]',
                'textarea',
                'div[contenteditable="true"]',
            ):
                tried_textareas.append(selector)
                try:
                    loc = _page.locator(selector).first
                    await loc.wait_for(state="visible", timeout=3_000)
                    await loc.click()
                    # Use Playwright's insertText — dispatches a single
                    # composition event that React's controlled-input
                    # state actually picks up (loc.type() fires raw
                    # keystrokes that React often misses on
                    # contenteditable / React-controlled inputs).
                    try:
                        await _page.keyboard.insert_text("hi")
                    except Exception:
                        # Fallback to type() if insert_text isn't supported
                        await loc.type("hi", delay=30)
                    typed_in = selector
                    logger.info("UI-send: inserted text into selector=%s", selector)

                    # Verify React state actually has the text — read the
                    # input back via JS. If empty, the controlled state
                    # didn't update and Send will refuse to fire.
                    try:
                        verify = await _page.evaluate(
                            """(sel) => {
                                const el = document.querySelector(sel);
                                if (!el) return null;
                                if ('value' in el && typeof el.value === 'string') return el.value;
                                return el.textContent || el.innerText || '';
                            }""",
                            selector,
                        )
                        logger.info("UI-send: post-type verify content=%r", (verify or "")[:30])
                        text_landed = bool((verify or "").strip())
                    except Exception:
                        pass

                    # If verify shows empty, force a React-aware update:
                    # set value/textContent + dispatch a native input event
                    # that React's synthetic event system listens for.
                    if not text_landed:
                        try:
                            await _page.evaluate(
                                """(sel) => {
                                    const el = document.querySelector(sel);
                                    if (!el) return;
                                    const text = 'hi';
                                    if ('value' in el) {
                                        // For textarea — use the native value setter
                                        // so React picks up the change.
                                        const setter = Object.getOwnPropertyDescriptor(
                                            window.HTMLTextAreaElement.prototype, 'value'
                                        ).set;
                                        setter.call(el, text);
                                    } else {
                                        // contenteditable div — set textContent
                                        el.textContent = text;
                                    }
                                    el.dispatchEvent(new InputEvent('input', {
                                        inputType: 'insertText',
                                        data: text,
                                        bubbles: true,
                                        cancelable: false,
                                    }));
                                }""",
                                selector,
                            )
                            logger.info("UI-send: forced React state via dispatchEvent")
                            text_landed = True
                        except Exception as e:
                            logger.warning("force-input failed: %s", e)

                    break
                except (PlaywrightTimeout, Exception) as e:
                    logger.debug("textarea selector %s failed: %s", selector, str(e)[:120])
                    continue

            if typed_in is None:
                try:
                    _page.remove_listener("request", _obs_req)
                    _page.remove_listener("response", _obs_resp)
                except Exception:
                    pass
                return {
                    "conversation_id": None,
                    "method": "ui_failed_typing",
                    "tried_selectors": tried_textareas,
                    "hint": "no usable textarea found on grok.com — open noVNC to inspect",
                }

            # Find + click the Send button.
            # 2026-06-05 — confirmed via button-inventory dump on the
            # current grok.com SPA: the actual send button is
            # ``data-testid="chat-submit"`` (type=submit, with SVG icon).
            #
            # Click strategy: Playwright's loc.click() synthesizes
            # `isTrusted=false` events which Grok's bot detection
            # ignores at the React onClick handler level. We bypass
            # that by walking the React fiber to invoke onClick
            # directly — bypasses isTrusted entirely because we're
            # calling the handler in JS land. Falls back to native
            # button.click() and Playwright click if the fiber walk
            # fails.
            tried_buttons: list[str] = []
            clicked_button: Optional[str] = None
            selectors = (
                'button[data-testid="chat-submit"]:not([disabled])',
                'button[data-testid*="submit" i]:not([disabled])',
                'button[data-testid*="send" i]:not([disabled])',
                'button[aria-label*="Send" i]:not([disabled])',
                'button[type="submit"]:not([disabled])',
                'form button[type="submit"]:not([disabled])',
                'div:has(textarea) button:has(svg):not([disabled])',
                'div:has([contenteditable="true"]) button:has(svg):not([disabled])',
            )

            for sel in selectors:
                tried_buttons.append(sel)
                try:
                    # Try React-fiber direct invocation FIRST.
                    clicked_via = await _page.evaluate(
                        """(sel) => {
                            const btn = document.querySelector(sel);
                            if (!btn) return null;
                            // React stores its fiber under a key like
                            // __reactFiber$... and props under __reactProps$...
                            const propsKey = Object.keys(btn).find(k =>
                                k.startsWith('__reactProps')
                            );
                            if (propsKey) {
                                const props = btn[propsKey];
                                if (props && typeof props.onClick === 'function') {
                                    try {
                                        // Synthetic event with the methods
                                        // most React onClick handlers expect.
                                        props.onClick({
                                            preventDefault: () => {},
                                            stopPropagation: () => {},
                                            currentTarget: btn,
                                            target: btn,
                                            type: 'click',
                                            isTrusted: true,
                                            nativeEvent: new MouseEvent('click', {bubbles: true}),
                                        });
                                        return 'react_fiber';
                                    } catch (e) { /* fall through */ }
                                }
                            }
                            // Native click — fires DOM event with
                            // isTrusted=false but at least invokes
                            // any native form submission logic.
                            try { btn.click(); return 'native_click'; }
                            catch (e) { return null; }
                        }""",
                        sel,
                    )
                    if clicked_via:
                        clicked_button = f"{sel} (via {clicked_via})"
                        logger.info("UI-send: clicked via %s on selector=%s", clicked_via, sel)
                        break
                    # Last resort: Playwright's click.
                    btn = _page.locator(sel).first
                    await btn.wait_for(state="visible", timeout=2_000)
                    await btn.click()
                    clicked_button = f"{sel} (via playwright_click)"
                    logger.info("UI-send: clicked via playwright on selector=%s", sel)
                    break
                except Exception as e:
                    logger.debug("button selector %s failed: %s", sel, str(e)[:120])
                    continue

            if clicked_button is None:
                try:
                    await _page.keyboard.press("Enter")
                    logger.info("UI-send: pressed Enter (no Send button found)")
                except Exception as e:
                    logger.warning("UI-send: Enter fallback failed: %s", e)

            # Poll for /c/<uuid> URL change, while collecting network
            # observations. Up to 25s.
            deadline = time.time() + 25.0
            cid: Optional[str] = None
            while time.time() < deadline:
                cid = _conv_id_from_url(_page.url)
                if cid:
                    break
                # Also check the observed responses — the POST URL itself
                # may carry the conversation_id even if window.location
                # hasn't navigated yet.
                for r in observed["responses"]:
                    inline = _conv_id_from_url(r["url"])
                    if inline:
                        cid = inline
                        break
                if cid:
                    break
                try:
                    live_url = await _page.evaluate("window.location.href")
                    cid = _conv_id_from_url(live_url)
                    if cid:
                        break
                except Exception:
                    pass
                await asyncio.sleep(0.4)

            try:
                _page.remove_listener("request", _obs_req)
                _page.remove_listener("response", _obs_resp)
            except Exception:
                pass

            # On success: clean return. On failure: include network
            # observations + selector attempts so the operator can
            # diagnose with one log line.
            if cid:
                return {
                    "conversation_id": cid,
                    "method": "ui_send",
                    "url": _page.url,
                    "typed_via": typed_in,
                    "clicked": clicked_button,
                    "observed_post_count": len(observed["responses"]),
                }

            # On timeout: dump every visible button on the page so we
            # can see what the actual send button looks like. Speeds
            # up the next diagnostic round considerably.
            button_inventory: list[dict] = []
            try:
                button_inventory = await _page.evaluate("""() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    return btns.slice(0, 30).map(b => ({
                        text: (b.textContent || '').trim().slice(0, 30),
                        aria: b.getAttribute('aria-label'),
                        type: b.getAttribute('type'),
                        testid: b.getAttribute('data-testid'),
                        title: b.getAttribute('title'),
                        disabled: b.disabled,
                        visible: !!(b.offsetWidth || b.offsetHeight),
                        rect_x: Math.round(b.getBoundingClientRect().x),
                        rect_y: Math.round(b.getBoundingClientRect().y),
                        has_svg: !!b.querySelector('svg'),
                    }));
                }""")
            except Exception:
                pass

            return {
                "conversation_id": None,
                "method": "ui_send_timed_out",
                "url": _page.url,
                "typed_via": typed_in,
                "clicked": clicked_button,
                "tried_buttons": tried_buttons,
                "observed_requests": observed["requests"][-8:],
                "observed_responses": observed["responses"][-8:],
                "button_inventory": [b for b in button_inventory if b.get("visible")],
                "hint": (
                    "Typed the message but no conversation_id appeared in 25s. "
                    "Inspect observed_responses: a POST to /responses with 200 "
                    "but no /c/<uuid> redirect means the SPA created a "
                    "conversation but didn't navigate. A POST 4xx/5xx means "
                    "the bridge's send-button click triggered the SPA's send "
                    "but the upstream call failed. Zero POSTs means the click "
                    "never reached the SPA's send handler — check "
                    "button_inventory for the actual send button selector."
                ),
            }
        except Exception as e:
            logger.warning("UI-send fallback failed: %s", e)
            return {
                "conversation_id": None,
                "method": "error",
                "error": str(e)[:300],
                "url": _page.url,
            }


# ── Login flow ───────────────────────────────────────────────────────────
@app.post("/api/login/start")
async def login_start():
    # Public — just navigates the embedded Chromium tab. The operator's
    # /login HTML calls this; protecting it would require shipping the
    # token to the browser. Side effect (a page navigation) isn't
    # sensitive on its own.
    """Navigate the Playwright tab to grok.com's login flow so the
    operator (watching via noVNC) can sign in via Google OAuth.
    """
    if _page is None:
        raise HTTPException(503, "playwright not ready")
    async with _lock:
        try:
            await _page.goto(GROK_BASE + "/", wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightTimeout:
            pass
    return {"status": "navigated", "url": _page.url}


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Tiny landing page that frames the noVNC viewer. Operator opens
    this in their llm-proxy2 admin tab; sees a live Chromium they can
    drive to sign in to grok.com.

    All URLs are emitted as ``{BRIDGE_PUBLIC_PATH}/...`` so nginx routing
    under /grok-bridge/ resolves correctly. Local-port deployments just
    set BRIDGE_PUBLIC_PATH="" and the URLs become root-relative.
    """
    p = BRIDGE_PUBLIC_PATH  # "" or "/grok-bridge"
    vnc_path = f"{p}/vnc/vnc.html"
    vnc_ws_path = f"{p[1:]}/vnc/websockify" if p else "vnc/websockify"
    api_status = f"{p}/api/status"
    api_login_start = f"{p}/api/login/start"
    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Connect Grok — llm-proxy2</title>
  <style>
    html,body {{ margin:0; padding:0; height:100%; background:#0f0f10; color:#e6e6e6;
                 font: 14px/1.5 -apple-system, system-ui, sans-serif; }}
    header {{ padding: 10px 16px; background:#191920; display:flex; align-items:center; gap:12px; }}
    header h1 {{ font-size:14px; margin:0; font-weight:600; }}
    header .actions {{ margin-left:auto; display:flex; gap:8px; }}
    button {{ background:#3b82f6; color:white; border:0; border-radius:4px;
              padding:6px 12px; cursor:pointer; font-size:12px; }}
    button.secondary {{ background:#374151; }}
    iframe {{ width:100%; height:calc(100vh - 44px); border:0; background:#000; }}
    #status {{ font-size:12px; color:#9ca3af; }}
  </style>
</head><body>
  <header>
    <h1>Grok bridge — sign in via Google OAuth, then click "Done"</h1>
    <span id="status">checking session…</span>
    <div class="actions">
      <button class="secondary" onclick="goGrok()">Open grok.com</button>
      <button onclick="checkStatus()">Refresh</button>
      <button onclick="markDone()">Done</button>
    </div>
  </header>
  <iframe src="{vnc_path}?path={vnc_ws_path}&autoconnect=true&resize=scale&view_only=false"></iframe>
  <script>
    const STATUS_URL = "{api_status}";
    const LOGIN_START_URL = "{api_login_start}";
    async function checkStatus() {{
      const r = await fetch(STATUS_URL);
      const j = await r.json();
      document.getElementById('status').textContent =
        (j.logged_in ? 'signed in' : 'not signed in')
        + ' · ' + j.cookie_count + ' cookies · last refresh ' +
        (j.last_refresh_at ? new Date(j.last_refresh_at*1000).toLocaleTimeString() : 'never');
    }}
    async function goGrok() {{
      await fetch(LOGIN_START_URL, {{ method:'POST' }});
      setTimeout(checkStatus, 1500);
    }}
    async function markDone() {{
      const r = await fetch(STATUS_URL);
      const j = await r.json();
      if (!j.logged_in) {{
        alert('Not signed in yet. Sign in to grok.com via Google OAuth in the embedded browser, then click Done.');
        return;
      }}
      alert('Signed in. Bridge will keep cookies fresh automatically.');
      window.close();
    }}
    checkStatus();
    setInterval(checkStatus, 5000);
  </script>
</body></html>"""


# ── Inference: bridge-as-proxy ───────────────────────────────────────────
def _model_to_mode_id(model: str) -> str:
    if not model:
        return "fast"
    m = model.lower()
    if "grok-4" in m:
        return "expert"
    return "fast"


def _flatten_messages(messages: list[dict]) -> str:
    """Same flattening as llm-proxy2's grok_web._flatten_messages_to_prompt."""
    parts: list[str] = []
    last_user: Optional[str] = None
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            chunks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text":
                    chunks.append(b.get("text", ""))
                elif isinstance(b, dict):
                    chunks.append(json.dumps(b))
                else:
                    chunks.append(str(b))
            text = "\n".join(c for c in chunks if c)
        else:
            text = str(content)
        if role == "system":
            parts.append(f"[System]\n{text}")
        elif role == "user":
            last_user = text
            parts.append(f"[User]\n{text}")
        elif role == "assistant":
            parts.append(f"[Assistant]\n{text}")
        else:
            parts.append(text)
    if len(parts) == 1 and last_user:
        return last_user
    return "\n\n".join(parts)


def _build_grok_body(message: str, mode_id: str) -> dict:
    return {
        "message": message,
        "parentResponseId": "",
        "disableSearch": False,
        "enableImageGeneration": False,
        "imageAttachments": [],
        "returnImageBytes": False,
        "returnRawGrokInXaiRequest": False,
        "fileAttachments": [],
        "enableImageStreaming": False,
        "imageGenerationCount": 0,
        "forceConcise": True,
        "enableSideBySide": False,
        "sendFinalMetadata": True,
        "metadata": {"request_metadata": {}},
        "disableTextFollowUps": True,
        "isFromGrokFiles": False,
        "disableMemory": False,
        "forceSideBySide": False,
        "isAsyncChat": False,
        "skipCancelCurrentInflightRequests": False,
        "isRegenRequest": False,
        "disableSelfHarmShortCircuit": False,
        "collectionIds": [],
        "disabledConnectorIds": [],
        "deviceEnvInfo": {
            "darkModeEnabled": False,
            "devicePixelRatio": 1.0,
            "screenWidth": 1280,
            "screenHeight": 800,
            "viewportWidth": 999,
            "viewportHeight": 800,
        },
        "modeId": mode_id,
    }


async def _post_to_grok(conv_id: str, body: dict, statsig_id: Optional[str]) -> tuple[int, str]:
    """Single HTTP POST to grok.com /responses. Returns (status, text).
    Cookies come from the live BrowserContext."""
    cookies = await _cookies_dict()
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = await _capture_request_headers()
    if statsig_id:
        headers["x-statsig-id"] = statsig_id
    headers["x-xai-request-id"] = str(uuid.uuid4())
    if "x-userid" in cookies:
        headers["x-userid"] = cookies["x-userid"]
    headers["referer"] = f"{GROK_BASE}/c/{conv_id}"
    headers["cookie"] = cookie_str

    url = f"{GROK_BASE}/rest/app-chat/conversations/{conv_id}/responses"
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(url, json=body, headers=headers)
            # v3.3.3: capture 429 timestamp so subsequent /api/chat
            # callers can short-circuit during the cool-off window.
            if r.status_code == 429:
                global _last_429_at, _last_429_body
                _last_429_at = time.time()
                _last_429_body = r.text[:500]
            return r.status_code, r.text
        except httpx.HTTPError as e:
            return 599, f"network error: {e}"


@app.post("/api/chat")
async def chat(req: Request, _: None = Depends(require_bridge_token)):
    """OpenAI-shape input. Body keys:
       - messages:        list[dict]  required
       - model:           str         optional (grok-3|grok-4 etc, defaults grok-3)
       - conversation_id: str         required (operator's grok.com conv UUID)
       - statsig_id:      str         optional (we'll capture one if absent)
       - stream:          bool        optional (NDJSON pass-through if True)
    """
    payload = await req.json()
    messages = payload.get("messages") or []
    model = payload.get("model") or "grok-3"
    conv_id = payload.get("conversation_id") or ""
    statsig_id = payload.get("statsig_id")
    stream = bool(payload.get("stream"))
    if not conv_id:
        raise HTTPException(400, "conversation_id is required")
    if not messages:
        raise HTTPException(400, "messages is required")

    # v3.3.3: short-circuit if we've recently been 429'd by grok.com.
    # Cuts grok-side load by ~half during throttle windows + lets the
    # proxy router fall through to OpenRouter / next provider faster
    # than waiting for grok.com to refuse us a second time.
    if GROK_429_COOLDOWN_SEC > 0:
        elapsed = time.time() - _last_429_at
        if 0 < elapsed < GROK_429_COOLDOWN_SEC:
            remaining = int(GROK_429_COOLDOWN_SEC - elapsed)
            logger.info(
                "grok.com 429 cool-off active — short-circuit (remaining %ds)",
                remaining,
            )
            raise HTTPException(
                429,
                f"grok.com 429 (cached, cool-off {remaining}s remaining): "
                f"{_last_429_body[:200]}",
                headers={"Retry-After": str(remaining)},
            )

    mode_id = _model_to_mode_id(model)
    prompt = _flatten_messages(messages)
    body = _build_grok_body(prompt, mode_id)

    # If caller didn't supply a statsig-id, try to capture one quickly.
    if not statsig_id:
        statsig_id = await _capture_statsig_id(timeout_sec=8.0)

    # First attempt
    status, text = await _post_to_grok(conv_id, body, statsig_id)

    # On 401/403: refresh page (Playwright will silently solve any CF
    # challenge), capture a fresh statsig-id, retry once.
    if status in (401, 403) and INFERENCE_RETRY_AFTER_REFRESH:
        logger.warning("grok.com %s — refreshing playwright page and retrying", status)
        await _force_refresh()
        statsig_id = await _capture_statsig_id(timeout_sec=8.0) or statsig_id
        status, text = await _post_to_grok(conv_id, body, statsig_id)

    if status != 200:
        raise HTTPException(status if 400 <= status < 600 else 502, f"grok.com {status}: {text[:300]}")

    if stream:
        # Pass-through NDJSON — caller (llm-proxy2) translates to SSE.
        return StreamingResponse(
            iter([text.encode()]),  # text already buffered (httpx non-streaming);
                                     # streaming end-to-end can come in v1.1.
            media_type="application/x-ndjson",
        )

    # Non-streaming: parse out final tokens, return OpenAI shape.
    full_text = ""
    upstream_model = "grok-3" if mode_id == "fast" else "grok-4"
    response_id: Optional[str] = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        result = obj.get("result") or {}
        if result.get("token") and result.get("messageTag") == "final":
            full_text += result["token"]
        mr = result.get("modelResponse")
        if mr:
            response_id = mr.get("responseId")
            if mr.get("model"):
                upstream_model = mr["model"]
    prompt_tokens = max(1, len(prompt) // 4)
    completion_tokens = max(1, len(full_text) // 4)
    return JSONResponse({
        "id": response_id or f"chatcmpl-bridge-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": upstream_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": full_text},
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    })


# ── /vnc proxy → websockify ──────────────────────────────────────────────
# We don't do a Python-side reverse proxy — nginx handles /grok-bridge/vnc/
# routing directly (websockify needs WebSocket upgrade headers nginx is
# better at). The bridge container exposes 6080 alongside 8443 for nginx
# to reach. For local-only setups (no nginx in front), uvicorn's static
# serving here just emits a redirect to the canonical noVNC URL.
@app.get("/vnc")
async def vnc_redirect():
    return RedirectResponse(url="/vnc/")


@app.get("/vnc/")
async def vnc_index():
    return RedirectResponse(url=f"http://localhost:{NOVNC_PORT}/vnc.html?autoconnect=true&resize=remote")
