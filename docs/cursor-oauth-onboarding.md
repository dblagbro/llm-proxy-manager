# Cursor as a Provider — onboarding guide

How to plug a Cursor Pro/Business subscription into llm-proxy2 as a backend provider so consumers can route through your Cursor account (which in turn routes to Anthropic, OpenAI, etc. on their side).

**Architecture in one sentence:** an MIT-licensed third-party sidecar (`Cursor-To-OpenAI`) sits between llm-proxy2 and `api2.cursor.sh`, translating OpenAI Chat Completions ↔ Cursor's ConnectRPC+protobuf protocol. llm-proxy2 treats the sidecar as a normal OpenAI-compatible upstream.

This is the same shape as `claude-oauth` and `ChatGPT-oauth-plan` (subscription-tier OAuth providers), and now has the **same polished UI flow** — the operator never has to know the sidecar exists.

---

## TL;DR — the operator path (v4.4.31+)

1. Providers page → **Add Provider** → set **Provider type** = `cursor-oauth`.
2. Click **Generate Auth URL** in the modal.
3. The modal opens `https://www.cursor.com/dashboard` in a new tab.
4. Log in (or confirm you are logged in).
5. In Cursor's tab: open DevTools → Application → Cookies → `cursor.com` → copy the value of `WorkosCursorSessionToken`.
6. Paste the value into the modal's **Paste cookie** field.
7. Click **Authorize**.
8. The proxy exchanges the cookie via the sidecar's `/cursor/loginDeepControl`, stores the resulting `user_<id>::<JWT>` access token, and pins `base_url=http://llm-proxy2-cursor-bridge:3010/v1` + `default_model=claude-4-sonnet` automatically.
9. Provider is created **disabled** — flip it on once you've smoke-tested with Step 3 below.

That's it. No raw cookie handling, no compose-file edits, no `docker exec` plumbing.

---

## What's already in place

The sidecar container is plumbed into the docker-compose stack on every node:

```yaml
# /home/dblagbro/docker/docker-compose.yml
llm-proxy2-cursor-bridge:
  image: ghcr.io/jiuz-chn/cursor-to-openai@sha256:03be6d97e174d7320cacddfadf158ff07f324cf9c83ea7d9171e9ba1bb259755
  # pinned by digest — :latest cannot retag us silently
  expose: ["3010"]    # internal only; not exposed to host
  # stateless — no volumes, no env-var secrets
  # health-check probes `/` and accepts 404 (proves express is up)
```

Reachable from `llm-proxy2` at `http://llm-proxy2-cursor-bridge:3010/v1` — standard OpenAI-format paths (`/v1/models`, `/v1/chat/completions`). The sidecar holds **no secrets itself**; every request carries the operator's Cursor cookie as the `Authorization: Bearer ...` header.

Verify the sidecar is healthy:

```bash
sudo docker ps --filter name=llm-proxy2-cursor-bridge --format "{{.Names}} {{.Status}}"
# expected:  llm-proxy2-cursor-bridge   Up <duration> (healthy)
```

---

## The flow under the hood

`cursor-oauth` rides the same shared `OAuthProviderSpec` machinery as `claude-oauth` / `ChatGPT-oauth-plan`, with one vendor-specific quirk: **Cursor does not implement OAuth+PKCE**, so the "authorize" step is a deep-link to the user's Cursor dashboard, and the "code" the operator pastes back is the `WorkosCursorSessionToken` cookie.

| Step | Endpoint | What it does |
|---|---|---|
| Authorize | `POST /api/providers/cursor-oauth/authorize` | Returns `{state, authorize_url}`. `authorize_url` is the Cursor dashboard. `state` is a 32-byte random token TTL=10min, stored in process memory. |
| Exchange | `POST /api/providers/cursor-oauth/exchange` | Body `{state, code, name, default_model?}`. The proxy validates `state`, calls the sidecar's `/cursor/loginDeepControl` with the pasted cookie, gets back `accessToken`, and creates the Provider row. |
| Rotate | `POST /api/providers/{id}/cursor-oauth-rotate` | Same shape as exchange, used when the stored cookie expires (~30 days) and the operator pastes a fresh one. |

`extract_code_from_callback` strips known prefixes (`WorkosCursorSessionToken=`, `Cookie: WorkosCursorSessionToken=`) and trailing other cookies, so the operator can paste the bare value OR the full `document.cookie` line.

The Provider row created has:

- `provider_type = 'cursor-oauth'`
- `base_url = 'http://llm-proxy2-cursor-bridge:3010/v1'` (auto-pinned; not surfaced in the form)
- `default_model = 'claude-4-sonnet'`
- `cost_class = 'subscription'` (inherited from `SUBSCRIPTION_TIER_PROVIDER_TYPES`)
- `api_key` = the `user_<id>::<JWT>` token from the sidecar
- `enabled = false` (operator must verify, then flip on)

Dispatch reuses the standard OpenAI/litellm path — no new dispatcher. The sidecar speaks OpenAI Chat Completions; the proxy treats it like any other OpenAI-compatible upstream.

---

## v3-era fallback (paste-the-token directly)

The polished flow is what the UI presents. If you ever need to bypass it (e.g. during cluster bring-up before the sidecar is online on a peer), the **paste fallback** still works: pick `cursor-oauth` as the type, then paste a `user_<id>::<JWT>` token directly into the API key field. The same `parse_credentials` accepts the bare token, a JSON blob with `access_token` / `accessToken`, or a `{tokens: {access_token: ...}}` shape.

