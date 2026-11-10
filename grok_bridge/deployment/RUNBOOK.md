# Grok release operator — staged, not deployed

This source is a reviewable candidate for the maintained repository. Its unit
tests use synthetic files and simulated Docker commands. They do not certify
that an actual deployment or rollback has succeeded. No release command was run
while developing this operator. The repository's Grok release/lint gate remains
unresolved; The requested gateway5.22.54 exception does not cover Grok.

An isolated full-operator Docker Compose lifecycle rehearsal is still pending.
Source tests and a successful prepare stage do not prove that lifecycle. Exercise
actual create/recreate/rollback against synthetic state before production cutover.

## Scope and invariants

- Primary host only: `tmrwww01`, root, directly under the shared capacity guard.
- Canonical stack: `/home/dblagbro/docker/docker-compose.yml`.
- Only `llm-proxy2-grok-bridge` is stopped/recreated. No build, pull, stack-down,
  volume removal or Nginx restart exists in this operator.
- Original image: `sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e`.
- Primary candidate: `sha256:2d6e56831b6c36dae36e4f775f9397bcf1ffb4a8dc8e9cb533434e85ab56e0c8`.
- Original `/data` must be an exclusively consumed, writable Docker named volume.
  The candidate gets a separate, verified bind-directory restoration. The original
  volume, original image, archive, manifests and failed candidate data are retained.
- Root-only transaction directory under `/var/backups/codex-grok-releases/`.
  Inspect/configuration/log/manifest files can contain secrets or application
  filenames. Never copy them to GitHub, public reports or chat output.
- Every non-Grok Compose service and top-level object must resolve identically.
  Bytes outside the Grok service block and its marked Nginx block are preserved.
  Existing environment/token and private.env are retained. Unrecorded changes
  to private inputs, container identities or prepared artifacts refuse deployment.
- Nginx configuration writes preserve their inode because a file bind mount can
  retain the old inode after an atomic rename. Exact-byte CAS and advisory locks
  detect changes before writing; nginx is tested before reload.

## Review and tests before integration

Install `requirements.txt`, pytest and ruff in an isolated development environment.
Run `python -m pytest test_release.py -q -p no:cacheprovider` and scoped ruff checks.
Tests cover original/candidate data separation, exact CAS conflicts, archive
traversal, real restored-file hash checks, stop timeout recovery, missing service
after a failed Compose create, rollback after a failed probe, preservation of a
concurrent edit, immutable ownership checks, and release-policy refusal.

Integrate the operator/tests/runbook into maintained source and expose commands
through the repository Makefile. Review the final source/CI and the exact candidate
mapping. Do not change the example evidence booleans merely to make the command
run. The operator consumes an audited evidence record; it cannot independently
verify GitHub approvals or establish a new lint exception.

The main agent must first test `prepare` against the actual canonical configuration
and review its private resolved diff. This does not change production service
configuration or stop a service, but it writes private staging files and creates
an owned temporary Nginx validation container. It is not a zero-side-effect check.

## Exact invocation shape (not executed)

Use absolute paths for the reviewed operator, current capacity guard, maintained
Nginx fragment and a new private transaction directory. Example path names below
are placeholders and deliberately do not select a release timestamp automatically.

```sh
sudo python3 /path/to/capacity_guard.py --peak-gib 1 -- \
  python3 /path/to/release.py prepare \
  /var/backups/codex-grok-releases/REVIEWED-TRANSACTION \
  --fragment /path/to/maintained/nginx.grok-bridge.conf
```

Inspect the private staging files and receipt without exposing their contents.
Confirm original runtime environment, command/entrypoint, original volume name,
canonical Compose resolution, immutable images, Nginx identity and fragment.
The operator refuses configurations outside its supported canonical block layout.
Do not bypass those refusals; adapt source and tests first.

After the already-required Grok release gates and integration are satisfied, supply
a root-only evidence JSON based on `release-evidence.example.json`:

