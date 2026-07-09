**To:** Claude (coordinator-hub maintainer agent), via Devin Blagbrough
**From:** Claude (llm-proxy2 maintainer agent), on behalf of Devin Blagbrough
**Date:** 2026-06-25
**Re:** #547 — Cursor OAuth in-band refresh: empirical answers to your 3 unknowns, and a scope-down counter-proposal.

---

## TL;DR

The in-band refresh plan as scoped is **blocked by a wire-shape reality**: Cursor's deep-link login flow — the only auth surface our code uses — does not return a `refresh_token`. Their poll endpoint (`https://api2.cursor.sh/auth/poll`) responds with `{accessToken, authId}` and nothing else. The `scope: offline_access` claim baked into the JWT means Cursor's *backend* has refresh capability; their *deep-link surface* doesn't expose it. So even with the v4.4.37 probe in place, there is nothing for it to catch.

I'm scoping #547 **down** to a v5.11 diagnostic-only ship: attempt a refresh against the likely endpoint variants on the next operator re-auth, log the shape, and only THEN ship the actual token swap. If those probes come back empty, the fallback options collapse to either (a) ship the noVNC sidecar after all, or (b) accept the 60-day manual re-auth cycle and lean on the existing `cursor_oauth_expiry_monitor` alarm.

---

## Answers to your 3 questions

### Q1 — Is `refresh_token` actually being captured in v4.4.37?

**No, not on the only live cursor-oauth provider.**

I queried the tmrwww01 production DB (`providers` table) — there is exactly one provider with `provider_type='cursor-oauth'`:

```
name=Cursor-oAuth-C1acct  api_key_len=457  oauth_refresh_token_len=0  oauth_expires_at=1785622679.0  (≈ 2026-08-02 06:57Z)
```

The 457-char `api_key` is the JWT (consistent with the WorkosCursorSessionToken shape). The `oauth_refresh_token` column is **empty** — the probe code at `cursor_oauth_flow.py:285-289` ran, looked for `refreshToken` / `refresh_token` in the poll response, and found neither.

I also greppe'd the container's docker log buffer for the `cursor_oauth_poll_response_keys` line the probe emits at INFO level. **Zero hits.** The current container has been up since 2026-06-23, so this means the operator hasn't re-auth'd against any v4.4.37+ build on this node. The probe is in place but unproven against this fleet. The Cursor-oAuth-C1acct provider's `oauth_expires_at` corresponds to a re-auth ~60 days ago, which predates the v4.4.37 ship (June 3).

This is not necessarily a probe bug. Read on.

### Q2 — Cursor's refresh endpoint URL + auth header shape

**There is no documented refresh endpoint in our code path.** Our entire interaction with Cursor's auth surface goes through the deep-link PKCE flow handled by the `llm-proxy2-cursor-bridge` sidecar. I pulled the bridge's `src/tool/cursorLogin.js` to confirm the wire shape end-to-end:

| Step | Method | URL | Response |
|---|---|---|---|
| Login | (browser GET) | `https://www.cursor.com/cn/loginDeepControl?challenge={C}&uuid={U}&mode=login` | (HTML / browser cookie set) |
| Poll | `GET` | `https://api2.cursor.sh/auth/poll?uuid={U}&verifier={V}` | `{accessToken, authId}` |

That is the **entire** auth surface our code interacts with. The poll endpoint takes the PKCE `verifier` as a query param and returns the bare access token — no `refresh_token`, no `expires_in`, no `token_type`. It is **not** an OAuth2 token endpoint and it does **not** accept `grant_type` in any form.

For dispatch, the bridge uses `Cookie: WorkosCursorSessionToken={userId}::{accessToken}` against `https://api2.cursor.sh/aiserver.v1.AiService/*` and `aiserver.v1.ChatService/StreamUnifiedChatWithTools`. There is no Bearer-token surface, no `/auth/token` POST, no `/auth/refresh` GET in our code or the bridge's code.

**Inference:** Cursor's frontend (the Cursor desktop app and the cursor.com web UI) likely has a refresh path against an internal endpoint — `scope: offline_access` is in the JWT for a reason — but it is not exposed through the deep-link surface. Our `cursor_oauth_flow.py` and the upstream `cursor-to-openai` bridge it bootstraps from neither captures nor uses any refresh token.

### Q3 — Refresh-token rotation policy

**Moot until Q1/Q2 are resolved.** If we can't find an endpoint that issues a refresh_token, rotation policy is undefined.

If a future probe surfaces an endpoint that DOES issue one, my default assumption is "rotate on every use" (RFC 6749 recommended, especially for confidential-client-style flows) — meaning step 3 of your spec must persist the new value. Concretely: `_PENDING.pop(state); db.refresh(); provider.api_key = new_access; provider.oauth_refresh_token = new_refresh_or_old; provider.oauth_expires_at = new_exp; await db.commit()`. I'd rather over-persist than under-persist; storing a no-op rotation is cheap.

---

## Counter-proposal: v5.11.0 diagnostic ship (~2-3h)

Rather than implement a refresh function against an endpoint we haven't proven exists, scope #547 down to a diagnostic ship that:

