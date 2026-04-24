# Claude Pro Max OAuth — capture guide

Research plan for the "Claude Pro Max OAuth as a provider" backlog item.
This doc walks through capturing the full OAuth handshake that the
`claude-code` CLI performs against Anthropic's console, so we can
implement a direct provider that reproduces it.

## Why we have to capture

The `claude-code` CLI uses a private OAuth flow that Anthropic hasn't
publicly documented. The only reliable spec is what the CLI actually
sends on the wire. We own the proxy, so the cleanest path is:

```
workstation's claude-code CLI
  → configure ANTHROPIC_*_URL to point at our proxy
  → proxy records every request + response, then forwards to Anthropic
  → we get a transcript we can re-implement against
```

This is legitimate protocol capture (we control both endpoints and the
tokens are our own). Keep the capture log treated as a secret store —
it contains authorization codes and bearer tokens.

## Endpoint

The proxy exposes `/api/oauth-capture/*` as a passthrough/recorder.
It is disabled by default. Enable via settings / env:

```
OAUTH_CAPTURE_ENABLED=true
OAUTH_CAPTURE_UPSTREAM=https://console.anthropic.com
OAUTH_CAPTURE_SECRET=<long-random-string>   # optional but recommended
```

`OAUTH_CAPTURE_SECRET`, when set, must appear as a `?cap=<secret>`
query parameter OR an `X-Capture-Secret: <secret>` header on every
inbound request, or the call is 403'd without forwarding. This prevents
drive-by abuse of the open relay.

## Workstation-side setup (typical)

The `claude-code` CLI respects a handful of base-URL env vars. Which
one matters depends on the CLI version; set all of them to be safe:

```bash
export ANTHROPIC_BASE_URL="https://your-proxy/llm-proxy2/api/oauth-capture"
export ANTHROPIC_AUTH_URL="https://your-proxy/llm-proxy2/api/oauth-capture"
export ANTHROPIC_API_URL="https://your-proxy/llm-proxy2/api/oauth-capture"
# If OAUTH_CAPTURE_SECRET is set:
#   the CLI will send these URLs as-is, so append ?cap=... via a
#   wrapper script or put the secret in the upstream config instead.
```

Then run `claude login` or whichever subcommand triggers the OAuth dance.

## Admin endpoints for inspecting captures

All require admin auth.

- `GET  /api/oauth-capture/_log`              — list recent (default 100) captures with body previews
- `GET  /api/oauth-capture/_log/{id}`         — full record (headers + bodies)
- `GET  /api/oauth-capture/_export`           — NDJSON dump for offline analysis
- `DELETE /api/oauth-capture/_log`            — wipe before a new recording run

## What we're looking for in the capture

1. The **authorization endpoint** — where the CLI redirects the user to
   log in. Usually something like `/v1/oauth/authorize` with client_id,
   redirect_uri, scope, code_challenge (PKCE).
2. The **token endpoint** — where the CLI exchanges the auth code for
   an access_token + refresh_token. Look for `grant_type=authorization_code`.
3. The **refresh endpoint** — how the CLI refreshes an expired token.
   Usually the same token endpoint with `grant_type=refresh_token`.
4. The **API bearer format** — `Authorization: Bearer <token>` or a
   custom header? Does it include a workspace/organization identifier?
5. **Scope claims** — what scopes does the CLI request? Does Pro Max
   vs Team make a difference?
6. **Session lifetime** — access_token and refresh_token expiry windows.

## Next step after capture

Once we have 20-50 captured requests covering the full flow, implement:

- `app/auth/claude_oauth.py` — device-code / authorization-code flow
  client.
- `app/providers/claude_oauth_provider.py` — new `provider_type=
  claude-oauth` that stores encrypted tokens and refreshes on demand.
- UI: Admin → Providers → "Add Claude OAuth provider" with a "Login"
  button that kicks off the flow inline.

## Safety note

Do **not** enable capture on a publicly-reachable URL without the
secret set. A malicious client with the URL could use our proxy as a
free HTTPS amplifier pointed at Anthropic's servers. The secret +
feature-flag + single-upstream whitelist are all that prevent this.
