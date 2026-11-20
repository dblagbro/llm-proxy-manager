# Gateway runtime UID migration

The gateway runtime account lives inside the image. Docker needs numeric ownership
on persistent storage; deployment does not create a corresponding user on the Docker
host. A site-operated emergency SSH bastion has a separate lifecycle. If a future
deployment needs an SSH anchor, package it as an optional companion such as
`llm-proxy-ssh-tunnel-anchor`, with its own image-local identity and explicitly
provisioned keys. The standard gateway starts and serves requests independently.

The gateway image runs as UID/GID 640:640. Existing persistent volumes retain
their original numeric ownership when an image is replaced; operators must
migrate the data and log volumes explicitly before starting the new image.

Before any cutover, inventory the affected volumes, their consumers, backup
access, host identity allocation and user-namespace configuration. Preserve a
current, integrity-checked SQLite online backup and the previous image. Rehearse
with isolated copies, coordinate writers and service restarts, and verify that
the selected numeric IDs do not conflict in the target environment.

Migrate one node at a time. Stop only the gateway, preserve an ownership
manifest, change ownership only for the reviewed gateway volumes, and recreate
the service using the host's canonical Compose configuration. Verify health,
data access, persistence and application behavior before advancing to another
node. A rollback must account for writes made after cutover; restoring an older
database can discard those writes. Do not delete historical volumes merely
because they retain an old owner.

For distribution, record the source commit, release version and immutable image
digest. Validate a registry pull on an authorized test node before rolling the
same digest out to other nodes. Back up source/release artifacts separately from
per-node application data, and verify restore procedures. A digest identifies
the released image; rebuilding from source is not necessarily bit-for-bit
identical when base images or dependencies are not pinned.

The host-specific migration tools contain environment assumptions. Review and
adapt their guards to the actual inventory before use; they do not allocate host
accounts or make a fleet-wide UID reservation.

## Portable image check

Run `make test-gateway-container GATEWAY_IMAGE=<local-release-image>` on a Docker test
host. The check starts the image with its default user, isolated networking and fresh
anonymous data and log volumes. It verifies the gateway's health endpoint and effective
UID/GID, then replaces the container and checks that its original SQLite admin row
and a non-root log marker persist. It removes its disposable containers and volumes.
No host account, SSH key, forwarding service or model-provider credentials are involved.

## Release source and image

`tools/cut-release.sh` requires a clean worktree on its target branch and an
explicit reviewed Docker Hub repository, tested image ID, backup directory,
and health URL for each gateway. It stages
the committed tree for the image build and archives that same commit for the
source backup. It builds before creating the release tag, and refuses publication if the rebuilt image
does not match the tested ID. It publishes a versioned image without changing
`latest` or the local Compose image tag; promotion follows registry pull testing.
A successful build
does not replace the project's required lint, test, CI, publication review and
approval gates. Record the published image digest and verify the actual image
used by each Compose service; a mutable local tag alone is not evidence that a
node received the intended release.
