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
