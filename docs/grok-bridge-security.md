> November12 update: PR13 is merged; final authorization requires a new candidate.
> See [final-source reconciliation](grok-final-source-20261112.md) for current evidence and open release gates.
> Older candidate results below remain historical, not tests of the new image.

# Grok bridge security candidate

## Status and scope

Prepared November 10, 2026 in draft PR13, stacked on gateway PR12. **Not deployed
or accepted for release.** Production retains the original image
`sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e`
and its earlier 0 Critical / 36 High scan. Canonical concurrent notes and the
production build checkout are preserved.

The tested candidate is
`sha256:61ed90130f77b07b6ca2141fe65be303e77dd504480a7d967621e975f1c337db`.
Its unsuppressed scan has 0 Critical / 2 High matches, neither with a listed fix:
CVE-2025-3887 in the two GStreamer bad-plugin packages. This is a package result,
not proof of exploitability or complete browser vulnerability coverage. Tests
and proxy configuration added after the build do not change its runtime context.
The unchanged candidate and production image have not been rescanned.

## Authorization and runtime changes

Previously the outer Nginx session check accepted any gateway role, while raw
VNC5900 and websockify6080 were accessible from the shared Docker network.
Those ports were not host-published. Direct bridge routes could mutate browser
state without authorization. No live conversation/model request tested that issue.

The bridge now verifies administrator sessions with a fixed internal gateway
`/api/auth/me` URL. Identity headers are ignored. Browser mutations and sockets
require the configured exact Origin; auth failures fail closed. Machine calls
still require a constant-time service-token check, without requiring an operator
cookie or Origin. Diagnostic/chat/cookie-import routes remain token-only. Tokens
are absent from HTML. An active socket rechecks its session every30seconds.
`/healthz` remains a browser-aware health probe.

Raw VNC and websockify bind loopback. FastAPI protects noVNC static content and
proxies binary sockets to fixed loopback websockify. The maintained fragment
`grok_bridge/deployment/nginx.grok-bridge.conf` routes browser HTTP and sockets
through that authorization layer. Its exact chat route supports token callers.
Docker DNS is deferred so a stopped sidecar cannot prevent Nginx reload.

Playwright1.64.1 and its matching Noble base are digest-pinned together. Supported
Ubuntu and Python updates are installed in an isolated venv. Dependencies are
locked and checked during the build. Unused global tooling, virtualenv caches
and runtime pip are removed; scanner package metadata is retained.

## Reproducible checks and evidence

Use an isolated Python3.12 environment with `grok_bridge/requirements.txt`,
pytest9.2.0 and ruff0.17.4. Run `make test-grok lint-grok lint-grok-browser`.
The59 offline tests cover token/admin/origin enforcement, side effects,
subpaths, static files, binary sockets, session revocation and health/version.
The existing httpx TestClient compatibility warning remains visible.

Build only this sidecar with guarded `make build-grok GROK_IMAGE=<candidate>`.
The exact image passed synthetic Chromium127 profile migration to156.0.8078.4,
cookie/localStorage preservation, startup/shutdown/restart, real RFB handshake,
raw-port isolation and independent original-image/profile rollback boot.

The isolated browser harness is retained in `grok_bridge/testing/browser/`.
Review its three exact image IDs before running. Optional `GROK_TEST_IMAGE`,
`GROK_TEST_GATEWAY_IMAGE` and `GROK_TEST_NGINX_IMAGE` select verified immutable
local image IDs. On a host with those images,
use `make test-grok-browser GROK_TEST_DIR=<new-local-directory>
GROK_CAPACITY_GUARD=<maintained-capacity_guard.py>` under the normal capacity
policy. It publishes no ports, uses an internal network and synthetic accounts,
and removes only container/network IDs successfully created by that run. It retains evidence/data for
inspection and refuses to overwrite prior state. A failed run needs inspection
before choosing a new directory.

The2026-11-10 final browser run passed HTTPS redirects, the actual gateway login
router and cookie flags, nonadmin rejection, noVNC framebuffer decoding and real
keyboard input, token-only machine forwarding, origin/token denial and logout
revocation of an active VNC connection in28.5seconds. It uses actual baked gateway
auth/database code, but a minimal fixture app rather than its full middleware
and workers. All300 gateway Python source files matched production. The test
Nginx had identical configuration and rootfs layers to production, though image
IDs differed. This is not full production gateway or live provider acceptance.

The replacement fragment also passed `nginx -t` within a private copy of the
complete production configuration on the exact production Nginx image. No live
Nginx configuration was changed or reloaded. Sanitized evidence accompanies this
document; private profiles/configurations and cookies must never enter GitHub.

## Recovery and deployment prerequisites

A quiesced preparation snapshot retained the entire original `/data` volume and
private configuration. Only Grok was stopped for58.93seconds, then the same
container/image/configuration resumed healthy. Independent restoration verified
52,817 files and1,230,360,511bytes. The original image booted, stopped cleanly and
restarted on its independent copy with networking disabled. A root-only NAS
copy passed an independent hash readback. This is preparation recovery material;
production has accepted writes since and needs a fresh snapshot at cutover.

Candidate recovery on a second private restoration is tracked separately in the
security report. Never run old Chromium on a profile upgraded by the candidate.
Prefer a new candidate volume restored from the fresh quiesced snapshot, retaining
the original volume unchanged for rollback. Keep all retained generations.

`grok_bridge/deployment/compose.grok-security.yaml` specifies only the pinned
image and bridge origin/auth settings. It is **not a complete rollout command**:
the deployment operator must also select the independently restored candidate
volume, retain the existing token/limits, and assert unrelated settings unchanged.
Stage the canonical Nginx fragment and its compiled block with compare-and-swap
checks, validate the full configuration, and change only the Grok service.
Never publish5900/6080, stop the full stack, or remove volumes.

