# Gateway UID migration tools (review candidate)

These tools are a candidate for review, not an authorization or a fleet-wide
UID allocator. `examples/uid640-migration.json` is synthetic. Before use, independently
review the exact scripts and record their SHA-256 hashes. Install their reviewed
bytes to a root-owned, non-group/world-writable directory such as
`/opt/gateway-uid-migration/tools`; verify that directory and every ancestor
are root owned, are real directories (no symlinks), and are not group/world
writable. Install `tools/__init__.py` and the three `tools/uid640_*.py` files as root-owned mode 0644, and
verify the installed files against the previously recorded review hashes with
`/usr/bin/sha256sum` **after installation and before execution**. Do not run a
root Python process that imports code from a user-writable source checkout.

Make a separate private JSON configuration outside Git and image contexts. Set
it root owned and mode 0600. Every parent directory of the configuration, backup
and manifest directories must be root owned and not writable by group or
others; backup and manifest directories must already exist. An ordinary
user-owned backup directory or `/var/tmp` does not satisfy this rule. Fill
every field from an independently verified inventory. Exactly two local Docker
volumes are expected.

Run using isolated Python with site packages disabled and an explicit trusted module path. Replace the
placeholders with the private configuration and exact container and image IDs:

```
sudo /usr/bin/python3 -I -S -c 'import runpy,sys; sys.path.insert(0,"/opt/gateway-uid-migration"); runpy.run_module("tools.uid640_backup",run_name="__main__")' /root/private-config.json CONTAINER_ID IMAGE_ID
sudo /usr/bin/python3 -I -S -c 'import runpy,sys; sys.path.insert(0,"/opt/gateway-uid-migration"); runpy.run_module("tools.uid640_ownership",run_name="__main__")' /root/private-config.json apply CONTAINER_ID IMAGE_ID
sudo /usr/bin/python3 -I -S -c 'import runpy,sys; sys.path.insert(0,"/opt/gateway-uid-migration"); runpy.run_module("tools.uid640_ownership",run_name="__main__")' /root/private-config.json rollback CONTAINER_ID IMAGE_ID MANIFEST
sudo /usr/bin/python3 -I -S -c 'import runpy,sys; sys.path.insert(0,"/opt/gateway-uid-migration"); runpy.run_module("tools.uid640_ownership",run_name="__main__")' /root/private-config.json rollback-absent MANIFEST
```

The Docker executable is fixed to `/usr/bin/docker` and must be a root-owned,
non-symlink regular file under root-controlled ancestors. The tools connect only
to `unix:///var/run/docker.sock` with a restricted environment; inherited Docker
contexts, hosts and config paths are not used. The host must use this local
socket and standard executable, or the tools fail closed.

Backup expects the gateway running; ownership changes expect the exact gateway
stopped with restart policy `no`. The ownership manifest is private, remains
under the configured manifest parent, and is required for rollback. A rollback
refuses changed roots, configuration, inodes, contents list, owners or modes and
never restores database contents. Coordinate writers and Docker operators: these
checks cannot prevent a concurrent actor from starting a container or changing
files after a check. Validate independently, rehearse restoration, stop on
discrepancies and account for post-cutover writes.

The tools check container names and Compose service and project labels, volume
names, local drivers and mountpoints against the configuration. They do not
check upstream authentication, external writers, fleet-wide identity
reservation, retention of other volumes or full service health. The backup uses
SQLite's online backup API and normal named-path WAL/SHM handling, pins the
source inode, checks SQLite opened that inode, rechecks the name and checks the
copied database. These checks do not defend against a hostile actor replacing
and restoring a path during the operation; exclude untrusted writers.

## Isolated verification

The configuration, backup and ownership tests require root because they test
real numeric ownership on disposable files. Run them only in an isolated
container with a private root-owned temporary directory and `TMPDIR` pointing
to that directory; a world-writable `/tmp` intentionally fails the custody
checks. Ordinary non-root runs skip these tests. Mocked Docker tests do not
replace a host-specific rehearsal or an actual restore test.
