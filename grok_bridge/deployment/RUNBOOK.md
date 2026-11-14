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
read-only rootfs, dropped capabilities and bounded memory/CPU/PIDs. It joins only
the retained default network while original Grok still supplies upstream DNS.
It never borrows a namespace or joins the dedicated network. The fixture and
ledger are retained; inventory exclusion requires the exact verified stopped ID.
Docker compatibility of this new validator still needs real acceptance.

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
