# Multi-vendor OAuth capture guide

Research tool for reverse-engineering the OAuth flows used by vendor CLIs
(claude-code, codex, gh copilot, gcloud, az, …). Each vendor gets its
own **capture profile** with its own upstream host(s), secret, and
enable flag so multiple captures can run in parallel without
interference.

Available presets (v2.5.0):
| Preset key | Vendor / CLI | Primary upstream |
|---|---|---|
| `claude-code` | Anthropic Claude Code CLI | `https://console.anthropic.com` |
| `openai-codex` | OpenAI Codex CLI / ChatGPT | `https://auth.openai.com` (+ api) |
| `github-copilot` | GitHub Copilot | `https://github.com` (+ copilot api) |
| `azure-aad` | Microsoft / Azure AD | `https://login.microsoftonline.com` |
| `google-oauth` | gcloud / Gemini CLI | `https://accounts.google.com` (+ oauth2) |
| `xai-grok` | xAI / Grok | `https://api.x.ai` |
| `cohere` | Cohere | `https://dashboard.cohere.com` (+ api) |
| `custom` | anything | you pick |

## UI walkthrough

Admin → **OAuth Capture** (side nav).

1. **New capture profile** card: pick a preset, give the profile a name
   (default: `<preset>-<yyyymmdd>`), click **Create + enable**.
2. Detail panel shows:
   - **Reveal** button to see the auto-generated secret (once per session).
   - Copy-paste **env block** templated to the preset's env-var names,
     with the profile URL + secret already filled in.
3. Run the CLI on your workstation (`claude login`, `codex auth`, etc.).
4. **Live captures** table tails new requests via SSE as they arrive.
5. When satisfied, **Download NDJSON** for offline reverse-eng, or **Pause**
   to stop recording.

## Manual (env-var) workflow

If you'd rather skip the UI:

```bash
# Create a profile
curl -X POST https://proxy/api/oauth-capture/_profiles \
  -H 'Content-Type: application/json' \
  -d '{"name":"claude-2026-04","preset":"claude-code","enabled":true}'
# → returns {..., "secret": "abc…"}

# On the workstation, route the CLI through us
export ANTHROPIC_BASE_URL="https://proxy/api/oauth-capture/claude-2026-04?cap=abc..."
export ANTHROPIC_AUTH_URL="$ANTHROPIC_BASE_URL"
export ANTHROPIC_API_URL="$ANTHROPIC_BASE_URL"
claude login
claude "ping"

# Inspect via admin API
curl 'https://proxy/api/oauth-capture/_log?profile=claude-2026-04' \
  -H 'Cookie: ...admin-session...'
```

## What to look for in the capture

1. **Authorization endpoint** — usually `/v1/oauth/authorize` or similar;
   records `client_id`, `redirect_uri`, `scope`, `code_challenge` (PKCE).
2. **Token endpoint** — `grant_type=authorization_code`; response has
   `access_token`, `refresh_token`, `expires_in`, `scope`.
3. **Refresh endpoint** — same token endpoint with `grant_type=refresh_token`.
4. **API bearer format** — `Authorization: Bearer <token>`, or a custom
   header? Any workspace/org ID piggy-backing?
5. **Scopes** — what does Pro Max vs Team vs free-tier ask for?
6. **Token lifetime** — access-token `expires_in`, refresh-token TTL if
   visible.

## Security notes

- Profiles are disabled by default. An enabled profile without a secret
  is an open relay — the profile create endpoint always generates a
  secret, so you'd have to actively clear it to get into that state.
- Captured request+response bodies can contain authorization codes and
  bearer tokens. Treat the `oauth_capture_log` table like a secret
  store (don't export / share the NDJSON casually).
- Each profile has its own secret. Rotating one via **Rotate** does not
  affect other profiles.
- Delete a profile via the UI (or `DELETE /api/oauth-capture/_profiles/{name}`)
  to wipe both the profile and its captured logs in one shot.

## After capture

Once the captures cover login + first API call + a refresh cycle:

1. Export NDJSON.
2. Ship a new provider in `app/providers/<vendor>_oauth_provider.py` and
   a `<vendor>-oauth` `provider_type` in `app/models/db.py`.
3. Encrypted token storage uses the existing Fernet key (same as API
   key encryption).
4. Add a **Login** button to the provider create-edit modal that kicks
   off the real OAuth flow inline.
