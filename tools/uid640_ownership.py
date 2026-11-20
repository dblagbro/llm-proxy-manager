"""Prepare a reversible ownership change for the stopped gateway volumes.

This tool changes only the two named gateway volumes. It refuses mixed owners,
links, special files, mount crossings, special mode bits and extended attributes.
The root-only manifest is durable before any ownership changes are attempted.
Rollback refuses a changed file set or inode and does not restore database data.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.uid640_config import load, trusted_directory

# Loaded only from a validated private configuration in main().
CFG = None
OLD = None
NEW = None
ROOTS = ()


def config_digest():
    data = {
        "host": CFG.host,
        "container": CFG.container,
        "service": CFG.service,
        "project": CFG.project,
        "volume_names": CFG.volume_names,
        "roots": [str(p) for p in CFG.roots],
        "destinations": CFG.destinations,
        "old": CFG.old,
        "new": CFG.new,
        "backup_parent": str(CFG.backup_parent),
        "manifest_parent": str(CFG.manifest_parent),
        "db_name": CFG.db_name,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def trusted_docker():
    path = Path("/usr/bin/docker")
    trusted_directory(path.parent)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Docker executable is not root-controlled")


def docker_output(args):
    trusted_docker()
    return subprocess.check_output(
        ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", *args[1:]],
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/root"},
    )


def check_volumes():
    raw = docker_output(["docker", "volume", "inspect", *CFG.volume_names])
    volumes = json.loads(raw)
    actual = {(v.get("Name"), v.get("Driver"), v.get("Mountpoint")) for v in volumes}
    expected = {
        (name, "local", str(root)) for name, root in zip(CFG.volume_names, CFG.roots, strict=True)
    }
    if len(volumes) != 2 or actual != expected:
        raise RuntimeError("Docker volume identity mismatch")


def inventory(roots: tuple[Path, ...]) -> list[dict]:
    result = []
    for root in roots:
        base = root.lstat()
        if not stat.S_ISDIR(base.st_mode) or root.resolve() != root:
            raise RuntimeError("invalid volume root")
        pending = [root]
        while pending:
            path = pending.pop()
            item = path.lstat()
            directory = stat.S_ISDIR(item.st_mode)
            if not (directory or stat.S_ISREG(item.st_mode)):
                raise RuntimeError("link or special file found")
            if item.st_dev != base.st_dev or (not directory and item.st_nlink != 1):
                raise RuntimeError("mount crossing or hardlink found")
            if item.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
                raise RuntimeError("special mode bits found")
            if os.listxattr(path, follow_symlinks=False):
                raise RuntimeError("extended attributes found")
            result.append(
                {
                    "path": str(path),
                    "device": item.st_dev,
                    "inode": item.st_ino,
                    "mode": stat.S_IMODE(item.st_mode),
                    "directory": directory,
                    "uid": item.st_uid,
                    "gid": item.st_gid,
                }
            )
            if directory:
                with os.scandir(path) as entries:
                    pending.extend(Path(entry.path) for entry in entries)
    return sorted(result, key=lambda item: item["path"])


def change(
    roots: tuple[Path, ...],
    expected: list[dict],
    target: tuple[int, int],
    allowed: set[tuple[int, int]],
) -> None:
    actual = inventory(roots)
    if len(actual) != len(expected):
        raise RuntimeError("file set changed")
    descriptors = []
    try:
        for current, prior in zip(actual, expected, strict=True):
            keys = ("path", "device", "inode", "mode", "directory")
            if any(current[key] != prior[key] for key in keys):
                raise RuntimeError("file identity or mode changed")
            if (current["uid"], current["gid"]) not in allowed:
                raise RuntimeError("unexpected owner")
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if current["directory"]:
                flags |= os.O_DIRECTORY
            fd = os.open(current["path"], flags)
            descriptors.append(fd)
            info = os.fstat(fd)
            if (
                info.st_dev,
                info.st_ino,
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
            ) != (
                current["device"],
                current["inode"],
                current["mode"],
                current["uid"],
                current["gid"],
            ):
                raise RuntimeError("file changed while opening")
            if not current["directory"] and info.st_nlink != 1:
                raise RuntimeError("file link count changed")
            if os.listxattr(fd):
                raise RuntimeError("file attributes changed")
        if inventory(roots) != actual:
            raise RuntimeError("file set changed before ownership update")
        # All identities are validated before the first mutation. The durable
        # manifest supports recovery if a later fchown or process is interrupted.
        for fd in descriptors:
            os.fchown(fd, *target)
        after = inventory(roots)
        keys = ("path", "device", "inode", "mode", "directory")
        if len(after) != len(expected) or any(
            any(current[key] != prior[key] for key in keys)
            or (current["uid"], current["gid"]) != target
            for current, prior in zip(after, expected, strict=True)
        ):
            raise RuntimeError("ownership verification failed")
    finally:
        for fd in descriptors:
            os.close(fd)


def check_host() -> None:
    if os.geteuid() != 0 or CFG is None or socket.gethostname() != CFG.host:
        raise RuntimeError("unexpected account or host")
    options = json.loads(docker_output(["docker", "info", "--format", "{{json .SecurityOptions}}"]))
    if any("userns" in option.lower() for option in options):
        raise RuntimeError("Docker user namespace remapping is not supported")
    check_volumes()


def check_database_integrity() -> None:
    db_path = ROOTS[0] / CFG.db_name
    if not stat.S_ISREG(db_path.lstat().st_mode):
        raise RuntimeError("database identity mismatch after failed exit")
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True, timeout=20) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("database integrity failed after failed exit")


def check_volume_users(excluded_id: str | None) -> None:
    ids = docker_output(["docker", "ps", "-aq", "--no-trunc"]).split()
    if excluded_id is not None and excluded_id not in ids:
        raise RuntimeError("gateway absent from Docker inventory")
    others = [item for item in ids if item != excluded_id]
    if others:
        inventory_json = docker_output(["docker", "inspect", *others])
        containers = json.loads(inventory_json)
        if {item["Id"] for item in containers} != set(others):
            raise RuntimeError("incomplete Docker inventory")
        for other in containers:
            name = other.get("Name")
            if not isinstance(name, str) or not name.startswith("/"):
                raise RuntimeError("unknown container name")
            if name == "/" + CFG.container:
                raise RuntimeError("gateway exists during absent-container recovery")
            config = other.get("Config")
            if not isinstance(config, dict):
                raise TypeError("unknown container configuration")
            labels = config.get("Labels")
            if labels is None:
                labels = {}
            if not isinstance(labels, dict):
                raise TypeError("unknown container labels")
            same_service = (
                labels.get("com.docker.compose.service") == CFG.service
                and labels.get("com.docker.compose.project") == CFG.project
            )
            if same_service:
                state = other.get("State")
                if (
                    not isinstance(state, dict)
                    or state.get("Running") is not False
                    or state.get("Status") not in {"created", "exited", "dead"}
                    or state.get("Paused") is not False
                    or state.get("Restarting") is not False
                ):
                    raise RuntimeError("same-service container running or state unknown")
            if not isinstance(other.get("Mounts"), list):
                raise TypeError("unknown container mounts")
            for mount in other["Mounts"]:
                if not isinstance(mount, dict):
                    raise TypeError("unknown mount identity")
                source = mount.get("Source")
                if mount.get("Name") in set(CFG.volume_names):
                    raise RuntimeError("another container references a gateway volume")
                if source is not None and not isinstance(source, str):
                    raise TypeError("unknown mount source")
                kind = mount.get("Type")
                if kind not in {"bind", "volume", "tmpfs"}:
                    raise RuntimeError("unsupported or missing mount type")
                if kind in {"bind", "volume"} and (not source or not Path(source).is_absolute()):
                    raise RuntimeError("unknown mounted source")
                if kind == "volume" and (
                    not isinstance(mount.get("Name"), str) or not mount["Name"]
                ):
                    raise RuntimeError("unknown volume name")
                if kind == "tmpfs" and source not in (None, ""):
                    raise RuntimeError("unexpected tmpfs source")
                if source and Path(source).is_absolute():
                    resolved = Path(source).resolve()
                    if any(
                        resolved.is_relative_to(root) or root.is_relative_to(resolved)
                        for root in ROOTS
                    ):
                        raise RuntimeError("another container binds a gateway volume")


def stopped_gateway(container_id: str, image_id: str, *, allow_failed_exit: bool = False) -> None:
    check_host()
    raw = docker_output(["docker", "inspect", CFG.container])
    container = json.loads(raw)[0]
    mounts = {(m.get("Name"), m.get("Source"), m.get("Destination")) for m in container["Mounts"]}
    expected = {
        (name, str(root), destination)
        for name, root, destination in zip(CFG.volume_names, ROOTS, CFG.destinations, strict=True)
    }
    if (
        container["Id"] != container_id
        or container["Image"] != image_id
        or container["State"]["Running"]
        or not isinstance(container["State"].get("ExitCode"), int)
        or (container["State"].get("ExitCode") != 0 and not allow_failed_exit)
        or container["Config"].get("User") != "appuser"
        or container["HostConfig"].get("UsernsMode") not in ("", None)
        or container["HostConfig"].get("RestartPolicy", {}).get("Name") != "no"
        or mounts != expected
        or any(m.get("Type") != "volume" or not m.get("RW") for m in container["Mounts"])
        or container["Config"].get("Labels", {}).get("com.docker.compose.service") != CFG.service
        or container["Config"].get("Labels", {}).get("com.docker.compose.project") != CFG.project
    ):
        raise RuntimeError("stopped gateway identity or mounts mismatch")
    if container["State"].get("ExitCode") != 0:
        check_database_integrity()
    check_volume_users(container_id)


def absent_gateway() -> None:
    check_host()
    check_volume_users(None)
    check_database_integrity()


def apply(roots: tuple[Path, ...], parent: Path, guard=None) -> Path:
    entries = inventory(roots)
    if any((item["uid"], item["gid"]) != OLD for item in entries):
        raise RuntimeError("unexpected initial owner")
    directory = Path(tempfile.mkdtemp(prefix="llmproxy-uid640-ownership-", dir=parent))
    manifest = directory / "manifest.json"
    with manifest.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(
            {
                "roots": [str(root) for root in roots],
                "entries": entries,
                "config_sha256": config_digest(),
            },
            stream,
        )
        stream.flush()
        os.fsync(stream.fileno())
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    print("prepared_manifest=" + str(manifest), flush=True)
    try:
        if guard is not None:
            guard()
        change(roots, entries, NEW, {OLD})
        if guard is not None:
            guard()
    except BaseException:
        # A failed recovery remains an explicit operator-visible error; the
        # manifest is never deleted and can be used for a later rollback.
        if guard is not None:
            guard()
        change(roots, entries, OLD, {OLD, NEW})
        raise
    return manifest


def rollback(roots: tuple[Path, ...], manifest: Path, guard=None) -> None:
    if (
        not manifest.is_absolute()
        or manifest.name != "manifest.json"
        or manifest.parent.parent != CFG.manifest_parent
        or not manifest.parent.name.startswith("llmproxy-uid640-ownership-")
    ):
        raise RuntimeError("manifest outside configured parent")
    trusted_directory(CFG.manifest_parent)
    base_fd = os.open(CFG.manifest_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        base_info = os.fstat(base_fd)
        if base_info.st_uid != 0 or base_info.st_mode & 0o022:
            raise RuntimeError("manifest parent custody mismatch")
        directory_fd = os.open(
            manifest.parent.name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=base_fd,
        )
        try:
            info = os.fstat(directory_fd)
            if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
                raise RuntimeError("manifest directory custody mismatch")
            file_fd = os.open(manifest.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
            try:
                info = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != 0
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise RuntimeError("manifest file custody mismatch")
                with os.fdopen(file_fd, "r", encoding="utf-8") as stream:
                    file_fd = -1
                    data = json.load(stream)
            finally:
                if file_fd != -1:
                    os.close(file_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(base_fd)
    if data.get("config_sha256") != config_digest() or data.get("roots") != [
        str(root) for root in roots
    ]:
        raise RuntimeError("manifest roots mismatch")
    if guard is not None:
        guard()
    change(roots, data["entries"], OLD, {OLD, NEW})
    if guard is not None:
        guard()


def main() -> None:
    global CFG, OLD, NEW, ROOTS
    if os.geteuid() != 0 or len(sys.argv) < 3:
        raise RuntimeError("root required; usage: CONFIG apply|rollback|rollback-absent ...")
    CFG = load(Path(sys.argv[1]))
    OLD, NEW, ROOTS = CFG.old, CFG.new, CFG.roots
    args = sys.argv[2:]
    if len(args) == 2 and args[0] == "rollback-absent":
        absent_gateway()
        rollback(ROOTS, Path(args[1]), absent_gateway)
        print("ownership restored")
        return
    if len(args) not in (3, 4) or args[0] not in ("apply", "rollback"):
        raise RuntimeError(
            "usage: CONFIG apply CONTAINER_ID IMAGE_ID | "
            "CONFIG rollback CONTAINER_ID IMAGE_ID MANIFEST"
        )
    action, container_id, image_id = args[:3]
    if (action == "apply") != (len(args) == 3):
        raise RuntimeError("invalid arguments")
    stopped_gateway(container_id, image_id, allow_failed_exit=action == "rollback")
    if action == "apply":
        manifest = apply(
            ROOTS, CFG.manifest_parent, lambda: stopped_gateway(container_id, image_id)
        )
        print("ownership migrated; manifest=" + str(manifest))
    else:
        rollback(
            ROOTS,
            Path(args[3]),
            lambda: stopped_gateway(container_id, image_id, allow_failed_exit=True),
        )
        print("ownership restored")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print("ownership operation failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
