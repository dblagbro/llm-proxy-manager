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

        # ── Strategy 1: in-browser fetch to /conversations/new ──────────
        # Tries the canonical create endpoint via the page's own fetch().
        # If grok.com's anti-bot only triggers on server-IP requests, this
        # bypasses it. Cookies + UA + TLS fingerprint all match the
        # operator's logged-in browser.
        try:
            result = await _page.evaluate(
                """async () => {
                    try {
                        const res = await fetch('https://grok.com/rest/app-chat/conversations/new', {
                            method: 'POST',
                            headers: {'content-type': 'application/json'},
                            body: '{}',
                            credentials: 'include',
                        });
                        const text = await res.text();
                        return {ok: res.ok, status: res.status, body: text.slice(0, 600)};
                    } catch (e) {
                        return {error: String(e)};
                    }
                }"""
            )
            logger.info("conversation/new fetch result: %s", result)
            if isinstance(result, dict) and result.get("ok"):
                # Try to parse the conversation_id out of the body.
                import re as _re
                body = result.get("body") or ""
                m = _re.search(r'"conversation"\s*:\s*"?([0-9a-f-]{32,40})"?', body)
                if not m:
                    m = _re.search(r'"id"\s*:\s*"([0-9a-f-]{32,40})"', body)
                if m:
                    cid = m.group(1)
                    return {
                        "conversation_id": cid,
                        "method": "in_browser_fetch",
                        "url": _page.url,
                    }
                logger.info("fetch ok but no UUID in body; falling back to UI")
        except Exception as e:
            logger.warning("in-browser fetch failed: %s", e)

        # ── Strategy 2: UI automation ───────────────────────────────────
        # Send a tiny "hi" via the page's textarea — the SPA assigns a
        # UUID when the first message lands and navigates to /c/<uuid>.
        # This is the exact flow a human user follows; Cloudflare can't
        # tell it apart from real traffic.
        #
        # Use Locator API (not ElementHandle) so React re-renders during
        # SPA hydration don't detach the element between locate and act.
        # Also explicitly wait for the page to be interactive.
        try:
            # Wait for SPA to hydrate before locating anything.
            try:
                await _page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeout:
                pass  # not fatal — SPAs sometimes never go fully idle

            # Try selectors in order of specificity. Locator auto-retries
            # on stale DOM, which fixes the "Element not attached" error
            # we saw with ElementHandle.click().
            tried = []
            sent = False
            for selector in (
                'textarea[placeholder*="What"]',
                'textarea[placeholder*="Ask"]',
                'textarea[aria-label*="prompt" i]',
                'textarea[aria-label*="message" i]',
                'textarea[name="prompt"]',
                'textarea',
                'div[contenteditable="true"]',
            ):
                tried.append(selector)
                try:
                    loc = _page.locator(selector).first
                    # wait_for() handles the visibility/stable checks the
                    # ElementHandle path was failing on
                    await loc.wait_for(state="visible", timeout=3_000)
                    await loc.click()
                    # Type instead of fill — some React inputs ignore
                    # programmatic value changes that don't fire keystroke
                    # events
                    await loc.type("hi", delay=20)
                    await _page.keyboard.press("Enter")
                    logger.info("UI-send via selector=%s", selector)
                    sent = True
                    break
                except (PlaywrightTimeout, Exception) as e:
                    logger.debug("selector %s failed: %s", selector, str(e)[:120])
                    continue

            if not sent:
                return {
                    "conversation_id": None,
                    "method": "ui_failed",
                    "tried_selectors": tried,
                    "hint": (
                        "couldn't find a usable textarea on grok.com; "
                        "open /grok-bridge/login in noVNC, send a "
                        "message manually, then return"
                    ),
                }

            # Wait up to ~20s for the URL to flip to /c/<uuid>. Polled
            # rather than relying on wait_for_url because grok.com may
            # do an intermediate redirect through ``/?continue=...``.
            deadline = time.time() + 20.0
            cid: Optional[str] = None
            while time.time() < deadline:
                cid = _conv_id_from_url(_page.url)
                if cid:
                    break
                await asyncio.sleep(0.4)

            return {
                "conversation_id": cid,
                "method": "ui_send",
                "url": _page.url,
                "hint": (
                    "UI-send didn't produce a conversation in 20s; "
                    "the message may still be in flight — refresh in a "
                    "moment, or use noVNC to confirm"
                    if cid is None else None
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
