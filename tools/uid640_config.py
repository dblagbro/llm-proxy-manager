"""Validate a private, explicit host configuration for gateway UID migration."""

from __future__ import annotations

import json
import os
import re
import socket
import stat
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    host: str
    container: str
    service: str
    project: str
    volume_names: tuple[str, str]
    roots: tuple[Path, Path]
    destinations: tuple[str, str]
    old: tuple[int, int]
    new: tuple[int, int]
    backup_parent: Path
    manifest_parent: Path
    db_name: str


def trusted_directory(path: Path) -> None:
    """Check each ancestor without following symlinks or trusting writable parents."""
    if not path.is_absolute() or ".." in path.parts:
        raise RuntimeError("directory must be absolute")
    for parent in (path, *path.parents):
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022:
            raise RuntimeError("directory is not root-controlled")


def absolute_path(value: object) -> Path:
    if not isinstance(value, str):
        raise TypeError("path must be a string")
    p = Path(value)
    if not p.is_absolute() or ".." in p.parts or p == Path("/"):
        raise RuntimeError("path must be absolute and bounded")
    return p


def identifier(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value
    ):
        raise RuntimeError("invalid identifier")
    return value


def pair(value: object) -> tuple[int, int]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or any(type(x) is not int or x <= 0 for x in value)
    ):
        raise RuntimeError("invalid UID/GID pair")
    return value[0], value[1]


def load(path: Path) -> Config:
    trusted_directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise RuntimeError("configuration must be root-owned mode 0600")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            data = json.load(stream)
    finally:
        if fd != -1:
            os.close(fd)
    required = {
        "host",
        "container",
        "service",
        "project",
        "volumes",
        "old",
        "new",
        "backup_parent",
        "manifest_parent",
        "db_name",
    }
    if not isinstance(data, dict) or set(data) != required:
        raise RuntimeError("unexpected configuration fields")
    volumes = data["volumes"]
    if (
        not isinstance(volumes, list)
        or len(volumes) != 2
        or any(
            not isinstance(v, dict) or set(v) != {"name", "root", "destination"}
            for v in volumes
        )
    ):
        raise RuntimeError("exactly two explicit volumes required")
    roots = tuple(absolute_path(v["root"]) for v in volumes)
    names = tuple(identifier(v["name"]) for v in volumes)
    destinations = tuple(str(absolute_path(v["destination"])) for v in volumes)
    if (
        len(set(roots)) != 2
        or len(set(names)) != 2
        or len(set(destinations)) != 2
        or any(a in b.parents for a, b in (roots, roots[::-1]))
    ):
        raise RuntimeError("overlapping volumes")
    old, new = pair(data["old"]), pair(data["new"])
    if old == new:
        raise RuntimeError("old and new ownership must differ")
    backup = absolute_path(data["backup_parent"])
    manifests = absolute_path(data["manifest_parent"])
    trusted_directory(backup)
    trusted_directory(manifests)
    cfg = Config(
        identifier(data["host"]),
        identifier(data["container"]),
        identifier(data["service"]),
        identifier(data["project"]),
        names,
        roots,
        destinations,
        old,
        new,
        backup,
        manifests,
        identifier(data["db_name"]),
    )
    if socket.gethostname() != cfg.host:
        raise RuntimeError("host mismatch")
    return cfg
