# Grok release operator — draft network integration, not deployed

This source combines corrected PR17's candidate lifecycle with PR21's dedicated
network planner, consumer journal and retained pre-cutover Nginx validator.
The integrated lifecycle has synthetic tests only. Historical Docker acceptance
in `docs/evidence/grok-operator-lifecycle-20261111.json` belongs to the older
operator and image; it does not validate this implementation. Existing adapter
and canonical-renderer receipts remain separate component evidence.

## Scope and release gates

Only the primary host's Grok service is recreated. Nginx and llm-proxy2 remain
running and acquire an additional `docker_grok_security` endpoint with gateway
priority -1. They retain `docker_default`; Grok moves off that shared network.
The network has normal egress. Coordinator, its auth bridge, OpenBao and all
other services remain outside this PR. Reconcile the topology with H7/W16 before
deployment. Internal TCP reachability is not evidence of external RCE.

Original image: `sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e`.
Candidate image: `sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9`.
An image reference is not a deployment or acceptance claim.

The evidence example deliberately sets every gate false. Required gates include
merged source, required checks, the Grok-specific release policy, browser and
private recovery acceptance, reviewed network topology, integrated network
recovery, outsider denial, multi-network Nginx acceptance and explicit operator
deployment approval. The inherited global lint failure remains unresolved.
The operator consumes an audited record; it cannot establish GitHub review or
grant a policy exception. Never set flags merely to make a command run.

## Reproducible checks

Use an isolated venv with deployment `requirements.txt`, pytest and Ruff:

```sh
make test-grok-release test-grok-network lint-grok-release lint-grok-network \
  GROK_PYTHON=/absolute/path/to/venv/bin/python
```

Ensure the venv's Ruff is on PATH. Network tests use pytest so both unittest and
pytest-style cases are collected. The old `test-grok-release-docker` harness now
refuses any operator declaring NETWORK_RELEASE_SCHEMA before filesystem or
Docker mutations. It is intentionally unavailable for this version: a new,
retention-preserving integrated harness is required. Do not bypass that guard.
Do not repeat the already accepted unchanged adapter fixture or full Elastic
restore as a substitute for integrated Grok acceptance.

## Private storage and preparation

New root-only transactions use
`/mnt/s/backups/codex-private-security-20261109/grok-releases/REVIEWED-TRANSACTION`.
This is outside the proposed user-run P1 checkpoint mirror. Independent backup
coverage of this private path is unverified and requires separate planning.
The NAS must be mounted separately from root and the new directory must report
root ownership and mode 0700 before private input is saved. No recursive mode,
ownership or ACL change is allowed. Raw profiles, inspection, manifests and logs
may contain secrets/application data; keep them out of GitHub and shared reports.
Existing NAS `/mnt/s/backups/codex-security-20261109/grok-releases` and local
`/var/backups/codex-grok-releases` generations remain rollback-only, unchanged.

Preparation takes the maintenance lock, reads fresh canonical Compose, service
identities and private inputs, and snapshots the original data for preparation
checks. It renders the exact canonical service/network changes and compares the
resolved result. Raw duplicate YAML is refused. Only the known new-network empty
IPAM/false-internal normalization and bounded existing Grok mount normalization
are allowed. Non-target services and other top-level objects must match exactly.
The sample overlay alone is not a migration or rollback artifact.

Preparation creates or reuses one exactly owned, stopped Nginx validation
container per transaction. It uses the original image, read-only existing mounts,
the staged full config and private scratch logs. It has no published ports,
read-only rootfs, all capabilities dropped except CHOWN, and bounded memory/CPU/PIDs. It joins only
the retained default network while original Grok still supplies upstream DNS.
It never borrows a namespace or joins the dedicated network. The fixture and
ledger are retained; inventory exclusion requires the exact verified stopped ID.
The bounded Docker validator was accepted on November 14; see its receipt below.
Full network-aware lifecycle acceptance remains pending.

## Deployment sequence — invocation shape only, gates remain pending

Run only after fresh capacity and release review, using absolute paths:

```sh
sudo /path/to/python /path/to/capacity_guard.py --peak-gib 1 -- \
  /path/to/python /path/to/release.py prepare \
  /mnt/s/backups/codex-private-security-20261109/grok-releases/REVIEWED-TRANSACTION \
  --fragment /path/to/nginx.grok-bridge.conf
```

Prepare has staging/fixture side effects; it does not change production config.
Review the private resolved diff and receipts. Deployment uses the same path,
`deploy` mode, a fresh capacity bound (the prior 9 GiB estimate assumes a profile
no larger than 4 GiB) and `--release-evidence /root-only/reviewed-evidence.json`.
Recalculate capacity rather than treating that estimate as current clearance.