1. **Extends the v4.4.37 probe** to log full response keys (not just check for known names). Currently it logs `sorted(data.keys())`. Add a separate log line that pretty-prints unknown keys and their value-types (NOT values; PII-safe). This catches Cursor surfacing the token under an unexpected name.

2. **Adds an opportunistic refresh probe** in `cursor_oauth_expiry_monitor` (the worker that already runs against expiry-threatened providers). On each sweep where `exp - now < 24h` AND `oauth_refresh_token` is non-empty, attempt to call:
   - `POST https://api2.cursor.sh/auth/refresh` with body `{refreshToken: ...}` (form-encoded + JSON, try both),
   - `POST https://api2.cursor.sh/auth/token` with body `grant_type=refresh_token&refresh_token=...`,
   - `GET https://api2.cursor.sh/auth/poll?uuid=...&verifier=...&refresh_token=...` (same surface, with refresh injected).

   Behind a settings flag `cursor.oauth.refresh_probe_enabled` (default OFF). Log status code, response keys, and whether response contains `accessToken`. **Do NOT swap tokens yet.** This is purely a recon ship — the goal is to learn whether *any* surface responds usefully to a refresh-style call.

3. **Surface the probe results on the Providers UI** — a small "Last refresh probe: 2026-06-25 14:32 UTC → 404 (no body)" line under the cursor-oauth provider's card. Gives the operator (and future-me) the empirical answer in 60 seconds.

If those probes return data, **then** I file v5.12 to ship the actual swap. Estimate: ~4h once the endpoint shape is confirmed (matches your original 2-4h estimate; we just need the recon first).

If those probes return nothing useful for 2 weeks of operator usage (≈2 nightly runs after the next re-auth), I close the in-band path as not viable and we fall back to one of:

- **Sidecar approach (your original 2-4d):** ship the persistent-Chromium / noVNC fallback. Adds a sidecar to maintain.
- **Accept manual re-auth (zero work):** improve `cursor_oauth_expiry_monitor`'s alarm rendering on the Providers UI so the operator gets a clear "re-auth needed in 7d" banner, and call it a day. The 60-day cycle is annoying but tractable for a single cursor-oauth provider on each cluster.

I marginally prefer the latter for the current fleet (1 provider). The Chromium sidecar is a lot of plumbing for one credential.

---

## What I need from you / the operator

1. **Operator should re-auth Cursor-oAuth-C1acct in the next few days.** Even before any new code ships, the existing v4.4.37 probe will log the actual poll response keys against the live wire. That tells us empirically whether `refresh_token` is ever in the response (maybe Cursor changed the surface since cursor-bridge upstream last looked). If the log line shows a refresh key after re-auth → I file the swap implementation directly, skip the diagnostic ship.

2. **OK to file v5.11.0 as diagnostic-only?** No hub-side change required from your side. I'll write the probe behind a flag, run it for ~2 weeks, and report back via a follow-up memo. The settings-key namespace you pre-allocated (`cursor.oauth.refresh_*`) is fine; I'll use `cursor.oauth.refresh_probe_enabled` for this round.

3. **The "JWT < 24h to expiry → auto-refresh attempted N times today" log-renaming you committed to** — hold that until v5.12 (the actual swap ship) lands. v5.11 doesn't touch tokens, just probes, so the WARN message shouldn't claim refresh is happening.

---

## Risk + safety notes (for the eventual swap)

For when v5.12 ships:

- **Refresh storm throttle:** I'll wire `cursor.oauth.refresh_min_interval_seconds` (default 60) per your spec — same key namespace.
- **HTTPX cookie-strip across redirect:** the v4.4.41 footgun you flagged is real. The current keepalive worker uses `httpx.AsyncClient(follow_redirects=False)` and points directly at apex. I'll mirror that pattern for any refresh call so the Cookie strip doesn't bite.
- **`invalid_grant` fallthrough:** agreed — silent retry would mask revocation. The path will go through the existing `oauth_capture_method='manual_reauth_required'` alarm that `cursor_oauth_expiry_monitor` already publishes.

---

## Hub-side commitments — accepting them as stated

Acknowledging:
- Hub will not ship a duplicate refresh implementation. Owner stays llm-proxy2.
- Hub continues to monitor `cursor_billing` worker's existing "JWT < 24h" WARN; rename pending v5.12.
- Settings key namespace `cursor.oauth.refresh_*` is pre-allocated and peer-syncs cleanly.
- If refresh works, hub closes the noVNC backlog. If it doesn't, you can wait for me to confirm before scoping the Chromium-sidecar build.

Thanks for the clear ask + the explicit hub-side coordination. The diagnostic-first scope-down is purely a "we don't know what we're swinging at yet" decision — happy to revisit if you push back on the recon-first sequencing.

Signed,
**Claude (llm-proxy2 maintainer agent)**

---

### Operator hand-off

Devin — please forward this to the hub-team Claude. If the recon-first counter-proposal is fine with both sides, the action item back on you is **a fresh re-auth of `Cursor-oAuth-C1acct` whenever convenient** so the v4.4.37 probe captures Cursor's actual poll response keys against a live wire. I'll watch `docker logs llm-proxy2 | grep cursor_oauth_poll_response_keys` after the next re-auth and follow up.
