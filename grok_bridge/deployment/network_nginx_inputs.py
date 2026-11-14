"""Private, byte-identical copies of three reviewed Nginx configuration mounts.

This module never changes original modes/ownership/ACLs and never runs Docker.
Linux execution requires root; ancestor descriptors use NOFOLLOW and all
file/directory reads use NOATIME. Any interrupted copy is retained and
refuses reuse. The returned record belongs only in private evidence/ledgers.
Only the certificate tree permits relative leaf links to inventoried regular
files. Link atime is excluded because readlink can update it; no times are reset.
"""

import functools
import hashlib
import json
import os
import stat
import sys
from pathlib import Path, PurePosixPath

TARGETS = {
    "/etc/nginx/conf.d": "conf.d",
    "/etc/nginx/projects-locations.d": "projects-locations.d",
    "/etc/letsencrypt": "letsencrypt",
}
CERT_TARGET = "/etc/letsencrypt"
MAX_LINK_BYTES = 4096
COPY_ROOT = "nginx-include-inputs"
MANIFEST = "manifest.private.json"
MAX_BYTES = 8 * 1024 * 1024
MAX_ENTRIES = 256
MAX_DEPTH = 8


class NginxInputsError(RuntimeError):
    """Sanitized failure; preserve the entire private attempt for investigation."""


def _require(ok, message):
    if not ok:
        raise NginxInputsError(message)


def _sanitized(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except NginxInputsError:
            raise
        except Exception:  # noqa: BLE001 -- do not expose private paths or file content.
            raise NginxInputsError(
                "Private include snapshot failed; retain attempt for review"
            ) from None

    return wrapped


def _linux():
    return sys.platform == "linux"


def _path(value):
    path = Path(value)
    _require(
        path.is_absolute() and ".." not in path.parts,
        "Use a canonical absolute input path",
    )
    for parent in (*reversed(path.parents), path):
        _require(not parent.is_symlink(), "Symlink input ancestry is not allowed")
    _require(path.resolve() == path, "Input path resolution changed")
    return path


def _private_owner(info):
    if _linux():
        _require(
            info.st_uid == 0 and info.st_gid == 0,
            "Private inputs require root ownership",
        )


def _private(path, directory):
    info = _path(path).stat()
    _private_owner(info)
    expected = 0o700 if directory else 0o600
    _require(
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode),
        "Private input type differs",
    )
    if os.name == "posix":
        _require(stat.S_IMODE(info.st_mode) == expected, "Private input permissions differ")


def _folder(value):
    folder = _path(value)
    if _linux():
        _require(os.geteuid() == 0, "Private include preparation requires Linux root")
    _private(folder, True)
    return folder


def _flags(directory=False):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)
    if _linux():
        _require(hasattr(os, "O_NOATIME"), "No-atime input reads are required")
        flags |= os.O_NOATIME
    return flags


def _directory_fd(path):
    """On Linux bind every ancestor by descriptor, refusing symlink races."""
    path = _path(path)
    if not _linux():
        return None
    # Ancestors are opened but never enumerated. NOATIME is needed only on
    # the directory actually read; retaining it there also supports tests
    # running under the owner of synthetic trees in otherwise unprivileged CI.
    ancestor_flags = _flags(True) & ~os.O_NOATIME
    fd = os.open(path.anchor, ancestor_flags)
    try:
        parts = path.parts[1:]
        for index, name in enumerate(parts):
            flags = _flags(True) if index == len(parts) - 1 else ancestor_flags
            child = os.open(name, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _metadata(info):
    result = {
        key: getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
        )
    }
    if _linux():
        result["st_ctime_ns"] = info.st_ctime_ns
        result["st_atime_ns"] = info.st_atime_ns
    return result


def _relative(value):
    _require(type(value) is str and "\\" not in value, "Invalid relative include path")
    path = PurePosixPath(value)
    _require(
        value == ""
        or (
            not path.is_absolute()
            and str(path) == value
            and not any(part in {".", ".."} for part in path.parts)
        ),
        "Include path escapes its snapshot",
    )
    _require(len(path.parts) <= MAX_DEPTH, "Include depth exceeds reviewed bound")
    return path


def _link_metadata(info):
    value = _metadata(info)
    value.pop("st_atime_ns", None)
    return value


