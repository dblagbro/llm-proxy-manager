# ChatGPT Codex Cloud analytics — DevTools capture brief (#245 Phase 2)

**For**: dev-tools / capture team — same kind of ask as the Anthropic
Console billing capture done earlier.

**Why**: the llm-proxy2 v3.7.27 scaffolding can already scrape and store
ChatGPT Plus / Codex Cloud usage snapshots — it just doesn't know which
XHR endpoint to hit, since the chatgpt.com analytics URL isn't
documented anywhere public. We need someone with a logged-in browser
session to identify the underlying API call so the scraper can be
pointed at it. Once the operator pastes the captured URL + cookies into
the Provider edit form, the 4h worker starts collecting authoritative
weekly utilization, closing the same blind spot we closed for Anthropic
Pro Max in v3.7.0 (proxy local counters undercount because the same
ChatGPT Plus account is also used outside the proxy — mobile app, chat
UI, Codex CLI elsewhere).

## What we need from you

Three artifacts:

1. The **full XHR request URL** that the analytics page calls
2. The **cookies** required to authenticate that request
3. A **sample response body** (JSON dump)

That's it — paste them back to the operator and we ship the field
extractor in the next release.

## Step-by-step capture procedure

1. **Sign in to ChatGPT** in a real browser tab — needs an account
   with ChatGPT Plus / Team / Enterprise. Free-tier accounts won't
   show the analytics page.

2. **Open DevTools** before navigating. Chrome / Edge: F12 →
   **Network** tab → ensure recording (red dot) is on → check
   "Preserve log".
   Filter: type "Fetch/XHR" to drop noise.

3. **Navigate** to:
   ```
   https://chatgpt.com/codex/cloud/settings/analytics
   ```

4. **Reload the page** (Ctrl+R) so the analytics XHR fires fresh.
   You should see the page render charts / numbers showing the
   account's recent ChatGPT / Codex usage.

5. **Identify the analytics XHR.** Look in the Network tab for a
   request whose response is JSON (not HTML) and contains usage
   numbers — likely names include:
   - `/api/.../usage`
   - `/backend-api/.../analytics`
   - `/codex/.../usage`
   - `/codex-api/.../analytics`
   The response should be the JSON shape that drives the page's
   chart / counter widgets. Right-click → **Copy** → **Copy as
   cURL** is the most thorough capture (gives URL + headers +
   cookies all at once).

6. **Capture the full URL** including any query string. Example
   placeholder:
   ```
   https://chatgpt.com/backend-api/codex/usage?window=7d&workspace=...
   ```
   We need it verbatim — the proxy fires a GET against this URL
   without templating it.

7. **Capture the cookies.** DevTools → **Application** tab →
   **Storage** → **Cookies** → `https://chatgpt.com` → copy all
   cookies as a JSON object. The session-auth cookie is usually
   `__Secure-next-auth.session-token` (NextAuth). Cloudflare
   anti-bot cookies (`cf_clearance`, `__cf_bm`) are commonly also
   required. When in doubt, include them all.

   Format:
   ```json
   {
     "__Secure-next-auth.session-token": "...",
     "cf_clearance": "...",
     "__cf_bm": "...",
     "_dd_s": "...",
     "...": "..."
   }
   ```

8. **Capture a sample response body.** Click the XHR row →
   **Response** tab → "Copy" or save as a `.json` file. We need
   ONE successful response so we can write the field extractor —
   the proxy stores `raw_response` on its first capture, so once
   the live scrape runs we'll have additional samples, but the
   first one unblocks the parser.

## What to send back

Three artifacts in any format that's easy for you (paste into a
secure channel, encrypted email, etc.):

| Artifact | Format | Example |
|---|---|---|
| Endpoint URL | string | `https://chatgpt.com/backend-api/codex/usage` |
| Cookies | JSON dict | `{"__Secure-next-auth.session-token": "..."}` |
| Sample response | JSON dump | `{"weekly": {"utilization": 0.42, ...}, ...}` |

**Redact carefully** — cookie values are bearer-equivalent
credentials. If you're sending them, treat them like an API key
(secure channel, short-lived). The operator will paste them into the
proxy's admin UI (which encrypts them at rest) and you can rotate
them by signing out / back in once the capture is done.

## Things we already know (don't re-derive)

- **Operator workflow** is already designed: edit a `ChatGPT-oauth-plan`
  provider in the proxy admin UI → scroll to "External Usage (ChatGPT
  / Codex Cloud)" panel → paste endpoint URL + cookies → Save → click
  "Refresh now" to verify.
- **Scraping cadence**: 4 hours (matches the Anthropic side). Operator-
  tunable later if needed.
- **Storage**: snapshots land in `external_usage_snapshot` table with
  `source='chatgpt_codex_v1'` so they're partitionable from Anthropic
  rows.
- **Cookie lifetime** for ChatGPT NextAuth sessions is typically ~14
  days; we surface a "cookies are N days old" badge in the UI so the
  operator knows when to re-capture.

## What we plan to do once you ship back the artifacts

1. Inspect the response body — identify which JSON fields map to the
   "current weekly utilization" and "resets at" concepts that the
   proxy's routing logic consumes.
2. Ship `parse_usage_response()` (currently a stub that returns `{}`)
   in `app/providers/codex_billing.py`.
3. Wire those extracted fields into the same auto-rotation rule the
   Anthropic scrape uses — when utilization >= 95% the provider is
   auto-skipped until its `resets_at` time.
4. Cut a v3.x.y release.

ETA after capture: ~1 hour. The scaffolding's been live since
v3.7.27.

## Why bother (TL;DR for context)

The ChatGPT Plus / Codex Cloud subscription is also consumed by the
operator's other channels (mobile app, chat UI, Codex CLI on personal
workstations). The proxy currently tracks only what passes through
*it*, so it has no idea what fraction of the weekly quota the account
has actually burned. That's the same gap we closed for Anthropic Pro
Max — pre-fix, the proxy thought VG was 0% used while Anthropic
Console said 100%. Closing it on the ChatGPT side prevents the proxy
from routing requests to an over-cap account when a fresher one is
available.

---

*Generated 2026-05-13. Contact: operator. Mirrors the workflow used
for the Anthropic Console capture in May 2026.*
