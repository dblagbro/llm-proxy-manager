"""Back up the configured gateway's live SQLite database without exposing contents."""

import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path

from tools import uid640_ownership as ownership
from tools.uid640_config import load


def main():
    if os.geteuid() != 0 or len(sys.argv) != 4:
        raise RuntimeError("root required; usage: CONFIG CONTAINER_ID IMAGE_ID")
    cfg = load(Path(sys.argv[1]))
    ownership.CFG, ownership.ROOTS, ownership.OLD, ownership.NEW = (
        cfg,
        cfg.roots,
        cfg.old,
        cfg.new,
    )
    ownership.check_host()
    raw = ownership.docker_output(["docker", "inspect", cfg.container])
    container = json.loads(raw)[0]
    mounts = {
        (m.get("Name"), m.get("Source"), m.get("Destination"))
        for m in container["Mounts"]
    }
    expected = {
        (name, str(root), dst)
        for name, root, dst in zip(
            cfg.volume_names, cfg.roots, cfg.destinations, strict=True
        )
    }
    labels = container["Config"].get("Labels", {})
    if (
        container["Id"] != sys.argv[2]
        or container["Image"] != sys.argv[3]
        or not container["State"]["Running"]
        or container["Config"].get("User") != "appuser"
        or labels.get("com.docker.compose.service") != cfg.service
        or labels.get("com.docker.compose.project") != cfg.project
        or container["HostConfig"].get("UsernsMode") not in ("", None)
        or mounts != expected
        or any(
            m.get("Type") != "volume" or not m.get("RW") for m in container["Mounts"]
        )
    ):
        raise RuntimeError("running gateway identity or mounts mismatch")
    source = cfg.roots[0] / cfg.db_name
    info = source.lstat()
    if not stat.S_ISREG(info.st_mode) or (info.st_uid, info.st_gid) != cfg.old:
        raise RuntimeError("source identity or ownership mismatch")
    directory = Path(
        tempfile.mkdtemp(prefix="gateway-uid-backup-", dir=cfg.backup_parent)
    )
    destination = directory / cfg.db_name
    fd = os.open(
        destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
    )
    os.close(fd)
    # Preserve the named database path so SQLite locates its real WAL and SHM.
    # Pin the inode and verify that SQLite opened the same inode; revalidate the
    # name after backup. Coordination still excludes hostile concurrent writers.
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    src = None
    dst = None
    try:
        pinned = os.fstat(source_fd)
        if (
            not stat.S_ISREG(pinned.st_mode)
            or (pinned.st_uid, pinned.st_gid) != cfg.old
            or (pinned.st_dev, pinned.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise RuntimeError("source changed before opening")
        before_fds = set()
        for item in os.listdir("/proc/self/fd"):
            try:
                os.fstat(int(item))
            except OSError:
                continue
            before_fds.add(item)
        src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=30)
        new_fds = set(os.listdir("/proc/self/fd")) - before_fds
        matching = False
        for item in new_fds:
            try:
                fd_info = os.fstat(int(item))
            except OSError:
                continue
            if (fd_info.st_dev, fd_info.st_ino) == (pinned.st_dev, pinned.st_ino):
                matching = True
        if not matching:
            raise RuntimeError("SQLite did not open the pinned database inode")
        dst = sqlite3.connect(destination.as_uri() + "?mode=rw", uri=True, timeout=30)
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("backup integrity check failed")
        after = source.lstat()
        if not stat.S_ISREG(after.st_mode) or (
            after.st_dev,
            after.st_ino,
            after.st_uid,
            after.st_gid,
        ) != (pinned.st_dev, pinned.st_ino, *cfg.old):
            raise RuntimeError("source changed during backup")
    finally:
        if dst is not None:
            dst.close()
        if src is not None:
            src.close()
        os.close(source_fd)
    digest = hashlib.sha256()
    with destination.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        os.fsync(stream.fileno())
    for path in (directory, cfg.backup_parent):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    print("backup=" + str(destination))
    print("size=" + str(destination.stat().st_size))
    print("sha256=" + digest.hexdigest())
    print("integrity=ok")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print("backup failed: " + str(exc), file=sys.stderr)
        sys.exit(1)
