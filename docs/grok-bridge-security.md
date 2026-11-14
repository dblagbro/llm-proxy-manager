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

## Dedicated network preparation (2026-11-14, not deployed)

`deployment/network_policy.py` is a pure planner over resolved Compose JSON.
It does not read Docker, write configuration, attach containers, or run a release.
`plan_networks` returns a private copied model and a separate sanitized receipt;
never publish or log the copied model because it retains existing secrets.
`verify_network_scope` rejects changes outside the exact network transformation.
`expected_runtime_networks` describes expected state, not observed Docker state.

The reviewed input has Grok, Nginx and the gateway solely on logical `default`
(`docker_default`). The candidate puts Grok only on `docker_grok_security`,
an egress-capable bridge, and adds that network to Nginx and the gateway while
preserving their existing attachments and all other service configuration.
Explicit service-name aliases preserve Nginx upstream resolution and the
`http://llm-proxy2:3000/api/auth/me` callback. No Grok host ports are allowed.
The hardened image binds both raw VNC 5900 and noVNC 6080 to loopback; trusted
consumers should reach the authenticated bridge on 8443 only.

New consumer attachments use `gw_priority: -1`; the reviewed existing attachment
must have a nonnegative gateway priority. Before use, verify Docker supports
`network connect --gw-priority` and Compose is at least 2.33.1. Docker's
[`gw_priority` documentation](https://docs.docker.com/reference/compose-file/services/#gw_priority)
explains that highest priority selects the default gateway; `priority` does not.
The planner preserves old options and does not assume attachment order determines
routing. Current policy conservatively refuses unfamiliar topologies for review.

Run `make test-grok-network lint-grok-network`. These checks use synthetic data.
They establish planning behavior, not runtime isolation, rollback, or release
approval. The gateway application version is unchanged because this is unshipped
preparation and is not connected to the release operator.

Before migration, integrate with the final-image operator correction in PR17;
the existing operator rejects network changes and cannot deploy this plan.
Record current IDs, network membership/aliases, config hashes and default routes;
reject existing-network collisions and extra live members. Journal intent before
each in-place consumer attachment, reconcile timeouts by exact IDs, and preserve
concurrent edits. Rollback may detach only attachments added by that transaction
to unchanged containers and restore Grok's original network and retained volume.
Do not recreate the gateway/Nginx just to attach a network. Persist the final
canonical Compose source so a later recreation keeps the reviewed topology.

Required isolated acceptance still includes unchanged consumer routes; gateway
auth callback and Nginx-to-8443; denial of outsider access to 5900/6080/8443 and
trusted-consumer access to 5900/6080; admin HTTP/WebSocket flows; restart/recreation
persistence; interrupted attachment and rollback. Check reachability independently
of Docker configuration and use synthetic browser state. Production browser
profiles, cookies, login and provider calls are outside this preparation test.
Global lint/release policy remains unresolved. Coordinator uses its separate
review/publication process and is not part of this planner.

Preparation evidence at 2026-11-14 07:09 UTC: 18 synthetic network tests and
scoped lint/formatting passed. Docker 29.9.3 / Compose 5.7.1 accepted the synthetic
model and preserved consumer gateway priority -1. The planner accepted the live
resolved model entirely in memory; target container IDs, images and configuration
fingerprints were unchanged. No network/container was created. The repository
`make lint` command still fails with 3,255 findings in unchanged `app/`/`tests/`;
this is an open release gate, not a newly passing global check.

### Consumer attachment journal preparation

`deployment/network_transaction.py` adds a dependency-injected transaction engine
beside the planner. It has **no Docker adapter or deployment CLI** and is not wired
into `release.py`. Only synthetic fake-host tests invoke its mutation interface.
The shared policy module supplies all names and permitted consumer identities.

`NetworkTransaction.begin(host, save, transaction_id)` snapshots sanitized identity,
configuration/default-route and original-attachment fingerprints. The caller must
hold the exclusive maintenance lock; `save` must durably write the supplied private
journal before returning. The host adapter must return the exact validated schema,
including network transaction ownership and immutable IDs. It must distinguish
absence from inspection failure. Never pass raw Docker Config/Env or command errors
into a public receipt. The journal has fingerprints and fixed identifiers only.

Creation, connect and disconnect intents precede their corresponding calls.
Ambiguous transport errors are reconciled through current inspection. The engine
attaches only Nginx and the gateway in place, resumes from the last durable journal,
and removes only attachments proven to belong to that transaction on unchanged
container/network identities. It never removes a network. A rollback leaves the
empty created network retained, so a new transaction cannot silently adopt it.

The Grok release operator still owns Grok creation and rollback. It must record an
independently verified Grok container ID with `record_bridge_id`, and restore/remove
that membership before consumer rollback. Unknown members, replaced containers,
configuration changes, original-attachment changes, owned endpoint changes and
default-route drift stop the engine. Those cases require operator reconciliation;
they are **not** claimed as successful automatic recovery. Cross-file configuration
CAS, a real sanitized host adapter, endpoint/route inspection, Nginx validation with
two networks, current image integration, real interruption tests and browser/network
acceptance all remain pending.

`make test-grok-network lint-grok-network` now covers both preparation modules:
18 topology tests plus 21 transaction tests. New failure-path cases cover journal
persistence before action and after an observed effect, fresh-instance resume,
timeout ambiguity, ownership collisions, endpoint/consumer replacement, route drift
and prevention of duplicate or unowned disconnects. These synthetic checks do not
establish the behavior of Docker or production rollback.
