# Cursor as a Provider — onboarding guide

How to plug a Cursor Pro/Business subscription into llm-proxy2 as a backend provider so consumers can route through your Cursor account (which in turn routes to Anthropic, OpenAI, etc. on their side).

**Architecture in one sentence:** an MIT-licensed third-party sidecar (`Cursor-To-OpenAI`) sits between llm-proxy2 and `api2.cursor.sh`, translating OpenAI Chat Completions ↔ Cursor's ConnectRPC+protobuf protocol. llm-proxy2 treats the sidecar as a normal OpenAI-compatible upstream.

This is the same shape as `claude-oauth` and `codex-oauth` (subscription-tier OAuth providers), just realized differently — Cursor's IDE protocol is too proprietary to embed directly in our codebase, so the sidecar absorbs the complexity.

---

## What's already in place

The sidecar container is plumbed into the docker-compose stack:

```yaml
# /home/dblagbro/docker/docker-compose.yml
llm-proxy2-cursor-bridge:
  image: ghcr.io/jiuz-chn/cursor-to-openai@sha256:03be6d97e174d7320cacddfadf158ff07f324cf9c83ea7d9171e9ba1bb259755
  # pinned by digest — :latest cannot retag us silently
  expose: ["3010"]    # internal only; not exposed to host
  # stateless — no volumes, no env-var secrets
  # health-check probes `/` and accepts 404 (proves express is up)
```

Reachable from `llm-proxy2` at `http://llm-proxy2-cursor-bridge:3010/v1` — standard OpenAI-format paths (`/v1/models`, `/v1/chat/completions`). The sidecar holds **no secrets itself**; every request must carry the operator's Cursor cookie as the OpenAI `Authorization: Bearer ...` header.

Verify the sidecar is healthy:

```bash
sudo docker ps --filter name=llm-proxy2-cursor-bridge --format "{{.Names}} {{.Status}}"
# expected:  llm-proxy2-cursor-bridge   Up <duration> (healthy)
```

---

## Step 1: get a Cursor session cookie

The proxy needs a JWT-format cookie of the shape `user_<id>::eyJhbGciOiJIUzI1NiI...`. Two paths:

### Path A (recommended) — use the sidecar's `/cursor/loginDeepControl`

1. Log into cursor.com in a browser.
2. Open DevTools → Application → Cookies → `cursor.com` → copy the value of `WorkosCursorSessionToken`.
3. From inside the docker network (or any machine that can reach the sidecar), call:

   ```bash
   sudo docker exec llm-proxy2 python3 -c "
   import urllib.request, json
   wcst = 'PASTE_WorkosCursorSessionToken_HERE'
   req = urllib.request.Request(
       'http://llm-proxy2-cursor-bridge:3010/cursor/loginDeepControl',
       headers={'Authorization': f'Bearer {wcst}'},
   )
   r = json.load(urllib.request.urlopen(req, timeout=10))
   print(r['accessToken'])
   "
   ```

4. The printed value is the cookie you'll paste into the proxy. Format: `user_<id>::<JWT>`.

### Path B — IDE login flow (slower)