To get a `user_<id>::<JWT>` token by hand:

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

---

## Step 3: verify with a synthetic request

Once the Provider exists (still disabled), drive a single non-streaming call to confirm round-trip works:

```bash
sudo docker exec llm-proxy2 python3 <<'PY'
import urllib.request, json, sqlite3, secrets
from hashlib import sha256
con = sqlite3.connect('/app/data/llmproxy.db'); cur = con.cursor()
raw = f"llmp-cursortest-{secrets.token_hex(6)}"
kid = secrets.token_hex(8)
cur.execute("INSERT INTO api_keys (id,name,key_hash,key_prefix,key_type,enabled,created_at) "
            "VALUES (?,?,?,?,'standard',1,datetime('now'))",
            (kid, "cursor-smoke", sha256(raw.encode()).hexdigest(), raw[:12]))
con.commit(); con.close()
try:
    body = json.dumps({
        "model": "claude-4-sonnet",
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

If you see HTTP 500 from the sidecar, check `sudo docker logs llm-proxy2-cursor-bridge --tail 20` — if it's `ERROR_NOT_LOGGED_IN`, the cookie has expired (~30 days). Use the **Rotate** button on the Provider row to paste a fresh one through the same flow.

---

## Choosing models

Cursor's relay supports several upstreams. Call `/v1/models` through the sidecar (with a valid cookie) for the live list:

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

Common picks: `claude-4-sonnet`, `claude-4.5-sonnet`, `claude-4.6-sonnet-medium`, `gpt-4o`, `gpt-5`. Cursor's catalog churns more often than the upstream model providers'; if a model name returns a 401-looking error from the sidecar, the most likely cause is "model no longer exists in Cursor's relay" — call `/v1/models` through the sidecar to refresh.

---

## Known v1 quirks

| Issue | Behavior | Workaround |
|---|---|---|
| Sidecar returns **HTTP 500** on missing / malformed Bearer | `TypeError: Cannot read properties of undefined (reading 'split')` at `routes/v1.js:13` — should be 401 | Cosmetic; the proxy's circuit breaker treats 5xx as a failure either way. |
| Sidecar wraps Cursor's `ERROR_NOT_LOGGED_IN` as **HTTP 500** | Cookie has expired | Use the Provider's Rotate button; paste a fresh `WorkosCursorSessionToken`. Plan for ~30 day rotation cadence. |
| Sidecar has **no refresh-token flow** | Cursor's IDE rotates via `POST /oauth/token` with `grant_type=refresh_token`, but the sidecar only stores access tokens | Manual rotate on expiry. Background `refresh_access_token` in `cursor_oauth_flow.py` raises `OAuthFlowError` cleanly so the proxy's rotation worker stops rather than silently no-ops. |
| Rate limits are per-account | The 150 fast-premium-request budget on Cursor Pro is **fleet-wide** through this Provider | Don't expose this Provider to every caller — gate by api_key.key_type or by manual_override; budget like any other subscription tier. |

---

## Architecture decision record

We picked the sidecar approach (Option A in the Phase 1 recon) over native ConnectRPC implementation (Option B) because:

- Cursor's wire protocol is **ConnectRPC + Protocol Buffers** with a proprietary `x-cursor-checksum` header (jyh_cipher of timestamp + machine_id). Embedding that directly in `app/providers/` would be a significant module + ongoing maintenance burden every time Cursor rotates the cipher.
- The sidecar isolates the protocol churn — when Cursor changes their backend, we update the sidecar image, not our code.
- The sidecar is stateless + has no secrets of its own, so the security review surface is minimal (we treat it as untrusted code in the request path; the cookie is the only credential and it goes through unmodified).

If the sidecar becomes unstable or we discover Cursor-specific needs that require deeper integration, we can port to native (Option B) without disrupting the operator's onboarding flow — the `OAuthProviderSpec` row stays the same; only the auto-pinned `base_url` would change to point at an internal in-proxy dispatcher.

## Sources / prior art

- Cursor [API docs](https://cursor.com/docs/api), [ToS](https://cursor.com/terms-of-service), [SDK forum thread](https://forum.cursor.com/t/api-sdk-terms-of-use-question/159741)
- Sidecar upstream: [`JiuZ-Chn/Cursor-To-OpenAI`](https://github.com/JiuZ-Chn/Cursor-To-OpenAI) (MIT)
- Wire-format reverse-engineering: [`eisbaw/cursor_api_demo`](https://github.com/eisbaw/cursor_api_demo), [`Jordan-Jarvis/cursor-grpc`](https://github.com/Jordan-Jarvis/cursor-grpc)
- LLM-client reverse-engineering write-up: [TensorZero blog](https://www.tensorzero.com/blog/reverse-engineering-cursors-llm-client/)

## ToS

Per Kevin Neilson (Cursor team, [forum link](https://forum.cursor.com/t/api-sdk-terms-of-use-question/159741)):

> *"Embedding Cursor as a backend AI service in your product's tech stack is a supported and explicitly intended use."*

Our use case (operator's own Cursor account powering their own internal proxy stack) fits cleanly. Forbidden: reselling Cursor as a standalone service, training competing models on Cursor outputs, HIPAA/PCI data without a separate arrangement.