The operator verifies all prepared files, inputs and exact runtime identities.
It persists outer and nested network intent before attaching the two consumers.
It rechecks original runtime and files before persisting stop intent and stopping
Grok. It takes a new cold archive, restores and verifies a separate candidate
directory, and only then performs inode-preserving configuration CAS. It recreates
Grok alone, with no pull/build, and validates exact candidate identity, image,
mounts, security, limits, environment and dedicated-network membership.
Production Nginx runs `nginx -t` with both networks before reload. Local health,
authorization denial and token-authenticated empty-chat checks send no model
prompt. Existing browser startup may still perform provider refresh traffic.
A 30-second local soak does not replace public/browser/provider acceptance.

## Recovery and limits

Invoke `rollback` against the same transaction under the lock/capacity guard.
Pre-stop network failure recovers only owned consumer attachments, without
restarting Grok. After cutover, restore original image/data/network first and
persist its recovered identity before Nginx reload and consumer detach. Never
start the old browser on candidate data. Networks and original/failed generations
are retained. Cleanup needs a named operator decision.

Journal writes use a private temporary file, fsync, expected-hash comparison,
atomic replacement and Linux directory fsync. Failed pending files are retained.
Configuration writes preserve bind-mounted file inodes. Cooperating controllers
use the maintenance lock; these checks do not serialize an unrelated root editor.
Foreign identity, route or configuration drift refuses automatic repair. On a
configuration conflict the original runtime can recover while consumer endpoints
remain attached; the result explicitly reports incomplete recovery. An unknown
network schema cannot fall back to legacy rollback. Genuine journals without any
network markers retain their legacy rollback path.

Real integrated recovery, interruption/power-loss behavior, browser session and
outsider-denial acceptance, current engine capabilities, private NAS behavior and
backup coverage, source review and release-policy resolution remain gates. Local
`deployed_local_checks_passed` never means all project findings are closed.


## Bounded retained Nginx acceptance runner

`make test-grok-nginx-docker` runs only the pre-cutover staged full-configuration
validator twice and verifies exact fixture reuse. Supply `GROK_PYTHON` as the
absolute reviewed venv interpreter, `GROK_CAPACITY_GUARD` as
`/usr/local/lib/codex-remediation/retention/capacity_guard.py`,
`GROK_NGINX_EXPECTED_COMMIT` as the exact reviewed clean commit, and
`GROK_NGINX_TEST_DIR` as a fresh direct child named `nginx-<unique-suffix>` under
`/mnt/s/backups/codex-private-security-20261109/nginx-validation`.
The 0.1 GiB guard bounds small fixture/metadata growth; no images are pulled.
Only its two exact private parent directories may be created root:root 0700;
shared parents and existing permissions remain unchanged. Off-site coverage
remains unverified. All retained test artifacts require named cleanup approval.

The runner captures all preexisting container/network identities and fingerprints,
checks canonical Compose and Nginx files, and re-audits after success or failure.
Only the exactly owned new validator may start or stop; no production service or
network is changed. Diagnostics remain private. The sanitized result names its
reviewed source and retained fixture. Snapshot comparisons do not exclude
transient changes between observations. Passing this component cannot satisfy
production dual-network, browser, outsider-denial or integrated recovery gates.


## Private Nginx include inputs

The first real validator attempt at commit `913dda18` failed and is retained;
see `docs/evidence/grok-nginx-validator-first-attempt-20261114.json`. Docker
changed the fixture OOM default from false to null after starting, and the
capability-dropped fixture could not read a uid1000 mode0600 include. The
independent follow-up audit verified all 139 preexisting containers and 14
networks unchanged; the runner failure itself did not establish that proof.

The helper now normalizes only that observed false/null fixture OOM default;
original-runtime fingerprints stay strict. The third-attempt cache correction below adds only CHOWN.
It takes private copies of only `/etc/nginx/conf.d`,
`/etc/nginx/projects-locations.d` and `/etc/letsencrypt` when those mounts exist.
Regular-file bytes and certificate link text/target semantics are preserved. Shared bounds are
256 entries, eight levels and 8 MiB. Both include trees reject symlinks. Certificate links must be relative, remain
inside that exact tree, traverse only inventoried directories and end at an
inventoried regular file. Absolute, escaping, chained and directory links,
special files, drift and interrupted copy generations refuse reuse. Original content, metadata and membership are
checked before each start and afterward (only symlink atime is excluded because
readlink may update it; source times are never reset). Copies use root-owned 0700 directories
and 0600 files. No original modes, ownership or ACLs are changed. Linux reads
use descriptor-relative NOFOLLOW and NOATIME. Records and copied include data
remain inside the private attempt; never publish them.