Run the upstream `npm run login` flow inside the sidecar container, follow the printed URL in a browser, complete the Cursor login. Cookie is printed to stdout. See [the upstream README](https://github.com/JiuZ-Chn/Cursor-To-OpenAI) for the exact procedure.

---

## Step 2: add the Provider in llm-proxy2

In the admin UI (Providers page), click **Add Provider** and set:

| Field | Value |
|---|---|
| Name | `Cursor-Subscription` (or whatever's meaningful) |
| Provider type | `openai` |
| Base URL | `http://llm-proxy2-cursor-bridge:3010/v1` |
| API key | paste the `user_<id>::<JWT>` cookie from Step 1 |
| Default model | `claude-3-7-sonnet` (or another — see Step 4) |
| Cost class | `subscription` ⚠️ **must be set explicitly** (per v3.0.57 — the default `per_call` derivation is wrong for this provider) |
| Enabled | `false` initially — flip on after the verification in Step 3 |

The api_key is the credential. The sidecar holds no secrets; the proxy stores the cookie in the encrypted `Provider.encrypted_key` column and forwards it per request.

---

## Step 3: verify with a synthetic request

Once the Provider row exists, drive a single non-streaming call to confirm round-trip works:

```bash
sudo docker exec llm-proxy2 python3 <<'PY'
import urllib.request, json, sqlite3, secrets
from hashlib import sha256
# Mint a temp api_key scoped to test the Cursor provider only
con = sqlite3.connect('/app/data/llmproxy.db'); cur = con.cursor()
raw = f"llmp-cursortest-{secrets.token_hex(6)}"
kid = secrets.token_hex(8)
cur.execute("INSERT INTO api_keys (id,name,key_hash,key_prefix,key_type,enabled,created_at) "
            "VALUES (?,?,?,?,'standard',1,datetime('now'))",
            (kid, "cursor-smoke", sha256(raw.encode()).hexdigest(), raw[:12]))
con.commit(); con.close()
try:
    body = json.dumps({
        "model": "claude-3-7-sonnet",  # or whatever you set as default
        "max_tokens": 16,
        "messages": [{"role":"user","content":"Reply with the single word: ok"}],
    }).encode()
    req = urllib.request.Request(
        "http://127.0.0.1:3000/v1/messages", data=body,
        headers={"x-api-key": raw, "content-type": "application/json"},
    )
    r = urllib.request.urlopen(req, timeout=60)
    print(json.dumps(json.loads(r.read()), indent=2)[:500])
finally:
    con = sqlite3.connect('/app/data/llmproxy.db')
    con.execute("UPDATE api_keys SET deleted_at=datetime('now') WHERE id=?", (kid,))
    con.commit(); con.close()
PY
```

Success criteria: 200 response with a JSON body containing `content[0].text == "ok"` (or close). Activity log should show one `llm_request` event against the Cursor provider with `cost_class=subscription`.

If you see HTTP 500 from the sidecar, check `sudo docker logs llm-proxy2-cursor-bridge --tail 20` — if it's `ERROR_NOT_LOGGED_IN`, the cookie has expired (Cursor cookies last ~30 days; refresh via Step 1).

---

## Step 4: choose models

Cursor's relay supports (as of 2026-05-29) several upstreams. The Provider's `default_model` should match one of these strings exactly — call `/v1/models` through the sidecar (with a valid cookie) for the live list:

```bash
sudo docker exec llm-proxy2 python3 -c "
import urllib.request, json
req = urllib.request.Request('http://llm-proxy2-cursor-bridge:3010/v1/models',
    headers={'Authorization': 'Bearer PASTE_COOKIE_HERE'})
r = json.load(urllib.request.urlopen(req, timeout=10))
for m in r['data'][:30]:
    print(' ', m.get('id'))
"
```

Common picks: `claude-3-7-sonnet`, `claude-3-5-sonnet`, `gpt-4o`, `gpt-5`.

---

## Known v1 quirks

| Issue | Behavior | Workaround |
|---|---|---|
| Sidecar returns **HTTP 500** on missing / malformed Bearer | `TypeError: Cannot read properties of undefined (reading 'split')` at `routes/v1.js:13` — should be 401 | Cosmetic only — when the Provider's stored cookie is valid, requests succeed. The proxy circuit-breaker treats 5xx as a failure either way. |
| Sidecar returns **HTTP 500** wrapping Cursor's `ERROR_NOT_LOGGED_IN` | When the cookie expires | Re-do Step 1 with a fresh `WorkosCursorSessionToken`. Plan for cookie rotation every ~30 days until we automate it. |
| Cookie has **no refresh-token flow** in the sidecar | The IDE rotates via `POST /oauth/token` with `grant_type=refresh_token`, but the sidecar only stores access tokens | Manual re-paste on expiry. Sidecar enhancement candidate for v2. |
| Rate limits are per-account | The 150 fast-premium-request budget on Cursor Pro is **fleet-wide** through this Provider | Don't expose this Provider to every caller — gate by api_key.key_type or by manual_override; budget like any other subscription tier. |

---

## Architecture decision record

We picked the sidecar approach (Option A in the Phase 1 recon) over native ConnectRPC implementation (Option B) because:

- Cursor's wire protocol is **ConnectRPC + Protocol Buffers** with a proprietary `x-cursor-checksum` header (jyh_cipher of timestamp + machine_id). Embedding that directly in `app/providers/` would be a significant module + ongoing maintenance burden every time Cursor rotates the cipher.
- The sidecar isolates the protocol churn — when Cursor changes their backend, we update the sidecar image, not our code.
- The sidecar is stateless + has no secrets of its own, so the security review surface is minimal (we treat it as untrusted code in the request path; the cookie is the only credential and it goes through unmodified).

If the sidecar becomes unstable or we discover Cursor-specific needs that require deeper integration, we can port to native (Option B) without disrupting the operator's onboarding flow — the Provider config stays the same; only the `base_url` would change to point at an internal in-proxy dispatcher.

## Sources / prior art

- Cursor [API docs](https://cursor.com/docs/api), [ToS](https://cursor.com/terms-of-service), [SDK forum thread](https://forum.cursor.com/t/api-sdk-terms-of-use-question/159741)
- Sidecar upstream: [`JiuZ-Chn/Cursor-To-OpenAI`](https://github.com/JiuZ-Chn/Cursor-To-OpenAI) (MIT)
- Wire-format reverse-engineering: [`eisbaw/cursor_api_demo`](https://github.com/eisbaw/cursor_api_demo), [`Jordan-Jarvis/cursor-grpc`](https://github.com/Jordan-Jarvis/cursor-grpc)
- LLM-client reverse-engineering write-up: [TensorZero blog](https://www.tensorzero.com/blog/reverse-engineering-cursors-llm-client/)

## ToS

Per Kevin Neilson (Cursor team, [forum link](https://forum.cursor.com/t/api-sdk-terms-of-use-question/159741)):

> *"Embedding Cursor as a backend AI service in your product's tech stack is a supported and explicitly intended use."*

Our use case (operator's own Cursor account powering their own internal proxy stack) fits cleanly. Forbidden: reselling Cursor as a standalone service, training competing models on Cursor outputs, HIPAA/PCI data without a separate arrangement.