Use a conservative capacity estimate and at least64GiB free reserve. Prepare and
verify the rollback operator before cutover. On failed acceptance, stop only the
candidate, restore the original configuration and original-image/volume pair,
restart and verify health. Preserve failed candidate state separately. Live
provider/account acceptance and final public/private checks remain unverified.

## Release gates and residual limitations

Global `make lint` remains a release gate. PR12's recorded5.22.54 exception does
not apply to5.22.55/Grok; no broader waiver is assumed. The parent's approval
provenance, final-head full CI and PR13 retargeting/merge remain unresolved.
Scoped checks and passing browser tests do not waive those gates or rollout tests.

Gateway sessions store the role captured at login. Logout revocation is proven;
an administrator's role demotion does not immediately revoke existing sessions.
That pre-existing gateway behavior needs a separately tested session-policy fix.
Chromium still runs as root with `--no-sandbox`; changing profile ownership and
sandbox/seccomp is a separate unverified change. No real account was used in the
browser fixture, and offline private-profile tests do not validate account health.

References: <https://playwright.dev/python/docs/docker> (version matching and
sandbox guidance), <https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html>
(fixed-target asyncio proxy), <https://pypi.org/project/fastapi/>.


### Current rehearsal checkpoint (2026-11-10 23:29 UTC)

The fresh primary private-profile rehearsal passed. It independently restored
all52,817 files from the original immutable archive, started and stopped the
candidate twice with exit0/noOOM (stop durations0.64and6.54seconds), and preserved
all80 stable cookies by offline fingerprint. Networking was disabled and no
cookie values were exported. The failed peer stop-timeout attempt remains
recorded separately; it is not relabeled successful. The original production
volume was never mounted by the candidate, and production stays unchanged.

The maintained Makefile browser target now passes all11 checks on the primary,
including actual keyboard navigation/framebuffer rendering and active VNC
revocation26.92seconds after logout. Synthetic auth storage is bounded64MiB
tmpfs; the real-profile recovery test remains disk-backed. Explicit canvas focus,
paced keystrokes and a bounded navigation wait make the input test exercise the
same browser interaction consistently. Earlier readiness/input failures remain
in retained checkpoint evidence; the earlier manual peer pass is also retained.
The harness uses the exact primary gateway/Nginx images, a private test directory
with individual readable fixture mounts, and cleanup of only owned resource IDs.
All test containers/networks were removed after acceptance.

Docker import assigned the primary candidate image ID
`sha256:2d6e56831b6c36dae36e4f775f9397bcf1ffb4a8dc8e9cb533434e85ab56e0c8`.
Its Config, RootFS layers, OS and architecture match the scanned peer candidate
exactly; image-identity receipts are retained. No unchanged image was rescanned.
See `docs/evidence/grok-primary-acceptance-20261110.json` and the updated recovery
and browser evidence files.

The maintained deployment/rollback operator and its isolated lifecycle rehearsal
are complete as described below. Before rollout, refresh the quiesced snapshot
at cutover and retain the original image/volume pair. No production proxy reload, configuration write,
merge or candidate deployment is claimed. Preserve all retained recovery copies.


## Scoped rollout operator preparation (November 10)

The operator and runbook are maintained under `grok_bridge/deployment/`.
`make test-grok-release` covers 35 synthetic failure-path tests, including a timed-out
stop, failure between original removal and candidate creation, configuration conflicts,
separate profile generations and original-runtime recovery. `make lint-grok-release`
checks these sources. CI runs both targets alongside the existing bridge checks.

The operator stages exact-image Compose and Nginx configuration with compare-and-swap
guards, snapshots a freshly quiesced original profile into a separate candidate bind
directory, and retains the original image/volume pair for rollback. It targets only
Grok. All example release gates remain false. Read the runbook before any execution.
These preparation files are not copied by the runtime Dockerfile; the tested runtime
image remains unchanged. Canonical prepare compatibility and a full isolated Docker
Compose deployment/rollback rehearsal are separate acceptance gates. Synthetic tests
do not establish that the operator has deployed or rolled back a real service.


Canonical `prepare` passed on the primary with PyYAML6.0.3 and Python3.10 after
fail-closed compatibility fixes for Compose's unused-volume rendering and Python
UTC support. The frozen original configuration round-trips through Compose exactly;
resolved dollar values are escaped to prevent a second secret interpolation. The
full staged Nginx configuration passed without reloading production. All35 synthetic
operator tests and scoped lint/format pass on Linux. The source adds a Python3.10
CI job to cover the actual primary host runtime. Sanitized evidence is in
`docs/evidence/grok-release-operator-20261110.json`. No service stop, cutover snapshot,
production configuration edit, deployment or real rollback occurred in this stage.
The subsequently completed isolated Compose lifecycle rehearsal is recorded below.


### Maintained operator lifecycle rehearsal (2026-11-11)

`make test-grok-release-docker` exercises the maintained release operator with the
exact primary original/candidate images on synthetic, isolated Docker resources.
Both full deployment/explicit rollback and failed-create recovery passed. The
original named volume remains separate from candidate data; independent restore,
candidate-only writes, Nginx container/configuration restoration and unchanged
production inventory were verified. All owned containers/networks were removed,
with synthetic data retained. See [sanitized evidence](evidence/grok-operator-lifecycle-20261111.json)
and the [operator runbook](../grok_bridge/deployment/RUNBOOK.md).

Production remains on the original image. No real provider call or new scan was
performed. Global lint/release policy, parent PR resolution, final CI and a fresh
cutover snapshot still gate deployment.