Every read-only bind requests `bind-recursive=readonly` and private propagation;
unsupported enforcement must fail rather than fall back. See the Docker
[recursive-bind documentation](https://docs.docker.com/engine/storage/bind-mounts/#recursive-mounts).
No DAC-bypass capability is granted. The runner independently audits original
containers, networks, staged/canonical files, include copies and source even
when fixture ownership verification fails. An exact-ID/name/owner observation
may be reported as unverified; it never authorizes lifecycle changes or a
network endpoint exemption. The bounded acceptance receipt below covers this component only.

The second retained attempt at `06a3b8be` could parse its private include copies
but failed on certificate access in the original mixed-ownership TLS mount.
All independent audits passed for its 140 preexisting containers and 14 networks;
see `docs/evidence/grok-nginx-validator-second-attempt-20261114.json`. The exact
certificate-mount snapshot handles that input without a DAC capability or any
source permission change. Certificate/private-key bytes and snapshot manifests
must remain private and must never be added to Git or public evidence.


### Retained validator cache ownership (November 14)

The third attempt at 283bdc6d passed its private include/TLS and source audits,
but nginx -t failed while assigning its ephemeral cache directory to the worker
UID. Its stopped fixture remains retained. A concurrent Coordinator replacement
prevented the all-original inventory invariant from passing: 140 other original
containers matched, one Coordinator identity changed, and one network changed.
This is not a production-wide unchanged-state claim. See the third-attempt receipt.

The current source drops ALL capabilities and adds exactly CAP_CHOWN for
Nginx cache initialization. No DAC, FOWNER, or SYS_ADMIN capability is allowed.
Every original/config/certificate bind remains forced recursively read-only;
the root filesystem is read-only. Only bounded tmpfs and this attempt's private
log scratch are writable. The exact capability list is checked before actions
and retained-fixture reuse; old fixtures without CHOWN refuse reuse. No original
permissions or service configuration change. The accepted bounded result below
passed with all independent audits; integrated release acceptance remains open.

Rationale: [Nginx ngx_create_paths](https://github.com/nginx/nginx/blob/master/src/core/ngx_file.c)
creates mode0700 cache directories and assigns the worker UID;
[CHOWN](https://docs.docker.com/engine/containers/run/#runtime-privilege-and-linux-capabilities)
is separate from discretionary-access bypass;
[forced recursive read-only mounts](https://docs.docker.com/engine/storage/bind-mounts/#recursive-mounts)
prevent writes through original mounted trees.

The fourth attempt at 1284b1c refused immediately after Docker create: Docker
reports the canonical capability name CAP_CHOWN. The fixture never started;
its default exit code0 does not prove nginx succeeded. All142 originals and14
networks matched. The source now requests and verifies exactly CAP_CHOWN;
short-name aliases are refused in inspection, and observations retain Docker
status so created-only attempts are explicit. All four fixtures remain retained.


### Accepted bounded result (November 14, 11:42 UTC)

At source 0e77488275984075d68c95a37abba83fc31d12c7, the guarded runner
validated the full staged Nginx configuration twice using the same fixture,
910fdcad4666945ee3a04846315336f1213b511b110a548db2688d4c6b8f4385.
Both checks returned success; the fixture is retained stopped, restart=no,
exit0, OOM=false. All eight audit categories passed: exact ownership, inventory
capture/evidence, 144 preexisting containers, 14 networks, five canonical/staged
files, original/private include and TLS inputs, and reviewed source.

See docs/evidence/grok-nginx-validator-accepted-20261114.json. The generation is
/mnt/s/backups/codex-private-security-20261109/nginx-validation/nginx-20261114t1142z.
The four earlier attempts remain retained (the fourth was created but never
started). No cleanup, image pull, production deployment or shared permission
change occurred. Snapshot audits do not exclude transient unrelated changes
between observations. Do not repeat this unchanged passed component.

Source checks: 100 release tests and311 network tests,201 subtests, scoped Ruff
and formatting passed; tracked secret scanning passed. Full network-aware
lifecycle/recovery, H7/W16 topology, browser authorization, outsider denial and
independent backup acceptance remain open. The retained global lint findings
are not waived. Coordinator remains outside this PR and its release process
is unchanged.


## Full network lifecycle lab preparation

The [disconnected-VM lab proposal](../../docs/rfc/grok-network-lifecycle-lab.md)
records the required identity, isolation, admission, storage and retention
boundaries for four synthetic recovery scenarios. Allocation and execution are
pending. No full-lifecycle harness command is available yet; retain the legacy
harness guard and the already accepted component evidence. Production release,
Grok lint, H7/W16, browser/provider acceptance and backup gates remain open.
