# Grok bridge security candidate

## Status and scope

Prepared November 10, 2026; **not deployed or accepted**. Production remains
image `sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e`,
with retained scan 0 Critical / 36 High. That old image has not been rescanned.
This scoped branch follows gateway PR12; it does not authorize its rollout.
Canonical uncommitted operational notes and production build copies are retained.

The existing Nginx `/api/auth/me` subrequest accepts any gateway role. Raw VNC
5900 and websockify6080 were reachable on the shared Docker network. Direct
bridge routes could navigate or create conversations without authentication.
No live conversation or model request was used to test this finding.

## Candidate behavior

Every operator HTTP request and VNC WebSocket verifies an administrator session
with the fixed internal gateway `/api/auth/me` endpoint. Client identity headers
are ignored. Browser mutations and sockets require the configured exact Origin.
Missing configuration and unavailable auth services fail closed. Token-based
gateway requests remain available without a browser Origin; wrong tokens cannot
fall back to cookie access. Diagnostic/chat/cookie-import routes retain token
requirements. No token is sent in UI HTML. Active WebSocket sessions are checked
again every30seconds. `/healthz` remains a public, browser-aware health probe.

Both VNC listeners bind loopback. FastAPI serves protected noVNC static files
and proxies binary frames to fixed loopback websockify. Subpath redirects retain
`/grok-bridge/`. Browser profile storage and inference behavior are unchanged.

Playwright1.64.1 and its matching Noble base are pinned together by registry
digest. PyPI1.65.0 existed when checked, but its matching MCR image returned404;
do not mix browser/package versions. Supported Ubuntu updates and pinned
FastAPI, Starlette, multipart, uvicorn, httpx and websockets are included.

## Reproducible checks

Install grok_bridge/requirements.txt plus pytest/ruff in an isolated Python3.12
environment. Run `make test-grok`, `make lint-grok`, and guarded `make build-grok
GROK_IMAGE=tmr-security/grok-bridge:1.2.0-candidate-20261110`. The build target
does not build the gateway. The current59offline tests passed: token rejection,
explicit admin role, expired/invalid/unavailable sessions, forged headers,
origin enforcement, route side effects, protected static files, subpaths,
synthetic binary WebSocket roundtrip and browser-aware health/version checks.
The httpx TestClient compatibility deprecation remains visible.

Exact-image scan, real local VNC handshake, synthetic profile migration/restart
and original-image rollback boot are still pending. No live credentials are
allowed in test output, source or GitHub. Real Grok account acceptance remains
separate from offline browser/profile tests.

## Deployment prerequisites and rollback

Use the capacity guard with conservative growth and at least64GiB reserve.
Production data is approximately1.3GiB. Stop only this bridge for a consistent
full profile backup, verify the copy/hash, and preserve its original image/profile
pair. Candidate tests use a synthetic profile and copies, never the live volume.
Do not run an older Chromium on a profile modified by the new browser.

At cutover, update only this service's pinned image and add
`BRIDGE_PUBLIC_ORIGIN=https://www.voipguru.org` (the configured public origin).
Preserve BRIDGE_TOKEN, state volume, limits and all unrelated Compose settings.
Change the existing Nginx VNC proxy destination from
`http://$up_llm_proxy2_grok_bridge:6080/$1$is_args$args` to
`http://$up_llm_proxy2_grok_bridge:8443/vnc/$1$is_args$args`.
Validate Nginx before reload. Retain the existing outer session gate; the new
bridge performs the final administrator check. Never republish5900/6080.

If acceptance fails, stop only the candidate, restore the original Nginx/Compose
and original-image/profile pair, start the bridge, and verify browser health.
Retain failed candidate data separately. Do not delete volumes or broad-prune.

## Gates and residual limitations

The repository's global lint gate still applies. PR12's recorded one-release
5.22.54 waiver does not cover5.22.55/Grok. No broader exception is assumed.
Scoped tests/lint do not waive repository checks, independent recovery tests,
Nginx tests, current-image scan review or live public/private acceptance.
The inherited root/no-sandbox Chromium policy is not changed by this candidate;
nonroot sandbox isolation requires a separate tested profile/ownership/seccomp
change. Package scan findings are not a proof of reachability or full browser
vulnerability coverage.

References: https://playwright.dev/python/docs/docker (matching versions and
sandbox guidance); https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html
(fixed-target asyncio proxy); https://pypi.org/project/fastapi/.

Latest preparation: the first build failed when pip tried to replace Ubuntu's
managed typing_extensions. Application dependencies now use /opt/grok-venv.
The retry image902ce803 scanned1Critical/11High; ten findings were in unused
global pip/virtualenv tooling. That retry was never deployed. The final recipe
removes that unused toolchain and locks resolved application dependencies.
Review also added active WebSocket session-revocation tests, clean rejection
of text frames, upstream-handshake error handling, and robust symlink lock
checks. The old synthetic Chromium127 profile passed creation, independent
rollback boot and restart; no real account was used.


The first cache-cleanup candidate passed its real private HTTP/RFB and synthetic profile upgrade/restart rehearsal. Its unsuppressed scan still included the base virtualenv cache and runtime pip bundle. The next candidate removes those unused packaging tools and the cached copy, preserving all library SBOMs rather than suppressing scanner results. Dependency consistency is checked during the build before pip removal; exact-image boot/scan must be repeated for the changed image.
