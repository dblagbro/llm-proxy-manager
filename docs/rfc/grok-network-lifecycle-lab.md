# Grok network lifecycle lab proposal

Status: prepared design; lab allocation and execution pending. No production
approval, deployment, lint waiver, cleanup, or H7/W16 change is implied.

This follows draft PR21 at d92bbe6cd377cb62826d8b28d8ad169c03b3146f.
The accepted Nginx component and consumer-adapter receipts remain valid for their
bounded scopes. Do not rerun them as substitutes for integrated recovery.

## Why a separate lab is needed

The original and candidate browser images can initiate provider traffic at
startup. Unpublished ports and a separate bridge on the production Docker engine
do not establish outbound isolation. Production policy requires an ordinary
bridge with `Internal=false`; an `Internal=true` fixture must not be presented as
equivalent evidence for default-route and gateway-priority behavior.

Use a disconnected Linux VM with its own Docker engine. Do not start a second
daemon on either production host. Docker documents multiple daemons on one host
as experimental and warns that they can overwrite each other's iptables rules:
[Docker daemon documentation](https://docs.docker.com/reference/cli/dockerd/#run-multiple-daemons).
The disconnected VM is our design recommendation, not a Docker certification.

No suitable lab allocation or console access has been established in this
workstream. The operator must name an existing suitable VM and its owner, or
authorize the infrastructure owner to provision one. Provisioning is separate
from this source-preparation change.

## Requested allocation and isolation

- One Linux amd64 VM, initially 4 vCPU, 8 GiB RAM and a 60 GiB virtual disk.
  These are conservative planning allowances, not measured runtime requirements.
- Use the observed production Docker 29.9.3 and Compose 5.7.1 versions when
  available, with kernel, filesystem and engine differences recorded explicitly.
- Stage reviewed source, dependencies and immutable image archives before testing.
  No production profile, environment file, certificate, credential or application
  dataset may be copied. No container starts during staging.
- Admit only a dedicated lab VM: confirm it has no production workload, retained
  third-party container, scheduled application job, Docker restart policy or VM
  automation that can start fixtures or reconnect networking. Verify eligibility
  and autostart settings before disconnection; refuse a reused VM with unknown
  ownership or startup behavior. Do not disable another team's jobs to qualify it.
- Disconnect every virtual NIC before any fixture starts. Disable connection at
  boot. Use the hypervisor console for control. No shared host folders, production
  NAS mounts, device passthrough, host Docker socket or remote Docker context.
- Record VM identity and hypervisor NIC state, then verify guest interfaces,
  IPv4/IPv6 routes, DNS and inability to reach production/provider destinations.
  A failed network request alone is insufficient isolation proof.
- Docker bridges inside this disconnected guest retain actual `Internal=false`
  semantics. All lab ports remain unpublished. Synthetic DNS/authentication and
  test clients stay inside the guest's isolated Docker networks.
- Run one scenario at a time. Recalculate peak space before each scenario and
  retain at least 10 GiB free guest space. Stop admission if capacity is unclear;
  no automatic expansion or pruning. Retained generations count against the cap.

Read-only image metadata on November 14 measured these available inputs:

| Role | Immutable image | Logical image bytes |
|---|---|---:|
| Original Grok | `sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e` | 2,243,160,250 |
| Candidate Grok | `sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9` | 2,964,567,760 |
| Nginx / synthetic transport fixtures | `sha256:67a10554437ff2017327a3ebb36c8a779f38a7f67f861980109c1cd81e133c4a` | 62,697,582 |

The sum is 5,270,425,592 bytes (5.27 GB / 4.91 GiB), before deduplication.
Archive staging, extracted layers, guest OS, browser writable layers, snapshots
and retained failures require additional space. The 60 GiB request includes
headroom for these; it is not a measured peak or proof that every scenario fits.
No images were exported, imported, rebuilt, pulled or rescanned for this design.

## Maintained implementation required before execution

Add a new `rehearse_network_release.py`; retain the legacy harness's fail-closed
guard. Use an immutable lab context: VM/engine identity, private configuration
root, unique Compose project, complete physical resource map, source/image hashes
and a distinct lab permit. Reuse the Docker adapter's validated
`codex-grok-nettest-*` resource map rather than rewriting raw inspection results.

Replace production-only hardcoded project/path lookups with context validation
only where needed. Production defaults and release requirements stay unchanged.
The policy, renderer, runtime verifier and Nginx adapter must all use the same
context. Logical roles stay canonical; physical resources have an exact unique
prefix. Journals persist the context and mode; production entry points reject
lab journals, and resume refuses changed identities or context.

The production CLI also requires hostname `tmrwww01`, its maintenance-guard
parent, a separate NAS mount and a minimum 64 GiB capacity reserve. Preserve those
checks. The lab entry requires its own exact VM identity, guest-only private path,
mutual-exclusion lock and explicit capacity policy (at least 10 GiB reserve plus
the next scenario's peak estimate). Thread that policy through shared-body checks;
do not monkeypatch `capacity()`, fake a production hostname, mount production NAS,
or weaken the production 64 GiB reserve to fit the proposed 60 GiB guest disk.

Factor the shared transaction body behind two separate admission paths. The
production path still requires genuine approvals and release evidence. The lab
path requires verified isolated-machine identity and the synthetic fixture
permit. Never manufacture successful production approval fields. A permit alone
does not prove containment; measured isolation and capacity are required too.

Each scenario uses a fresh private generation. On the lab guest the path is
`/var/lib/codex-security-lab/grok-network-lifecycle/SCENARIO-ID`, on the guest disk
only. If exported after all fixtures stop, private evidence is retained under
`/mnt/s/backups/codex-private-security-20261109/grok-network-lifecycle/`.
That host path is outside `/mnt/s/code` and the proposed P1 mirror; independent
off-site coverage remains unverified. Do not mount it into the running lab.

Use synthetic gateway/auth responses for transport and authorization tests.
This does not establish compatibility with the live gateway's actual session
state or provider authentication. Record synthetic-data provenance and preserve
separate later browser/provider acceptance gates.

## Retention and controlled replacement

Before deployment, create a stopped original reference container outside the
working service's Compose project/service ownership tuple. Give it a separate
synthetic named volume. Its exact ID, image and data must remain unchanged.
Renaming a Compose-owned container does not protect it from `--force-recreate`.

The separate working service is explicitly lifecycle-managed. Deployment and
rollback intentionally replace that newly created working container through
Compose; intermediate working container objects therefore do not all survive.
This scope must be stated in the operator's lab authorization. Preserve their
before/after records, all images, original reference container, volumes, snapshots,
failed generations, networks and journals. No existing campaign fixture is used.

No `rm`, prune, Compose `down`, volume deletion or prefix-based cleanup is part of
the harness. At completion or failure, stop only exactly registered lab IDs,
retain resources with restart disabled, and leave the VM disconnected. Later
artifact or VM retirement requires a reviewed named list and operator approval.

## Four bounded scenarios

1. Successful deployment and rollback: consumer IDs, configuration, routes and
   original endpoints stay unchanged; candidate has only the dedicated network;
   Nginx/gateway positive controls pass; an unrelated container is denied direct
   access. Verify original data is isolated from candidate-only writes. Restore
   original configuration/data before detaching owned consumer endpoints.
2. Cold recovery before Grok stop: interrupt the controller after a consumer
   attachment takes effect but before its receipt is saved. A fresh controller
   resumes from disk; network-only rollback leaves the original process untouched.
3. Ambiguous candidate creation: lose the successful Compose response or interrupt
   before candidate identity persistence. Recover through maintained full-identity
   validation and rollback; no manual removal or invented receipt.
4. Configuration conflict: change only a synthetic canonical file. Recovery must
   preserve that concurrent edit, report incomplete recovery and retain resources
   rather than overwrite the file or claim success.

Each records actual lab Nginx validation and any reload required by its lifecycle
path, exact source/image hashes,
resource identities, durable phases, restore manifests, endpoint/route comparisons
and private inventories. Record positive-control and denied TCP tests separately
from browser authorization. Reject a negative test if its positive control fails.
Pre-stop network-only recovery must not reload Nginx merely to produce evidence;
an unneeded reload is reported as not applicable and consumer identity is preserved.
Controller process interruption does not prove host power-loss recovery; any
claim of power-loss coverage requires a separately recorded scenario.

## Acceptance and decision boundary

First implement and review the harness and meaningful refusal/recovery tests.
Use repository Makefile targets and secret scanning; add a dedicated Make target
before lab invocation. Publish sanitized evidence in PR21 and keep private input
and raw inventories out of Git. No runnable full-lifecycle command exists yet.

The current operator decision is lab allocation/access and permission for the
explicit working-container replacement lifecycle above. It does not authorize
production Grok rollout, PR17/PR21 merge, a lint exception, Coordinator changes,
H7/W16 changes, backup pipeline changes or cleanup. Production remains unchanged;
the intended impact is lab CPU, RAM, disk and staging I/O only, with no planned
application outage and no production data migration.

Design review inputs: the release-engineering read-only review identified the
hardcoded context, production-gate, containment and Compose-retention constraints.
This document resolves a proposed approach; execution and acceptance remain open.


## Descriptor preparation checkpoint

A pure frozen descriptor is implemented in network_lab_context.py. It validates
bounded duplicate-rejecting JSON, VM/engine/source/image identity, deterministic
private paths and the adapter-compatible resource map, with exact journal binding.
Namespaces include both context identity and scenario, so separate scenarios with
the same context identity have disjoint paths, projects and resource names. Fresh
filesystem and Docker-resource admission still require independent verification.
Returned records do not alias the descriptor. The network definition stays
Internal=false. Descriptor and supplied-observation checks perform no I/O and
grant no execution authority. VM eligibility, hypervisor isolation, operator
permit, capacity, actual resource admission and the full harness remain pending.
The production operator does not consume this descriptor yet. Tests are included
in make test-grok-network; scoped lint includes the new module and tests.

Validation: 67 descriptor tests, 100 release tests and 378 network tests
(the 67 are included), with 201 subtests, passed on Linux. Scoped lint and
format checks passed. See the sanitized source-evidence receipt in docs/evidence.
No runtime fixture, image scan or production operation was repeated.

## Context-aware network preparation checkpoint

The pure planner and canonical-source renderer now accept a validated, explicit
lab context. Production calls with no context keep their original names, topology
and receipt shape. Lab logical roles and aliases stay canonical; project,
container and physical network identities must match the descriptor. The actual
network definition remains Internal=false. Reserved canonical and physical
aliases are checked on service keys, container names, hostnames and attachments,
including the dedicated-only candidate bridge.

Lab plan receipts include mode=lab and the full descriptor digest without
exporting VM/engine/source/image fields. verify_network_scope and render_networks
require the exact bound receipt; matching physical names alone cannot bind VM,
engine, source or images. expected_runtime_networks checks topology only. The
receipt is neither a signature nor an operator permit: it detects drift between
supplied artifacts and context, not reviewer identity or host isolation.

The renderer separately checks concrete identities and normalized network policy
in the actual raw source, then preserves unrelated bytes. Full resolved Compose
validation remains mandatory. Production release.py, its admission gates and
runtime path remain unchanged. This is still source preparation: separate lab
admission, capacity/storage policy, runtime context integration, transaction body
and the executable lifecycle harness remain pending. No lab execution, production
operation, image scan or accepted runtime fixture was performed for this increment.

Validation for this increment: 100 release tests and 477 network tests, including
99 new lab-policy checks, with 201 subtests, passed on Linux. Scoped lint and
format passed. See docs/evidence/grok-lab-policy-source-20261114.json.

## Lab journal and adapter consistency checkpoint

NetworkTransaction now accepts an explicit validated lab context. Lab journals
use schema 2 with mode=lab and the complete context binding. Production schema 1
and default calls retain their prior contract and reject lab metadata. No missing
binding is inferred or upgraded. A changed full descriptor refuses cold resume,
including when VM, engine, source or image changes leave physical names identical.

Lab mode requires the exact maintained DockerHost adapter with the descriptor's
physical resource map and Compose project. Transaction identity and consumer IDs
must match its existing capability and the journal; no read-only adapter is enabled
automatically. Rechecks surround reads, mutations and saves. Identity drift is
outside ambiguous-transport handling and cannot be swallowed as a timeout. The
canonical projection/transaction algorithm is shared. The new bridge ID is saved
before adapter rebinding; subsequent host operations require the matching bridge
binding. Caller-held lock, durable persistence and configuration CAS remain required.

This is consistency checking, not observation of the actual VM/engine identity,
containment, capacity or authorization. Tests use the real adapter with simulated
command responses, including ambiguous effects and controller interruption before
receipt persistence; they are not runtime acceptance or power-loss proof. No new
CLI or actual lab invocation is provided. Production release.py is unchanged.
Distinct admission/storage/capacity, runtime release/Nginx context integration and
the full lifecycle harness remain pending; allocation and execution gates stand.

Review corrected two early-refusal gaps: a pre-bound bridge on initial preparation
and loss of the active context. Initial binding checks and pinned instance mode
now refuse before subsequent I/O or save callbacks. Validation: 100 release tests
and 527 network tests (50 new lab-journal checks included), with 201 subtests,
passed on Linux; scoped lint and format passed. See
docs/evidence/grok-lab-journal-source-20261114.json.

## Lab capacity arithmetic checkpoint

A separate pure lab capacity policy computes the fixed 10 GiB reserve plus
explicit simultaneous upcoming evidence and Docker-storage peaks. Roles on the
same filesystem share one reserve and aggregate both peaks; conservative minimum
available bytes are used when the supplied observations differ, while conflicting
totals refuse. Distinct filesystems each require their own reserve and peak.
Strict counters and full-context receipt binding prevent accidental type/context
drift; no implicit zero peak or production 64 GiB override is introduced.

This is arithmetic over supplied values, not a measurement or admission decision.
It does not establish freshness, peak-estimate adequacy, filesystem/mount identity,
continued free space, VM containment, eligibility or authorization. A future
read-only collector must identify the actual bound engine's Docker data root and
the exact private lab generation, validate paths and mount/device identities,
reject symlink/shared/readonly/unexpected-mount substitutions, collect fresh
space under the separate lab lock, and repeat checks before material allocation.
Retained artifacts consume measured free space; future allocations belong in the
reviewed peak. A cached assessment cannot replace fresh observation. There is no
CLI, filesystem access, Docker call or permit in this component. Production
capacity(), hostname/NAS guards and release entry points remain unchanged.

The next runtime context seam is staged Nginx validation: preserve raw inspection
truth while validating physical names from the lab descriptor, bind a distinct
lab ledger before I/O, and retain the production ledger/accepted-fixture contract.
That integration, actual storage/admission collection and the full harness remain
pending. No accepted runtime fixture or unchanged-image scan is repeated here.

Validation: 100 release tests and 603 network tests (76 new capacity tests
included), with 201 subtests, passed on Linux. Scoped lint and format passed.
See docs/evidence/grok-lab-capacity-source-20261114.json.

## Read-only storage collector source checkpoint

`network_lab_storage.collect(context, peaks=...)` observes the exact existing
generation and the bound local engine's reported Docker data root. It creates no
directory, lock, file or Docker resource. The generation and private ancestors
must already be root-owned 0700; the distinct storage lock must already be a
root-owned 0600 single-link regular file. Other queried ancestors must be
root-owned and not writable by group or others.

The collector pins directory descriptors and their mount IDs, reopens every
queried ancestor, and compares identity, ownership, mode and relevant mount
records. Queried storage mounts require writable ext4/xfs and mount root `/`;
symlinks and subtree mount roots refuse. Legitimate nested Docker overlay mounts
are not queried. Lexical and inode/device ancestry checks reject overlapping
evidence/Docker trees. Two bounded, deadline-limited read-only HTTP requests use
the fixed local Unix socket, root peer credentials and repeated socket checks.
The reported engine ID must match the full context and the engine/root/peer
binding must remain stable. No Docker CLI context or credential file is read.

Two descriptor-based space samples use unprivileged available blocks and the
lower available byte count. Device/fsid and total-byte drift refuse before the
existing capacity arithmetic runs. The storage lock serializes only this
snapshot and is released before return. A later lifecycle controller must hold
its own operation lock and freshly collect/compare storage before material
allocations; the result is neither a reservation nor an execution permit.
Public results contain hashes and counters, with sanitized failures.

Only classic `overlay2` is currently supported; missing/unknown driver metadata
or a containerd snapshotter refuses before querying storage. Docker's
[separate containerd storage](https://docs.docker.com/engine/daemon/#daemon-data-directory)
can lie outside DockerRootDir. Supporting that layout requires a reviewed
multi-root collector and capacity accounting; this change does not switch any
storage backend or authorize such a switch.

This does not prove VM eligibility, physical disconnection, physical storage
provenance, source/image admission, estimate adequacy or future free space.
Whole-filesystem binds and ext4/xfs over SAN/iSCSI remain provenance limitations;
the collector does not claim complete bind or remote-storage detection.
There is no CLI or full harness, and the production operator is unchanged.
No live collector invocation, accepted runtime-fixture repetition or image scan
was performed for this source preparation.

Kernel interfaces: [mountinfo and descriptor mount IDs](https://www.kernel.org/doc/Documentation/filesystems/proc.txt),
[Linux advisory flock semantics](https://man7.org/linux/man-pages/man2/flock.2.html)
and [Python descriptor filesystem statistics](https://docs.python.org/3/library/os.html#os.fstatvfs).

Validation: 100 release tests and 696 network tests (93 collector tests included),
with 201 subtests and zero Linux skips, passed using canonical Makefile targets.
Scoped lint and format passed. Windows ran 74 collector tests with 19 platform
skips; Linux ran all 93, including real temporary FD/flock/symlink operations
with simulated ownership/mount observations. Primary review corrected short HTTP
body framing, the separate-containerd-store omission and a fixture mode dependency.
See docs/evidence/grok-lab-storage-source-20261114.json. A separate read-only
production info query reported classic overlay2; it did not invoke this collector
or establish lab eligibility.

## Explicit lab Nginx validation context

The retained validator accepts an explicit keyword-only lab_context on
validate_staged_nginx, verify_validation_inputs and retained_validation_ids.
Raw inspect names, network keys and immutable image IDs must already match the
descriptor; records are never rewritten to resemble production. The original
Nginx/Grok snapshots are pinned and checked around host calls and journal writes.

Lab validation uses nginx-validation.lab.private.json schema 2, the complete
context binding and a context-digest fixture label. Production continues to use
schema 1 and its existing filename/defaults. Both ledger filenames are checked
before host I/O, including broken opposite-mode symlinks, so another mode cannot
silently create a second fixture. The initially validated ledger is reused;
expected-content digests advance only after this operation's successful save.
Any lab binding refusal stops reconciliation, including cleanup in a finally
block, rather than inspecting or stopping a fixture under uncertain identity.

Lab generation and Nginx bind sources require exact canonical paths, root
ownership, private modes and nonsymlink ancestry. Every original Nginx source
must lie below the exact generation; volumes and external configuration/TLS
sources refuse before copying or host calls. Ancestor identity, mode, original
snapshot, context, host-folder and ledger drift are checked before and after
host operations. Existing bounded private include copies and recursive read-only
mount protections remain. The fixture joins only the retained physical default
network, retains CAP_CHOWN as its sole added capability, and remains stopped and
retained after acceptance. A failed validation is retained for review.

These checks establish consistency only. They neither observe the actual VM or
engine identity nor grant isolation, storage, image or execution admission.
The separate admission path, shared release-body integration and full lifecycle
harness remain pending. No production hostname/NAS/capacity guard, CLI entry
point, accepted fixture or container was changed or invoked. Lab filesystem
tests simulate ownership at a private test seam; Linux also exercises real
temporary lstat/symlink behavior. See
docs/evidence/grok-lab-nginx-source-20261114.json for final scoped checks.

Validation: 100 release tests and 771 network tests (75 new lab Nginx tests),
with 201 subtests and zero Linux skips, passed canonical Makefile targets.
Scoped Ruff lint/format passed. Focused Windows checks passed 139 tests with
14 POSIX skips, including 66 new lab tests and 9 new lab platform skips.

## Shared pure release rendering for the lab

The existing release preparation helpers now accept an explicit lab descriptor.
The two network wrapper APIs additionally require the caller's exact reviewed
network receipt. They reuse the maintained candidate-service, canonical-source,
network-planner and resolved-scope algorithms, without rewriting raw resource
identities or creating implicit approval fields.

Lab rendering requires immutable original/candidate image pins, the exact
generation's candidate-data path and the full context/receipt binding. It uses
the fixed synthetic origin https://grok-lab.invalid and the canonical gateway
alias for synthetic authentication. Logical service keys remain canonical;
physical project/container/network names must match the descriptor. The desired
dedicated bridge remains Internal=false. Input objects are unchanged and
unrelated raw bytes remain preserved under the existing canonical YAML limits.
Changing VM, engine, reviewed source or image identity in the same namespace
invalidates the receipt. This does not prove that supplied data is synthetic or
that an image is admitted for use. Unmodified mounts and named-volume backing
storage still require separate admission; a concrete volume name alone does not
establish ownership or local-storage provenance.

Production calls retain their existing image, environment, path and network
defaults. Host, outer journals, prepare/deploy/rollback, CLI, release gate,
operation lock, NAS check and capacity policy are unchanged. This is a pure
source transformation and scope check: it performs no filesystem or Docker I/O
and introduces no lab execution entry point. Actual isolation, observed machine
and engine identity, source/image admission, capacity, operation locking and
authorized working-container replacement remain separate requirements.

The shared runtime body and distinct lab admission are still pending; producing
valid bytes is not integrated lifecycle or provider acceptance. No accepted
fixture, image scan, live validator/collector or production operation was run.
See docs/evidence/grok-lab-release-render-source-20261114.json.

Validation: 100 original release tests and 869 network tests (98 new lab
release-render tests), with 201 subtests and zero Linux skips, passed canonical
Makefile targets. Scoped Ruff lint/format passed. Windows passed the 100
original release and 98 new tests. All 44 other original definitions, decorators
and existing top-level code/assignments were verified unchanged by source/AST
comparison. No runtime acceptance was performed.

## Supplied lab runtime-record verification

The shared assert_runtime verifier accepts an explicit lab context and mandatory
caller-supplied full journal binding. Raw original/current container IDs, physical
names, Compose project/service labels and immutable image IDs must match that
context. The candidate requires the exact caller-supplied dedicated network ID,
distinct from the retained original default network; omission cannot fall back to
the shared bridge. Original/rollback mode rejects any candidate network ID.

Original /data remains an exact writable named volume: its retained name, source,
type and writable flag must match. Its backing directory is not assumed to live
under a default Docker root. Candidate /data is a bind of exactly the generation's
candidate-data path. Lab paths use canonical lexical comparisons without
filesystem resolution. Duplicate mount destinations/environment keys, malformed
IDs/record shapes, inconsistent context and scalar/nested comparison type drift
refuse. Existing command, security/limit, non-data-mount, restart, token and
managed-environment checks remain active; candidate origin/auth use the existing
fixed synthetic lab configuration.

Stopped and created records can be validated for ambiguous recovery. Passing this
comparison does not grant running/healthy admission. All inputs are supplied
records: this does not observe VM/engine identity, establish network/volume
ownership, prove local storage or synthetic provenance, or authorize execution.
Production calls preserve their existing comparison behavior and path resolution,
with explicit rejection of recognizable lab identity, paths and supplied bindings.

Only the existing assert_runtime definition changes in this increment, plus
private pure helpers. Host, outer journals, prepare/deploy/rollback, CLI, release
gate, operation lock, hostname/NAS and production capacity checks remain unchanged.
Distinct admission and integration into the full lifecycle body remain pending.
No live collector/validator, accepted fixture repetition, image scan, deployment,
backup/permission change or cleanup occurred. See
docs/evidence/grok-lab-runtime-context-source-20261114.json for source checks.

## Separate outer lab release journal

The shared save/load_state APIs accept explicit lab_context and lab_binding
keywords. Production keeps its unversioned state.private.json, including genuine
legacy recovery journals. Lab uses state.lab.private.json with release_schema 1,
mode lab and the caller's complete retained context binding. A caller must supply
the same full binding; the persistence layer never generates a replacement.

The lab generation must already exist at its exact canonical private path.
Existing ancestor metadata checks are reused without directory creation,
permission changes or path resolution. Opposite-mode journals, including broken
symlinks, refuse. Lab reads are bounded and reject duplicate JSON keys and
malformed values. Headers, physical project, pinned images, immutable IDs,
intent flags, paths and the nested schema2 network journal must agree.

Persistence reuses the pending-file, exact-byte CAS, atomic replacement and
directory-fsync mechanism. Validation occurs before a lab timestamp or receipt
changes. A failed write/replacement retains its pending file and the caller's
old receipt; an observed disk digest never silently becomes a new receipt.
Existing disk bytes must validate against the same full binding even when their
CAS digest matches. Original/configuration and nested consumer preparation
baselines cannot change across saves. Recorded candidate/snapshot and network/
bridge ownership receipts remain retained. Durable final bytes are reread after
directory fsync before advancing the caller's receipt.
The operation requires the caller's lock. Rechecks detect ordinary concurrent
changes; this is not protection against a hostile filesystem administrator.

Recovery journals deliberately preserve incomplete actions: network creation or
attachment intent without a receipt, cutover intent before candidate identity,
candidate identity before nested bridge binding, and original recovery before
reload/configuration-conflict resolution. Restarting the retained original may
legitimately make rollback_id equal original_id. A candidate cannot reuse either
identity. No recovery or health action is authorized by loading these records.

CLI, Host, lifecycle methods, network_save, release gates, operation lock,
hostname, NAS and production capacity policy remain unchanged. Their full
context dispatch and separate lab admission remain to implement. These journals
do not observe VM/engine identity or establish physical isolation, storage
provenance, operator authorization or source/image admission. No live journal,
fixture, collector, validator or deployment was exercised for this increment.
See docs/evidence/grok-lab-outer-journal-source-20261114.json.

Tests use actual temporary files with explicit generation-path and root-ownership
simulation. Linux exercises real descriptor metadata, FIFO, symlink, hardlink,
replacement and fsync behavior; Windows uses explicit metadata/symlink substitutes.
No test result establishes actual lab eligibility or physical isolation.

## Held lab observation session

A separate observation_session API holds a preexisting private operation lock
across the caller's scope and each fresh storage collection. The operation lock
is distinct from the collector's short snapshot lock; neither lock creates,
changes permissions on, or reserves capacity in the generation.

Before I/O, the explicit context, retained full binding and upcoming peaks must
validate. Linux/root, amd64 guest architecture, bounded kernel-reported SMBIOS
UUID and boot identity are checked. Guest UUID must match the descriptor; the
guest hostname is not substituted for the hypervisor VM name. Guest, context,
binding and lock identity are rechecked around the existing local-engine/storage
collector. Refresh requires the owning process and controller thread in an active held
session. Reentry and cross-process use refuse. A foreign thread cannot close the
owner's lock; forked children only close inherited descriptors, without unlocking
the parent's open description. Failed entry, owning-body exceptions and normal
exit release the operation lock. Closed sessions cannot collect again.

This is an observation scope, not admission. Returned receipts retain
execution_admission=false. Guest-reported UUID/boot and local engine/storage
consistency cannot prove hypervisor identity, every NIC disconnected at boot,
VM ownership or autostart policy, device/shared-folder/storage provenance,
operator authorization, source/image or synthetic-data provenance, or runtime
eligibility. Guest interfaces, routes, DNS, jobs, workload/resource inventory
and independently obtained management-plane evidence remain to integrate.
The operator's allocation and permitted working-container replacement decision
is still required. No pending email or supplied descriptor becomes a permit.

Production operator, journal persistence, existing storage collector, context,
capacity policy, CLI and release gates remain unchanged. The full lifecycle
controller is still unwired. Source tests simulate kernel/engine observations
and ownership; Linux exercises real temporary descriptor/flock lifetime and
contention. Windows results do not establish Linux flock behavior. No live
observation session, collector, fixture or lab execution was invoked.
See docs/evidence/grok-lab-session-source-20261114.json.


## Production deployment factoring (source preparation, 2026-11-14)

The production `deploy(folder, state, host, evidence)` entry now delegates to a
private `_deploy_transaction` entry. That entry calls the existing production
release gate once, rejects recognizable lab state/evidence/generation paths,
and only then constructs fixed `_ProductionDeployment` delegation. No caller
adapter, policy, image, path or context override is accepted. An observation
session receipt does not admit a deployment. These checks do not form a security
boundary against arbitrary Python execution or module monkeypatching.

The existing deployment algorithm remains in one transaction body. Persistence,
network audits, original/candidate checks, snapshotting, capacity and rollback
delegate to the existing production operations. Durable intent still precedes
consumer attachment and original stop; ambiguous candidate creation is recorded
before recovery; failures before the original stop recover network attachments
without restarting the original. Both snapshot peak checks and the final 64 GiB
production reserve remain unchanged. Expanding the fixed delegation reproduces
the original deployment AST exactly. Every other preexisting definition,
decorator and top-level statement is unchanged, including Host, preparation,
rollback, locks, NAS/hostname/path checks and the CLI.

This increment adds no lab lifecycle entry, runtime admission, collector call,
container or network action. Source tests reuse synthetic transaction fixtures;
they test gate/refusal ordering and durable recovery behavior, not authorization
to operate a real host. The existing 100 release tests remain unchanged. The
future lab path still requires genuine operator/hypervisor admission, allocated
isolated VM and console access, source/image/synthetic-material acceptance,
guest workload/network and storage checks, bounded working-fixture replacement
authority, and context-aware Host/capacity/snapshot/rollback integration. Only
after those seams are complete can a separate admitted lab dispatcher reuse the
transaction body. Production release/lint, H7/W16, independent Coordinator and
private backup gates remain open; this is not a deployment approval.


## Production recovery factoring (source preparation, 2026-11-14)

The public `rollback(folder, state, host)` entry now delegates to one private
`_rollback_transaction`. It rejects recognizable lab records and generation
paths before file or host effects, then constructs the same fixed production
delegation used by deploy. Recovery never invokes the deployment release gate
and does not need deployment-approval evidence; a failed deployment must remain
recoverable. The scope helper accepts omitted evidence only for this state/path
check. Deployment still performs its complete existing gate first and passes its
explicit evidence unchanged. No caller context, adapter or callback is accepted.

The rollback transaction never calls the dispatcher's `rollback` method, avoiding
recursive dispatch. The deploy recovery route remains: deploy transaction,
fixed rollback delegation, public rollback, private rollback transaction.
Existing dispatch methods are unchanged; three fixed methods delegate network
contract detection, configuration restoration and original-runtime validation.

Expanding fixed delegation reproduces the prior rollback algorithm AST exactly.
Network-only recovery before original stop preserves the running original.
Ambiguous candidate identity is recorded before candidate stop or network audit.
Only the retained original identity is restarted, or the frozen original
configuration recreates it. The recovered original ID is saved before reload or
later checks can fail. Concurrent configuration edits are retained; recovery
records the conflict and leaves consumer attachments intact before refusing final
acceptance. Genuine legacy recovery remains supported. No deployment approval
has been added as a prerequisite for emergency recovery.

Source tests exercise these branches through synthetic fixtures, including
explicit failure if recovery calls the deploy gate. The existing tests, deploy
transaction, Host, preparation, snapshot/capacity, CLI, locks and release gate
remain unchanged. No live recovery, observer/collector, lab fixture or production
operation is performed by this source work. The lab dispatcher, context-aware
Host/snapshot/rollback operations and independently verified lab admission remain
pending; the observation session grants no mutation permission. Allocation,
console access, working-fixture replacement authority and all existing release,
H7/W16, Coordinator and private backup gates remain open.


## Empty-engine bootstrap observations (source preparation, 2026-11-14)

The held read-only session now offers `observe_bootstrap_engine(peaks=...)`.
It refreshes storage around two Docker inventory samples and checks the owning
process/thread, guest boot, operation lock and retained context before returning.
The operation lock stays held. Collection failure invalidates the session; no
CLI, lab dispatcher, lifecycle operation or execution permit is introduced.

Only the fixed local Unix socket is used. Bounded allowlisted GET requests require
the root daemon peer and unchanged socket/engine identity. Bootstrap refuses any
container (including stopped/created fixtures), any volume, Swarm membership,
custom networks or endpoints. It requires the three built-in local networks and
inspects each by its full ID because modern network-list responses omit endpoints.
The three immutable image pins must be locally inspectable as Linux/amd64. Other
images are not inventoried; no claim is made about their absence or provenance.
Response framing, body/header/wire limits and existing per-request timeouts remain
in force. One 30-second HTTP deadline spans both samples and all their requests;
it is not an OS-level deadline for arbitrary filesystem metadata syscalls.

The sanitized result contains counts and identity/configuration digests with
`execution_admission=false`. Matching samples detect observed drift; they cannot
exclude transient changes or reserve the engine against other actors. The lock is
advisory. Host jobs/autostart, physical NIC isolation, shared devices/storage,
source and image provenance, operator scope and working-fixture replacement
authority remain separate requirements. This observer applies only before the
first fixtures exist. It never adopts, removes or stops retained resources, and
refusal does not authorize cleanup. Later retained-fixture admission is unfinished.

The HTTP extraction preserves the prior `/info` projection and framing/peer
checks. Factoring the session's storage refresh preserves its previous algorithm.
Production release/Host/CLI and release gates are unchanged. Tests use synthetic
inventory/socket/session fixtures; no live collector, image scan, container,
network, VM, backup or permission operation is performed by this preparation.
Lab allocation, console access, full admission and the shared lifecycle harness
remain pending. API review: [Docker Engine API v1.51](https://docs.docker.com/reference/api/engine/version/v1.51/).


## Initial synthetic fixture inputs (source preparation, 2026-11-14)

`network_lab_fixtures.original_fixture_inputs(context, lab_binding=...)` is a pure
generator returning five relative-path byte inputs: initial Compose, original
Nginx configuration, its Grok location fragment, a synthetic gateway configuration,
and a seed attributed to the full context digest. It validates the exact descriptor
and retained binding before and after generation. It reads no environment or files
and performs no process, Docker, socket, journal or admission operation.

The initial model has exactly the original Grok image and two pinned Nginx-image
services. Physical names and logical aliases come from the descriptor. Pulls and
automatic restarts are disabled, no ports are published, and no host networking,
privileged mode, external volume or Docker socket is introduced. Configuration
and writable log binds stay within the generation with implicit host-path creation
disabled. The original data is a named local Docker volume in separately observed
guest storage. The seed is only an input: a future admitted controller must install
it before startup. Candidate data remains a separate generation-contained bind.

Original routes actually proxy the bridge API on 8443 and noVNC on 6080, including
prefix stripping and WebSocket headers. Only `/fixture-health` is a synthetic
positive control. The marked original block can be replaced by the maintained
candidate renderer without changing the surrounding Nginx configuration. Gateway
responses accept fixed public synthetic admin/user cookies; other cookies receive
401. The gateway fixture models the role contract, not production authentication.

Tests exercise the actual network planner, candidate Compose renderer and Nginx
block renderer, including unchanged consumer settings and seed attribution across
contexts. They do not execute Compose resolution, nginx -t, containers or browsers.
Resource limits are recipe settings, not observed runtime capacity. A Docker
bridge with `internal: false` is not physical isolation. Original browser startup
can attempt provider traffic, so VM isolation and startup compatibility still need
independent proof. No TLS/browser/provider acceptance is claimed.

File materialization, seed installation, frozen-reference and probe fixtures,
retained-resource admission, context-aware Host/capacity/snapshot/recovery and the
full admitted harness remain unfinished. No existing production operator or lab
observer changes in this increment; the draft remains unmerged and undeployed.


## Auxiliary preservation/probe inputs (source preparation, 2026-11-15)

`network_lab_auxiliary.auxiliary_fixture_inputs(context, lab_binding=...)` returns
pure relative-path byte inputs for two separately owned Compose recipes, an idle
probe script and a context-attributed reference seed. Full descriptor/binding
consistency is required before and after construction. No environment, file,
process, Docker, socket, journal or admission operation occurs in the generator.
The initial working-service recipe and existing production/observation code remain
unchanged. These inputs confer no creation, startup, replacement or cleanup scope.

The preservation reference uses the exact original image pin, a separate Compose
ownership tuple and an independently seeded local named volume mounted read-only
with copy-up disabled. It uses network mode none, no published ports, restart no
and disabled inherited healthchecks. Its explicit isolated Python entrypoint
refuses execution with exit 78 before browser startup. The controller must still
never start it: even a successful refusal records a violation of the never-started
invariant. This is an original-image/synthetic-data preservation reference, not a
replica of original runtime/configuration or a verified initialized-data snapshot.
Sharing the working original volume would fail the existing exclusive-volume
check, which includes stopped and created consumers.

The separate probe recipe reuses the original image's Python with an explicit
stdlib-only idle entrypoint, bypassing browser startup and disabling inherited
healthchecks. It attaches only to the exact planned physical default network,
never the dedicated network, with no published ports or reserved working aliases.
That network's recipe declaration does not verify its runtime identity or grant
adoption permission. Script/configuration binds stay inside the exact generation,
read-only and with implicit host-path creation disabled. Named-volume backing is
separately observed guest Docker storage. Resource settings are declarative only.

No TCP measurement logic or arbitrary target override is provided. Later checks
must bind fresh candidate ID, dedicated-network ID, literal candidate IP and
reviewed ports. They need a positive control from the same probe and successful
candidate reachability from an authorized member. DNS failure when the candidate
leaves the default network is not evidence of TCP denial. Immutable container IDs,
network/volume identities, configuration and seed hashes require a separate
reviewed ownership ledger; existing strict release journals must not silently
adopt auxiliary resources.

Tests cover exact pins/names, full-binding refusal, detached deterministic outputs,
separate ownership/volumes, entrypoint/healthcheck/network constraints and unchanged
initial inputs. They validate the declared recipe and generated script contract,
not Docker Compose resolution, image execution, live TCP or browser/provider
acceptance. Inherited image configuration, including VOLUME/ENV metadata, is not
inspected: an omitted recipe mount is not proof of absent anonymous image volumes.
Materialization, seed installation, actual image/runtime compatibility, retained
resource admission, context-aware lifecycle integration and the full admitted
harness remain pending, along with VM/console and operator release requirements.

### Auxiliary intent and receipt consistency (source preparation)

`network_lab_auxiliary_state.py` adds two pure APIs:
`initial_state(context, *, lab_binding, transaction_id)` and
`validate_state(context, *, lab_binding, state, previous=None)`. They consume an
exact validated descriptor and caller-supplied full binding, and return detached
records. The fixed preparation binds the transaction, generated recipe/script/
seed hashes, separate reference/probe names and projects, and a borrowed default
network. Working lifecycle resources remain in their existing separate journal.

The bounded record contains at most 32 append-only events. JSON bytes are bounded
to 64 KiB; duplicate keys, nonfinite numbers, invalid recursive types, unexpected
fields and excessive structure are refused. Standalone validation checks only
the supplied history's self-consistency. Supplying `previous` additionally checks
one appended event, unchanged preparation and prior events, and the canonical
SHA-256 of that predecessor. The digest is neither a signature nor a successful
write receipt; neither mode observes disk, proves persistence or establishes
actual intent-before-effect ordering.

Volume reports progress through create intent, creation report, seed intent and
seed report. The manifest fixes a single synthetic `reference-generation.txt`
file at the reference volume root, with its exact generated size/hash. The
ordinary local volume has no immutable Docker ID; a returned name or successful
create can refer to an existing object. Its continuity remains explicitly
`unverified`, including after a seed report. No creation time, label, mountpoint,
serialized inode tuple or digest establishes fresh creation or durable ownership.
Live backing-directory/mount continuity, secure seed installation and independent
reconciliation after continuity loss or controller restart are still absent.

Docker references: [Engine API v1.51 Volume schema](https://docs.docker.com/reference/api/engine/version/v1.51/)
and [existing-name reuse](https://docs.docker.com/reference/cli/docker/volume/create/).

Container reports require matching full returned/inspected IDs; those IDs cannot
change within a predecessor-bound history. The reference accepts only a created,
never-started runtime report. Even its deliberate refusal exit 78 violates that
invariant. The probe has distinct create/start/stop intents and receipts, one
retained ID, fixed configuration/mount baselines and the exact borrowed default
network ID. Collision checks cover represented auxiliary container/network IDs
only; they do not inspect independent working-resource identities. Network
ownership, creation or deletion is never granted by the borrowed binding.

Unresolved outcomes and invariant violations can append bounded diagnostic codes,
evidence hashes and an optional observed container/network ID while retaining all
prior facts. They permanently close normal progression for that generation; no
automatic clear, retry, replacement or name/label adoption path exists. A changed
object can be recorded diagnostically without replacing the retained receipt.
Closing normal progression is a pure consistency rule, not a new emergency
recovery or stop-authorization gate. Any eventual recovery controller and volume
continuity re-establishment need separate review; this unwired module changes no
production recovery behavior.

All outputs retain `execution_admission=false`. Inspection/configuration hashes
are supplied reports, not evidence that their contents have been verified. Image
inheritance (including VOLUME/ENV/defaults), exact live mounts/network endpoints,
seed permissions and material provenance still require separate collectors and
checks. No raw inspection/environment/application error text is stored here.
The module performs no filesystem, daemon, process, network, persistence, cleanup
or authorization operation and supplies no CLI or mutation dispatcher. Future
controller code must persist/fsync intent before side effects, save attributable
receipts through reviewed CAS persistence, and satisfy the independent operator,
VM/hypervisor, source/image, storage and runtime admission requirements. Existing
production release, rollback, observer, backup and release-review gates are
unchanged. This component does not complete the recovery harness.

### Pinned image metadata observations (source preparation)

`network_lab_image_metadata.collect(context, *, lab_binding)` observes only the
three exact descriptor image pins through the existing fixed local Unix-socket
transport. It validates and detaches the context and full binding before I/O.
Two samples of engine information, three image inspections and engine information
use ten allowlisted GETs under one shared 30-second HTTP deadline, with the
existing shorter per-request, header, body and wire bounds. Root peer, socket,
engine identity, exact image IDs and Linux/amd64 platform must remain consistent.
Unlike bootstrap, this observation does not require or claim an empty engine.

The full bounded image Config is hashed, including absent/null distinctions.
The 13 reviewed ImageConfig fields and environment-entry syntax/types are checked,
including duplicate environment names. Unknown fields refuse rather than being
silently discarded. Configuration is bounded to 64 KiB serialized, 256-entry
arrays/maps, 4 KiB strings, and bounded depth/node counts.
Stable pin/platform/configuration projections must agree
between samples; mutable repository tags and last-tag metadata are excluded.
Receipts expose fixed hashes, counts and booleans rather than environment values,
commands, labels, private paths or raw daemon errors. Well-formed nonempty volume,
healthcheck and OnBuild declarations are facts to review, not compatibility
approval. Empty image declarations do not prove the absence of runtime anonymous
volumes or other filesystem effects. The Engine API describes ArgsEscaped as
Windows-only command metadata: boolean true is retained and does not itself
disqualify a Linux image; malformed types are refused.

`session.observe_pinned_image_metadata(*, peaks)` surrounds collection with fresh
storage observations while retaining the existing owner-process/thread, reentry,
guest/boot and operation-lock guards. Failures latch the session closed. Repeated
metadata observations have their own binding; the bootstrap binding and its
empty-engine requirements remain separate. Results are detached observations,
not journal updates or resource adoption. Matching samples and advisory locks do
not reserve the engine or exclude changes between observations. The HTTP deadline
does not impose a hard OS deadline on arbitrary filesystem calls.

Every result keeps execution_admission, compatibility_verified and
material_safety_verified false. Metadata does not inspect image filesystem
contents, execute the supplied Python, prove supply-chain approval or establish
resource ownership. Tests use synthetic transport responses and guarded-session
fixtures; they do not establish actual guest admission or runtime compatibility.
Production release/rollback, storage transport, bootstrap and auxiliary journal
contracts remain unchanged. Secure materialization, durable receipt persistence,
retained-object/volume continuity, context-aware lifecycle operations, the full
recovery harness and independent VM/operator/release gates remain pending.

Reference: [Docker Engine API v1.51 ImageConfig](https://docs.docker.com/reference/api/engine/version/v1.51/).

### Auxiliary filesystem persistence (source preparation)

`network_lab_auxiliary_store.py` implements a separate, bounded `load` / `save`
API for the existing auxiliary state schema. It has no CLI, controller dispatch,
Docker calls, provisioning or connection to the read-only observation session.
The production and working release journals and their recovery gates remain
unchanged. These are source and temporary-filesystem tests; the API has not been
invoked on a real retained generation and does not admit a lab or deployment.

The full context derives the one allowed generation below
`/var/lib/codex-security-lab/grok-network-lifecycle/`. Ancestors and the private
generation must already exist on writable local ext4/xfs. Held directory
descriptors pin ancestry and mount observations. The four literal leaves are
`auxiliary-state.lab.private.json`, `.auxiliary-state.lab.private.pending`,
`.auxiliary-state.lab.lock`, and the prohibited opposite-mode
`auxiliary-state.private.json`. The root-owned 0600 auxiliary lock must already
exist and is acquired with a nonblocking exclusive flock. Future controllers
must acquire the operation lock before this separate auxiliary lock. No lock is
reopened inside the existing observation session, and no directory, permission,
ACL or existing pending artifact is provisioned, changed, adopted or cleaned up.

Initial publication accepts only an empty planned auxiliary state. Existing
journals, pending leaves and opposite-mode leaves refuse initialization.
Updates require a detached receipt bound to the actual current raw bytes, file
identity, full context and transaction, followed by validation of exactly one
event against the actual stored predecessor. Raw-file SHA-256 and the canonical
predecessor digest have separate purposes; matching logical JSON does not make
a stale raw-file receipt current. A receipt is a consistency condition, not a
capability or proof of an attributable Docker create operation.

Persistence uses a fixed exclusive no-follow pending file, a complete bounded
write, file fsync, exact readback, directory fsync, repeated context/lock/current
and pending checks, then descriptor-relative publication. Initial publication
uses Linux `renameat2(RENAME_NOREPLACE)` and refuses unsupported behavior without
an overwrite fallback. Updates use atomic replacement while holding the
auxiliary lock. A further directory fsync and exact final readback precede the
returned save receipt. Failed staging is retained. Failure once publication may
have occurred returns an explicit uncertain-commit error without an advanced
receipt, retry, write-back rollback or inferred success. Caller states and prior
receipts remain unchanged.

Cold `load` reports a validated historical record and a read receipt. It cannot
claim that a prior save finished its fsync sequence, resume an external effect,
attribute resource ownership or restore volume continuity. File and directory
fsync completion is not a power-loss-tested recovery result or an off-site
backup. The auxiliary flock coordinates cooperating writers: compare then
replace is not atomic content CAS against an uncooperative or root writer, and
observed ancestry/mount identity does not establish physical storage provenance.

Tests use synthetic state and temporary Linux filesystem descriptors, real
flock and rename operations, with narrowly simulated ownership/mount
observations. They exercise bounds, no-clobber initialization, raw-byte and
identity conflicts, one-event updates, retained pending files, and injected
write/fsync/rename/readback failures. Windows tests explicitly skip Linux
filesystem mechanics. No retained production path is a test target.

Secure materialization, attributable retained resources, volume continuity and
restart reconciliation, lifecycle integration and the full recovery harness
remain unfinished. Disconnected VM/console access, operator scope and
physical/material/runtime admission are still required. Global release/lint,
H7/W16, Coordinator and private-backup gates remain open.

Filesystem semantics reviewed against [Python descriptor-relative replacement](https://docs.python.org/3.10/library/os.html#os.replace),
[Linux rename flags](https://man7.org/linux/man-pages/man2/rename.2.html), and
[separate file/directory synchronization](https://man7.org/linux/man-pages/man2/fsync.2.html).

### Fresh-generation synthetic file staging (source preparation)

`network_lab_materialize.materialize(context, lab_binding=...)` stages only the
existing deterministic fixture recipes. It has no CLI, Docker operation, volume
installer, journal initialization, cleanup or execution permit. Its root,
ancestors, empty exact generation and host operation/storage locks must already
be provisioned separately. This source has only been exercised in temporary
filesystem tests, never on a retained generation or an allocated lab.

Staging directly acquires the existing operation lock as a separate phase. It
must not be called inside `observation_session`, reacquire that session's lock or
turn the read-only session into a mutation interface. It pins the observed guest
UUID/boot, owning process/thread, lock identities, full context and held local
ext4/xfs ancestry. Identity consistency is not VM eligibility, physical isolation,
source/image provenance or operator authority. Later live observations, capacity,
empty-engine bootstrap and admission must be established afresh after staging;
no returned receipt preserves authority across release of the operation lock.

The installer accepts no caller-selected path or file content. It derives nine
files from `original_fixture_inputs` and `auxiliary_fixture_inputs`, validates
their exact path membership and byte bounds, and requires a completely empty
generation. It creates exactly these nine child directories:

```text
configuration/
configuration/nginx/
configuration/nginx/projects-locations.d/
configuration/gateway/
configuration/auxiliary/
seed/
runtime/
runtime/nginx-logs/
runtime/llm-proxy2-logs/
```

The nine generated files are the working Compose file, both Nginx configurations,
the Grok location fragment, reference/probe Compose files, the idle probe script,
and the original/reference synthetic seed inputs. Three additional fixed leaves
are `.materialization.lab.intent.json`, the empty `.auxiliary-state.lab.lock`,
and `.materialization.lab.complete.json`. Existing auxiliary, working and
production state/journals remain untouched.

The bounded intent binds the full context and expected paths/modes/lengths/hashes.
It is exclusively created, fsynced and read back before any child changes. Each
child operation uses literal basenames relative to held directory descriptors;
new directories must be private root0700 and regular files root0600/single-link.
There is no recursive path fallback, permission repair, overwrite or adoption.
Complete bounded writes, file fsync, pre-open private regular-file validation,
exact readback and identity/mount guards precede a bounded exact-tree check.
Both log directories must still be empty. New directory entries receive separate
directory fsync before the completion record is written and verified last.

The record is non-self-referential and does not assert that its own subsequent
fsync has already completed. Only the returned receipt can acknowledge completed
synchronization and final readback, with `execution_admission=false`. A partial
tree or ambiguous completion returns no successful receipt and is retained for
review. Every later invocation refuses the nonempty generation, including a
matching prior success or its own interrupted output. There is no automatic
resume, retry, rollback or cleanup. A cold completion file cannot establish that
an earlier caller observed successful acknowledgment, and the whole tree is not
published atomically against other observers.

The two seed files remain ordinary generation inputs. This API does not locate,
create, mount, seed or adopt either named Docker volume or its backing directory.
Attributable volume/container/network creation, backing-storage continuity and
independent interrupted-state reconciliation remain separate work. File modes
may still be incompatible with an image's runtime user; image/runtime acceptance
remains necessary and must not be replaced by permission broadening.

Tests use actual temporary Linux FD/flock/mkdir/fsync mechanics with narrow
ownership/mount/guest observation simulation. They cover exact generated bytes,
existing and interrupted output refusal, link/identity/mount/lock/context drift,
partial writes and synchronization/completion failures. They do not prove physical
storage provenance, power-loss recovery, off-site backup or live deployment.
Operation locks remain advisory, and arbitrary filesystem syscalls are not
wall-clock bounded. Production release/rollback gates and the read-only session
are unchanged; VM/console/operator scope, full lifecycle harness, global
release/lint, H7/W16, Coordinator and private-backup gates remain open.

Reviewed API semantics: [Python directory-relative mkdir](https://docs.python.org/3.10/library/os.html#os.mkdir),
[Linux exclusive/no-follow open](https://man7.org/linux/man-pages/man2/open.2.html),
and [separate file/directory fsync](https://man7.org/linux/man-pages/man2/fsync.2.html).
