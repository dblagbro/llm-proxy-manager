# Gateway PR12 offline recovery rehearsal

The source-only suite passes 56 tests on the primary host's Python 3.10, with
scoped lint/format checks. Actual primary Docker acceptance also passed: two clean
candidate cycles and independent original-image recovery, with copied database
integrity, credential-subset preservation and unchanged production identities.
See `docs/evidence/gateway-primary-offline-recovery-20261111.json`. The peer's
runtime recovery is still unverified. This evidence establishes no release approval.

## Purpose and exact scope

Use retained primary snapshot
`/var/backups/codex-remediation-20261109/gateway-security-preparation-20261110T0445`
to verify candidate boot, clean stop, restart, and original-image recovery from an
independent fresh snapshot copy. Candidate data is never used for original recovery.
The original retained snapshot and production volumes remain unchanged.

- Original image: `sha256:21a85131e8b68dbdcd13c5bc0d7a5ec56fd971565ed3d481eadb1b75e33cd900`.
- Candidate: `sha256:13771511ed12cf18e122978ccbe48af43010dbf91aa81294dc8eb0d08d00acc3`.
- Primary only: hostname must be `tmrwww01`; no peer work is supported.
- No pulls/builds, Compose changes, Nginx changes, production stops, published ports,
  Docker sockets, host networking, external HTTP requests or account login.
- Each fixture runs as image-verified UID/GID 1001:1001, network `none`, 2 GiB RAM
  without swap, two CPUs, 256 PIDs, all capabilities dropped, no new privileges.
- Writable mounts are exactly its own fresh `/app/data` and `/app/logs` directories.
  Container root is read-only and `/tmp` is a 256 MiB tmpfs. This differs from the
  production writable root and is a deliberate rehearsal constraint. Failures caused
  by additional required write paths require review; do not relax networking.
- Baseline data must be flat regular files, at most 512 MiB, with every `.db` verified
  by the retained receipt. Two fresh copies and bounded growth fit a 3 GiB planned
  peak; each fixture directory is checked against a 1 GiB limit. Monitoring is not
  a filesystem quota, so abnormal growth can occur between checks (about 4–15 s).
- The actual shared guard holds `/run/codex-maintenance-capacity.lock` and preserves
  at least 64 GiB, or its larger dynamic reserve. An additional exclusive gateway
  rehearsal lock prevents duplicate work. Guard SHA-256 is pinned in the harness.
- Normal execution has a 20-minute alarm; cleanup can need additional stop time.

## Startup findings and interpretation

`app/main.py` calls `init_db()`, loads DB-backed settings, normalizes provider records,
recovers orphan runs and starts many workers. `app/config_runtime.py:load()` applies
saved settings after environment defaults. There is no reviewed master worker-off
switch. Worker suppression is **not** asserted. Docker network `none` is the hard
boundary preventing delivery of provider, billing, notification and cluster traffic.
Those workers may attempt connections and mutate only copied records. Such failures
in private logs are expected and are not evidence of online health.

The active `/health` route comes from `app/api/cluster.py`, which is registered before
the shadowed static route in `app/main.py`. It queries enabled providers (with a short
cache) and reports `healthy` when at least one circuit breaker is available, otherwise
`degraded`; neither status proves successful online provider access. The offline probe
requires HTTP 200, the exact running image version, either documented status, and the
expected `dbPool`, `workers`, and `circuitBreakers` keys. It retains the observed status
in the sanitized evidence without claiming online health. The harness also separately
checks SQLite integrity, credential-record fingerprints, and schema consistency after
each clean shutdown. It tests anonymous `/api/auth/me`
returns 401, with proxies disabled, only over container loopback. No real password,
session cookie, API key or OAuth token is submitted to an endpoint.

The initial primary attempt A used the shadowed route's `status: ok` expectation and
could not pass readiness despite the active route responding. Retain that bounded
failure and its cleanup evidence separately. This corrected probe needs a fresh run ID;
do not rewrite attempt A as a successful recovery rehearsal.

Credential subsets in `users`, `api_keys`, `providers`, and `provider_oauth_accounts`
must remain exactly equal, including counts. Hashes stay private. A legitimate
startup migration that adds/changes such records causes a reviewable failure; the
script does not automatically accept it. It does not claim all application rows are
unchanged, nor prove key decryption, SSO, provider success or cluster compatibility.

Original recovery uses the original image plus a separate unmodified snapshot copy.
It does **not** prove an in-place downgrade over candidate-migrated data. The retained
snapshot is from its own earlier point in time. Production cutover still requires a
fresh consistent snapshot and an explicit policy for writes after cutover.

## Local source-only checks

From the local chat workspace in PowerShell:

```powershell
& work/grok-test-venv/Scripts/python.exe -B -m pytest work/gateway-rehearsal-stage/test_rehearse_gateway.py -q -p no:cacheprovider --basetemp work/gateway-rehearsal-stage/test-tmp-review
& work/grok-test-venv/Scripts/ruff.exe check --no-cache work/gateway-rehearsal-stage/rehearse_gateway.py work/gateway-rehearsal-stage/test_rehearse_gateway.py
& work/grok-test-venv/Scripts/ruff.exe format --check --no-cache work/gateway-rehearsal-stage/rehearse_gateway.py work/gateway-rehearsal-stage/test_rehearse_gateway.py
```

Use a fresh `--basetemp` suffix for each repeat. Existing pytest temp paths are retained
in the staging folder. Only the Python files, this runbook and Ruff configuration are
intended for review/integration; do not commit test temporary data or private results.
After source integration, add repository Makefile targets for these scoped tests and
lint using Python >=3.10, pytest and Ruff. Keep global lint/release status separate.

## Proposed primary run after review (not executed)

Stage reviewed harness as
`/tmp/codex-remediate-20261109/gateway-rehearsal/rehearse_gateway.py`, root-owned and
mode 0600 in a root-only directory. Compare its hash with the reviewed local file.
Do not copy the production snapshot off the host. Confirm there is no other heavy
maintenance running and the guard is the reviewed file. A new run ID is mandatory.

```sh
sudo python3 /tmp/codex-remediate-20261109/capacity_guard.py --path / --peak-gib 3 -- python3 /tmp/codex-remediate-20261109/gateway-rehearsal/rehearse_gateway.py --run-id 20261111t010000z
```

The harness must be the guard's direct child; do not insert `timeout`, `sh -c`, or
another wrapper between them. The guard source must hash to
`628bc3a62636e2b2a6d6795aa861d4f78975b683ffed8b5d7b8bded4c6e0c672`.
Never replace its source hash pin merely to get past a refusal.

Output is retained under root-only
`/var/backups/codex-remediation-20261110/gateway-offline-rehearsal-<run-id>/`:

- `receipt.private.json`: full fixture IDs, private snapshot/DB checks, phase and intents.
- `environment.private.env`: unchanged snapshot runtime credentials with only PORT
  and PYTHONDONTWRITEBYTECODE pinned for this test; never print/export this file.
- `candidate/` and `original-recovery/`: separate test data/log copies, retained.
- `command-*.private.log`: raw results, including fixture inspect Env and application
  logs. All are private. Production metadata inventory reads only IDs/images/states.
- `result.sanitized.json`: the sole normal export. No credentials, application rows,
  credential hashes, table counts or raw logs. A success means only the listed offline
  checks passed. A failure exports the error class, not the raw exception.

Expected success: candidate two clean cycles, original one clean recovery cycle,
preserved credential subsets, equal candidate restart/original schemas, database
integrity, snapshot unchanged, fixtures removed, unrelated container IDs/images/start
timestamps/running states unchanged. The snapshot receipt's SQLite digests are
verified before copying and all copied files are independently hashed. Non-DB files
have a new source/copy equality check but no historical digest in the old receipt.
An unrelated concurrent container restart causes a conservative failure, not a claim
that the rehearsal caused it.

## Interruption and cleanup

Create intent is persisted before Docker create; stop intent is persisted before stop.
After a CLI timeout the harness reconciles actual exact ID, image, owner label, mount
roots and isolation. It stops/removes only verified fixture IDs and never deletes
volumes or retained directories. Missing fixtures require successful Docker inventory;
a daemon error cannot be interpreted as absence.

SIGTERM and normal exceptions run cleanup. SIGKILL, power loss, daemon failure or a
second interruption can leave fixtures running. Recover the **same run ID** under
the real shared guard using:

```sh
sudo python3 /tmp/codex-remediate-20261109/capacity_guard.py --path / --peak-gib 3 -- python3 /tmp/codex-remediate-20261109/gateway-rehearsal/rehearse_gateway.py --run-id 20261111t010000z --cleanup-only
```

This mode reads its private receipt, verifies ownership and exact image/mount identity,
and only stops/removes recorded fixtures. It never creates or starts a container and
never treats cleanup as a successful rehearsal. It preserves the original sanitized
test result and writes `cleanup-result.sanitized.json`. If capacity is below the guard
threshold, a reviewed emergency cleanup of exact recorded fixture IDs is needed;
do not disable the shared guard or issue broad pruning commands.

After cleanup, a retry uses a new run ID and fresh copies. Keep every snapshot and test
data directory. No artifact in this folder is a deployment/merge approval or a waiver
of PR12 lint/release requirements.