```sh
sudo python3 /path/to/capacity_guard.py --peak-gib 9 -- \
  python3 /path/to/release.py deploy \
  /var/backups/codex-grok-releases/REVIEWED-TRANSACTION \
  --release-evidence /root-only/location/grok-release-evidence.json
```

The nine-GiB estimate bounds two copies of a profile up to four GiB plus metadata.
The shared guard and operator also reserve at least64GiB, or15% of the filesystem
plus2GiB if greater. The operation must wait for capacity; never remove retained
data to force it through. Current capacity may be insufficient for this conservative
bound. A separately measured, reviewed smaller bound can be supplied to the guard,
but the operator still verifies actual available space for two measured copies.

Deployment records stop intent before submitting Docker stop. It resolves timeout
ambiguity by inspecting the exact container ID, then takes a new quiesced archive
and independently restores/hashes candidate data. Production stays stopped while
that copy is verified, so downtime includes archive, restore and hash time. The
earlier preparation snapshot is never silently reused for cutover.

The staged full Nginx check runs before stopping the original so upstream DNS is
available. Configuration CAS happens after restore. Compose recreates only Grok,
the exact candidate image/mount/environment/runtime limits are checked, local
health and authenticated/anonymous status checks run, and live-mounted Nginx is
tested and reloaded. A30-second local soak checks identity, health and unrelated
container IDs. These status checks do not send model prompts or create provider
conversations. A normal production browser startup can still perform its existing
Grok refresh traffic; this operator does not claim provider-network isolation.

## Rollback and failure handling

Pre-cutover failures restart the original container and verify health. If an
ambiguous Compose call removed the original but created no candidate, rollback
uses the frozen, fully resolved original configuration with its immutable original
image and original named volume. It never starts old Chromium on candidate data.

Explicit rollback uses the same transaction directory:

```sh
sudo python3 /path/to/capacity_guard.py --peak-gib 1 -- \
  python3 /path/to/release.py rollback \
  /var/backups/codex-grok-releases/REVIEWED-TRANSACTION
```

Only the recorded candidate ID can be stopped. Failed/unclean candidate shutdown
does not prevent restoration after Docker confirms it stopped. Configuration files
are restored only when their bytes match this transaction's candidate bytes.
If another editor changed a file, the operator preserves that edit and recovers
the original runtime from its private frozen configuration; the receipt becomes
`original_runtime_recovered_config_conflict`. Nginx is not reloaded in that state.
Reconcile the conflicting configuration before declaring service/UI recovery.

An unavailable Docker daemon, an unkillable process, missing storage or a conflicting
third-party container can prevent automatic recovery. Such a failure is not reported
as success. Inspect retained state and actual container identities; do not blindly
retry deploy. A hard-killed operator needs explicit rollback after reconciliation.
The same explicit rollback command above accepts a recorded `stop_intent` even
when `cutover_intent` is false, so an interrupted pre-cutover snapshot can restart
the retained original container. A host power loss still requires operator recovery.
Never delete the receipt, snapshot or candidate directory to retry an interrupted
transaction. Start a new preparation only after the prior state is understood.

## Remaining acceptance limits

- Source/unit tests do not prove actual canonical Compose compatibility or an actual
  deployment/rollback. Run the non-deploying prepare stage after integration first.
- Advisory file locks cannot serialize unrelated editors that ignore those locks;
  whole-file CAS detects existing conflicts, but filesystem compare-and-write is
  not a kernel-supported transaction across the three configuration files.
- GNU tar preserves ACLs/xattrs; the independent verifier checks member hashes,
  links, modes and ownership, but does not independently compare every ACL/xattr.
- The archive validator conservatively refuses dangling, chained or parent-directory
  links rather than weakening restore containment. Unexpected valid profile layouts
  need reviewed handling, not an unchecked extraction fallback.
- Local authenticated status is read through the container's LAN address. Public
  HTTPS/admin browser tests, independent private-network verification, live provider
  readiness and a longer production soak remain separate acceptance tasks.
- The gateway's inherited session-role demotion limitation and Chromium's existing
  root/no-sandbox policy are not changed here.
``deployed_local_checks_passed`` therefore does not mean all project findings closed.