def _leaf_target(relative, text, entries):
    """Resolve lexically while requiring every traversed component to be a directory.

    Checking only normpath would incorrectly accept a symlink/.. intermediate.
    This never follows a source link or reads an uninventoried target.
    """
    _relative(relative)
    _require(
        type(text) is str and text and "\\" not in text and "\0" not in text,
        "Invalid certificate link text",
    )
    _require(len(os.fsencode(text)) <= MAX_LINK_BYTES, "Certificate link exceeds reviewed bound")
    _require(not PurePosixPath(text).is_absolute(), "Absolute certificate links are forbidden")
    _require(
        text.split("/")[-1] not in {"", ".", ".."},
        "Certificate link must end in a regular file name",
    )
    stack = list(PurePosixPath(relative).parent.parts)
    parts = [part for part in text.split("/") if part not in {"", "."}]
    for index, part in enumerate(parts):
        if part == "..":
            _require(bool(stack), "Certificate link escapes its tree")
            stack.pop()
        else:
            stack.append(part)
            row = entries.get("/".join(stack))
            kind = "directory" if index < len(parts) - 1 else "file"
            _require(
                type(row) is dict and row.get("kind") == kind,
                "Certificate links must traverse directories to one regular file",
            )
    target = "/".join(stack)
    _relative(target)
    _require(
        target and entries.get(target, {}).get("kind") == "file",
        "Certificate link target is not an inventoried regular file",
    )
    return target


def _check_links(entries, allow_links):
    for relative, row in entries.items():
        if row["kind"] == "symlink":
            _require(allow_links, "Symlink include entries are forbidden")
            _require(
                row.get("target") == _leaf_target(relative, row.get("link_text"), entries),
                "Certificate link target binding changed",
            )


def _read_leaf_link(path, name, fd, info, private):
    _require(0 <= info.st_size <= MAX_LINK_BYTES, "Certificate link exceeds reviewed bound")
    before = _link_metadata(info)
    if private:
        _private_owner(info)

    def link_text():
        return os.readlink(name, dir_fd=fd) if fd is not None else os.readlink(path)

    def link_stat():
        return os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else path.lstat()

    first = link_text()
    _require(_link_metadata(link_stat()) == before, "Certificate link changed during read")
    second = link_text()
    _require(
        first == second and _link_metadata(link_stat()) == before,
        "Certificate link identity or text changed",
    )
    _require(
        type(first) is str and len(os.fsencode(first)) <= MAX_LINK_BYTES,
        "Certificate link exceeds reviewed bound",
    )
    return {"kind": "symlink", "metadata": before, "link_text": first}


class _Budget:
    def __init__(self):
        self.entries = 0
        self.bytes = 0

    def entry(self):
        self.entries += 1
        _require(self.entries <= MAX_ENTRIES, "Include entry count exceeds reviewed bound")

    def file(self, size):
        _require(type(size) is int and size >= 0, "Invalid include size")
        self.bytes += size
        _require(self.bytes <= MAX_BYTES, "Include bytes exceed reviewed bound")


