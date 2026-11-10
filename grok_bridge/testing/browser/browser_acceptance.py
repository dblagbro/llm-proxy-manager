"""Real Chromium/noVNC/TLS/session acceptance against isolated fixtures only."""

import asyncio
import json
import time
from pathlib import Path

from playwright.async_api import async_playwright

ORIGIN = "https://operator.example.test"
checks = {}
websockets = []


async def login(page, role):
    await page.goto(ORIGIN + "/llm-proxy2/")
    result = await page.evaluate(
        """async role => {
      const r=await fetch('/llm-proxy2/api/auth/login',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({username:'fixture-'+role,password:'synthetic-'+'browser-fixture'})
      });
      return {status:r.status,body:await r.json()};
    }""",
        role,
    )
    assert result["status"] == 200 and result["body"]["role"] == role, result


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        anon = await browser.new_context(ignore_https_errors=True)
        response = await anon.request.get(ORIGIN + "/grok-bridge/login", max_redirects=0)
        assert response.status == 302 and response.headers["location"].endswith(
            "/llm-proxy2/?bridge_login_required=1"
        )
        assert (await anon.request.post(ORIGIN + "/grok-bridge/api/chat", data={})).status == 401
        checks["anonymous_redirect_and_tokenless_chat_denied"] = True
        machine = await anon.request.post(
            ORIGIN + "/grok-bridge/api/chat",
            headers={"X-Bridge-Token": "synthetic-bridge-test-value"},
            data={},
        )
        assert machine.status == 400
        assert (await machine.json())["detail"] == "conversation_id is required"
        checks["machine_token_forwarded_without_cookie_or_origin"] = True
        await anon.close()

        nonadmin = await browser.new_context(ignore_https_errors=True)
        user_page = await nonadmin.new_page()
        await login(user_page, "user")
        for route in ("/login", "/api/status", "/vnc/vnc.html"):
            assert (await nonadmin.request.get(ORIGIN + "/grok-bridge" + route)).status == 403
        checks["real_nonadmin_session_denied"] = True
        await nonadmin.close()

        admin = await browser.new_context(
            ignore_https_errors=True, viewport={"width": 1280, "height": 900}
        )
        page = await admin.new_page()
        await login(page, "admin")
        cookies = await admin.cookies(ORIGIN + "/grok-bridge/login")
        session = next(x for x in cookies if x["name"] == "llmproxy_session")
        assert (
            session["secure"]
            and session["httpOnly"]
            and session["sameSite"] == "Lax"
            and session["path"] == "/"
        )
        checks["gateway_secure_cookie_accepted_by_browser"] = {
            k: session[k] for k in ("name", "secure", "httpOnly", "sameSite", "path")
        }

        def on_socket(ws):
            if "/grok-bridge/vnc/websockify" in ws.url:
                record = {"url": ws.url, "received_frames": 0, "closed": False}
                ws.on(
                    "framereceived",
                    lambda _: record.update(received_frames=record["received_frames"] + 1),
                )
                ws.on("close", lambda: record.update(closed=True))
                websockets.append(record)

        page.on("websocket", on_socket)
        assert (await page.goto(ORIGIN + "/grok-bridge/login")).status == 200
        await page.wait_for_timeout(1000)
        frame = next(f for f in page.frames if "/vnc/vnc.html" in f.url)
        await frame.wait_for_function(
            "document.documentElement.classList.contains('noVNC_connected')", timeout=30000
        )
        await frame.locator("canvas").wait_for(state="visible")
        assert websockets and websockets[-1]["received_frames"] > 0
        checks["browser_noVNC_connected_through_tls_subpath"] = True
        assert await page.locator("#status").inner_text() != "checking session…"
        checks["browser_cookie_reaches_status_and_websocket"] = True

        # Use noVNC's public RFB keyboard API. The target Chromium must navigate
        # to the fixture, proving actual input delivery beyond a handshake.
        await frame.evaluate("""async () => {
          const UI=(await import('/grok-bridge/vnc/app/ui.js')).default;
          const r=UI.rfb;
          r.sendKey(0xffe3,'ControlLeft',true);r.sendKey(0x6c,'KeyL');r.sendKey(0xffe3,'ControlLeft',false);
          for(const c of 'http://llm-proxy2:3000/input-observed') r.sendKey(c.charCodeAt(0));
          r.sendKey(0xff0d,'Enter');
        }""")
        for _ in range(40):
            response = await admin.request.get(ORIGIN + "/llm-proxy2/observations")
            if (await response.json())["vnc_input_navigation"]:
                break
            await page.wait_for_timeout(250)
        else:
            raise AssertionError("noVNC keyboard navigation never reached target browser")
        checks["real_noVNC_keyboard_input"] = True
        await frame.wait_for_function(
            """() => {
          const c=document.querySelector('canvas');if(!c||c.width<10)return false;
          const d=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
          let green=0,total=0;for(let i=0;i<d.length;i+=400){
            total++;if(d[i]<30&&d[i+1]>220&&d[i+2]<30)green++;
          }
          return green/total>0.25;
        }""",
            timeout=15000,
        )
        checks["real_framebuffer_decoded_and_rendered"] = True
        await page.screenshot(path="/evidence/novnc-synthetic.png")

        # CSRF and token authorization must still hold behind the TLS proxy.
        bad = await admin.request.post(
            ORIGIN + "/grok-bridge/api/login/start", headers={"Origin": "https://wrong.example"}
        )
        assert bad.status == 403
        assert (
            await admin.request.post(
                ORIGIN + "/grok-bridge/api/chat", headers={"X-Bridge-Token": "wrong"}, data={}
            )
        ).status == 401
        checks["proxy_preserves_origin_and_token_denials"] = True

        before = time.monotonic()
        result = await page.evaluate(
            "async()=>{const r=await fetch('/llm-proxy2/api/auth/logout',"
            "{method:'POST'});return r.status}"
        )
        assert result == 200
        assert not any(x["name"] == "llmproxy_session" for x in await admin.cookies())
        for _ in range(140):
            if websockets[-1]["closed"]:
                break
            await page.wait_for_timeout(250)
        else:
            raise AssertionError("active VNC connection survived gateway logout")
        checks["logout_removes_cookie_and_revokes_active_vnc_seconds"] = round(
            time.monotonic() - before, 2
        )
        assert (
            await admin.request.get(ORIGIN + "/grok-bridge/api/status", max_redirects=0)
        ).status == 302
        checks["new_browser_requests_denied_after_logout"] = True
        await admin.close()
        await browser.close()


try:
    asyncio.run(main())
    result = {
        "passed": True,
        "checks": checks,
        "websockets": websockets,
        "real_account_used": False,
        "production_modified": False,
    }
except Exception as exc:
    result = {
        "passed": False,
        "checks": checks,
        "websockets": websockets,
        "error": str(exc),
        "real_account_used": False,
        "production_modified": False,
    }
    Path("/evidence/browser-result.json").write_text(json.dumps(result, indent=2) + "\n")
    raise
Path("/evidence/browser-result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result))