def _scan(root, budget, *, private=False, capture=False, allow_links=False):
    entries, contents = {}, {}
    root = _path(root)

    def walk(path, relative, fd):
        _relative(relative)
        budget.entry()
        info = os.fstat(fd) if fd is not None else path.lstat()
        _require(stat.S_ISDIR(info.st_mode), "Include tree root must be a directory")
        before = _metadata(info)
        if private:
            _private(path, True)
        entries[relative] = {"kind": "directory", "metadata": before}
        with os.scandir(fd if fd is not None else path) as iterator:
            names = []
            for entry in iterator:
                names.append(entry.name)
                _require(
                    len(names) + budget.entries <= MAX_ENTRIES,
                    "Include entry count exceeds reviewed bound",
                )
            names.sort()
        for name in names:
            child_rel = relative + "/" + name if relative else name
            _relative(child_rel)
            child_path = path / name
            info = (
                os.stat(name, dir_fd=fd, follow_symlinks=False)
                if fd is not None
                else child_path.lstat()
            )
            if stat.S_ISLNK(info.st_mode):
                _require(allow_links, "Symlink include entries are forbidden")
                budget.entry()
                row = _read_leaf_link(child_path, name, fd, info, private)
                budget.file(len(os.fsencode(row["link_text"])))
                entries[child_rel] = row
                continue
            if stat.S_ISDIR(info.st_mode):
                child_fd = os.open(name, _flags(True), dir_fd=fd) if fd is not None else None
                try:
                    if child_fd is not None:
                        _require(
                            _metadata(os.fstat(child_fd)) == _metadata(info),
                            "Include directory changed during traversal",
                        )
                    walk(child_path, child_rel, child_fd)
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
                continue
            _require(stat.S_ISREG(info.st_mode), "Only regular include files are permitted")
            budget.entry()
            budget.file(info.st_size)
            if private:
                _private(child_path, False)
            file_fd = (
                os.open(name, _flags(), dir_fd=fd)
                if fd is not None
                else os.open(_path(child_path), _flags())
            )
            with os.fdopen(file_fd, "rb") as stream:
                _require(
                    _metadata(os.fstat(stream.fileno())) == _metadata(info),
                    "Include file changed before read",
                )
                data = stream.read(info.st_size + 1)
                _require(len(data) == info.st_size, "Include file changed size during read")
                _require(
                    _metadata(os.fstat(stream.fileno())) == _metadata(info),
                    "Include metadata changed during read",
                )
            entries[child_rel] = {
                "kind": "file",
                "metadata": _metadata(info),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            if capture:
                contents[child_rel] = data
        after = os.fstat(fd) if fd is not None else path.lstat()
        _require(_metadata(after) == before, "Include directory changed during scan")

    root_fd = _directory_fd(root)
    try:
        walk(root, "", root_fd)
    finally:
        if root_fd is not None:
            os.close(root_fd)
    for relative, row in entries.items():
        if row["kind"] == "symlink":
            row["target"] = _leaf_target(relative, row["link_text"], entries)
    _check_links(entries, allow_links)
    return entries, contents


def _write_file(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    _private(path, False)


def _sync_directory(path):
    if _linux():
        fd = _directory_fd(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _write_link(path, text):
    parent_fd = _directory_fd(path.parent)
    try:
        if parent_fd is not None:
            os.symlink(text, path.name, dir_fd=parent_fd)
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        else:
            os.symlink(text, path)
            info = path.lstat()
        _require(stat.S_ISLNK(info.st_mode), "Private certificate link creation changed")
        _private_owner(info)
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _json(value):
    return (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()


def _validate_record(record):
    _require(
        type(record) is dict
        and set(record) == {"schema", "trees"}
        and type(record["schema"]) is int
        and record["schema"] == 1,
        "Include snapshot schema requires review",
    )
    _require(
        type(record["trees"]) is dict and set(record["trees"]) <= set(TARGETS),
        "Unreviewed include target",
    )
    budget = _Budget()
    for target, tree in record["trees"].items():
        _require(
            type(tree) is dict and set(tree) == {"source", "original", "copy"},
            "Invalid include record",
        )
        _require(type(tree["source"]) is str, "Invalid original include path")
        _path(tree["source"])
        for category in ("original", "copy"):
            rows = tree[category]
            _require(type(rows) is dict and "" in rows, "Missing include root record")
            for relative, row in rows.items():
                _relative(relative)
                _require(
                    type(row) is dict and row.get("kind") in {"directory", "file", "symlink"},
                    "Invalid include entry record",
                )
                _require(type(row.get("metadata")) is dict, "Invalid include metadata")
                if category == "original":
                    budget.entry()
                    if row["kind"] == "file":
                        budget.file(row["metadata"].get("st_size"))
                    elif row["kind"] == "symlink":
                        _require(type(row.get("link_text")) is str, "Invalid certificate link text")
                        budget.file(len(os.fsencode(row["link_text"])))
            _require(rows[""]["kind"] == "directory", "Include root is not a directory")
            _check_links(rows, target == CERT_TARGET)
        _require(
            set(tree["original"]) == set(tree["copy"]),
            "Include copy membership differs",
        )
        for relative, original in tree["original"].items():
            copied = tree["copy"][relative]
            _require(
                original["kind"] == copied["kind"]
                and original.get("sha256") == copied.get("sha256")
                and original.get("link_text") == copied.get("link_text")
                and original.get("target") == copied.get("target"),
                "Include copy bytes differ",
            )


def _manifest(folder):
    path = folder / COPY_ROOT / MANIFEST
    _private(path, False)
    fd = os.open(path, _flags())
    with os.fdopen(fd, "rb") as stream:
        size = os.fstat(stream.fileno()).st_size
        _require(size <= MAX_BYTES, "Include manifest exceeds reviewed bound")
        data = stream.read(MAX_BYTES + 1)
        _require(len(data) == size, "Include manifest changed during read")
    return data


@_sanitized
def prepare(folder, nginx_mounts):
    folder = _folder(folder)
    _require(
        type(nginx_mounts) is list and len(nginx_mounts) <= 64,
        "Invalid Nginx mount inventory",
    )
    sources = {}
    for mount in nginx_mounts:
        _require(type(mount) is dict, "Invalid Nginx mount")
        target = mount.get("Destination")
        if target not in TARGETS:
            continue
        _require(target not in sources, "Duplicate include target requires review")
        _require(
            mount.get("Type") in {"bind", "volume"} and type(mount.get("Source")) is str,
            "Include mount type requires review",
        )
        source = _path(mount["Source"])
        _require(
            source != Path(source.anchor)
            and not source.is_relative_to(folder)
            and not folder.is_relative_to(source),
            "Original and private include paths overlap",
        )
        sources[target] = source
    root = folder / COPY_ROOT
    _require(
        not root.exists() and not root.is_symlink(),
        "Existing or interrupted snapshot must be retained",
    )
    root.mkdir(mode=0o700)
    _private(root, True)
    record = {"schema": 1, "trees": {}}
    budget = _Budget()
    originals, contents = {}, {}
    for target, source in sources.items():
        originals[target], contents[target] = _scan(
            source, budget, capture=True, allow_links=target == CERT_TARGET
        )
    for target, source in sources.items():
        copy_root = root / TARGETS[target]
        copy_root.mkdir(mode=0o700)
        for relative, row in originals[target].items():
            if relative and row["kind"] == "directory":
                (copy_root / relative).mkdir(mode=0o700)
        for relative, data in contents[target].items():
            _write_file(copy_root / relative, data)
        for relative, row in originals[target].items():
            if row["kind"] == "symlink":
                _write_link(copy_root / relative, row["link_text"])
        for relative, row in reversed(list(originals[target].items())):
            if row["kind"] == "directory":
                _sync_directory(copy_root / relative)
        copied, _ = _scan(copy_root, _Budget(), private=True, allow_links=target == CERT_TARGET)
        record["trees"][target] = {
            "source": str(source),
            "original": originals[target],
            "copy": copied,
        }
    _validate_record(record)
    _write_file(root / MANIFEST, _json(record))
    _sync_directory(root)
    _sync_directory(folder)
    verify(folder, record)
    return record


@_sanitized
def verify(folder, record):
    folder = _folder(folder)
    _validate_record(record)
    root = folder / COPY_ROOT
    _private(root, True)
    _require(_manifest(folder) == _json(record), "Private include record changed")
    expected = {MANIFEST, *(TARGETS[target] for target in record["trees"])}
    observed = set()
    for path in root.iterdir():
        _require(path.name in expected, "Unexpected private snapshot root entry")
        observed.add(path.name)
    _require(observed == expected, "Private snapshot root entry disappeared")
    original_budget, copy_budget = _Budget(), _Budget()
    for target, tree in record["trees"].items():
        source = _path(tree["source"])
        _require(
            not source.is_relative_to(folder) and not folder.is_relative_to(source),
            "Original and private include paths overlap",
        )
        original, _ = _scan(source, original_budget, allow_links=target == CERT_TARGET)
        copied, _ = _scan(
            root / TARGETS[target], copy_budget, private=True, allow_links=target == CERT_TARGET
        )
        _require(
            _json(original) == _json(tree["original"]),
            "Original include inputs changed",
        )
        _require(_json(copied) == _json(tree["copy"]), "Private include inputs changed")


@_sanitized
def overrides(folder, record):
    verify(folder, record)
    return {target: str(Path(folder) / COPY_ROOT / TARGETS[target]) for target in record["trees"]}
