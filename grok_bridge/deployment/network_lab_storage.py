"""Read-only Linux storage snapshot and lab capacity arithmetic; no entrypoint.

The preexisting storage lock serializes this snapshot only and is released before
return. Later lifecycle admission must hold its own operation lock, freshly
collect/compare acceptable identities and account for allocations. Neither this
observation nor an ext4/xfs label proves VM eligibility, physical disconnection,
physical storage provenance, safe peak estimates, future reserve or authorization.
Mount roots reject subtree binds, but whole-filesystem binds and SAN/iSCSI backing
remain admission limitations. Nested Docker mounts are not queried or rejected.
Only explicitly reported classic overlay2 storage is supported. Containerd image
and snapshot storage can lie outside DockerRootDir and requires a separately
reviewed multi-root collector; this module never changes storage configuration.
No directories, files, permissions or Docker resources are created or changed.
"""

import contextlib
import http.client
import io
import json
import os
import re
import socket
import stat
import struct
import sys
import time
from pathlib import PurePosixPath

import network_lab_capacity as capacity

SCHEMA = "grok-network-lab-storage-v1"
LOCK = "/run/codex-grok-lab-storage.lock"
SOCKET = "/run/docker.sock"
PRIVATE_ROOT = "/var/lib/codex-security-lab"
MAX_MOUNTINFO = 4 * 1024**2
MAX_HEADERS = 16 * 1024
MAX_BODY = 128 * 1024
MAX_WIRE = MAX_HEADERS + MAX_BODY + 64 * 1024
ENGINE_SECONDS = 5


class LabStorageError(RuntimeError):
    """Sanitized refusal; never interpolate private paths, IDs or daemon output."""


def _require(condition, message):
    if not condition:
        raise LabStorageError(message)


def _path(value):
    _require(
        type(value) is str
        and value.startswith("/")
        and len(value.encode("utf-8")) <= 4096
        and all(ord(c) >= 32 and ord(c) != 127 for c in value)
        and ".." not in PurePosixPath(value).parts
        and PurePosixPath(value).as_posix() == value
        and not value.startswith("//")
        and len(PurePosixPath(value).parts) <= 32,
        "Invalid absolute storage path",
    )
    return value


def _read_file(path, bound):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        parts, size = [], 0
        while True:
            data = os.read(fd, min(65536, bound + 1 - size))
            if not data:
                return b"".join(parts)
            size += len(data)
            _require(size <= bound, "Kernel observation exceeds bound")
            parts.append(data)
    finally:
        os.close(fd)


def _unescape(value):
    _require(not re.search(r"\\(?!040|011|012|134)", value), "Invalid mount escape")
    return re.sub(r"\\(040|011|012|134)", lambda m: chr(int(m[1], 8)), value)


def _parse_mountinfo(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= MAX_MOUNTINFO, "Invalid mount table bound")
    lines = raw.decode("utf-8").splitlines()
    _require(len(lines) <= 8192, "Mount table exceeds entry bound")
    result = {}
    for line in lines:
        _require(len(line) <= 16384 and line.count(" - ") == 1, "Invalid mount record")
        left, right = line.split(" - ")
        fields, tail = left.split(), right.split()
        _require(len(fields) >= 6 and len(tail) == 3, "Invalid mount record")
        _require(
            all(re.fullmatch(r"[0-9]{1,10}", n) for n in fields[:2])
            and re.fullmatch(r"[0-9]{1,10}:[0-9]{1,10}", fields[2]),
            "Invalid mount identity",
        )
        mid = int(fields[0])
        _require(mid > 0 and mid not in result, "Duplicate or invalid mount identity")
        result[mid] = {
            "id": mid,
            "parent": int(fields[1]),
            "device": fields[2],
            "root": _unescape(fields[3]),
            "target": _unescape(fields[4]),
            "options": fields[5].split(","),
            "optional": fields[6:],
            "fstype": tail[0],
            "source": _unescape(tail[1]),
            "super_options": tail[2].split(","),
        }
    return result


def _mounts():
    return _parse_mountinfo(_read_file("/proc/self/mountinfo", MAX_MOUNTINFO))


def _fd_mount_id(fd):
    raw = _read_file(f"/proc/self/fdinfo/{fd}", 16384).decode("ascii")
    values = re.findall(r"^mnt_id:\s*([0-9]{1,10})$", raw, re.MULTILINE)
    _require(len(values) == 1, "Descriptor mount identity is unavailable")
    return int(values[0])


def _metadata(value):
    # Ancestor entry counts/timestamps can change from unrelated work. Identity,
    # ownership and mode must not. Leaf lock/socket checks separately pin nlink.
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid)


def _directory(value, private=False):
    _require(
        stat.S_ISDIR(value.st_mode) and value.st_uid == 0 and not value.st_mode & 0o022,
        "Unsafe directory identity or ownership",
    )
    if private:
        _require(stat.S_IMODE(value.st_mode) == 0o700, "Evidence directory must be private")


def _open_root():
    return os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_child(parent, name):
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)


class _DirectoryChain:
    def __init__(self, path, *, evidence=False, storage=False):
        self.path, self.evidence, self.storage = _path(path), evidence, storage
        self.fds, self.bindings = [], []
        try:
            table = _mounts()
            parts = PurePosixPath(path).parts
            for index in range(len(parts)):
                fd = _open_root() if index == 0 else _open_child(self.fds[-1], parts[index])
                self.fds.append(fd)
                prefix = PurePosixPath(*parts[: index + 1]).as_posix()
                info = os.fstat(fd)
                private = evidence and (
                    prefix == PRIVATE_ROOT or prefix.startswith(PRIVATE_ROOT + "/")
                )
                _directory(info, private)
                mount = table.get(_fd_mount_id(fd))
                _require(mount is not None, "Descriptor mount is absent")
                target = mount["target"]
                _require(
                    target == "/"
                    or prefix == target
                    or prefix.startswith(target.rstrip("/") + "/"),
                    "Descriptor mount path differs",
                )
                _require(
                    mount["device"] == f"{os.major(info.st_dev)}:{os.minor(info.st_dev)}",
                    "Descriptor device differs from mount table",
                )
                if storage:
                    _require(
                        mount["fstype"] in {"ext4", "xfs"}
                        and mount["root"] == "/"
                        and "rw" in mount["options"]
                        and "ro" not in mount["options"]
                        and "rw" in mount["super_options"]
                        and "ro" not in mount["super_options"],
                        "Storage mount requires separate review",
                    )
                self.bindings.append({"path": prefix, "metadata": _metadata(info), "mount": mount})
        except BaseException:
            self.close()
            raise

    @property
    def fd(self):
        return self.fds[-1]

    def verify(self):
        for fd, item in zip(self.fds, self.bindings):
            _require(_metadata(os.fstat(fd)) == item["metadata"], "Held directory identity changed")
            _require(_fd_mount_id(fd) == item["mount"]["id"], "Held mount identity changed")
        with _DirectoryChain(self.path, evidence=self.evidence, storage=self.storage) as fresh:
            _require(fresh.bindings == self.bindings, "Directory ancestry or mount changed")

    def close(self):
        for fd in reversed(self.fds):
            os.close(fd)
        self.fds.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _Leaf:
    def __init__(self, path, kind):
        self.kind, self.name = kind, PurePosixPath(path).name
        self.parent = _DirectoryChain(PurePosixPath(path).parent.as_posix())
        try:
            self.identity = self._identity()
        except BaseException:
            self.parent.close()
            raise

    def _identity(self):
        value = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
        _require(value.st_uid == 0 and value.st_nlink == 1, "Unsafe special-file ownership")
        if self.kind == "lock":
            _require(
                stat.S_ISREG(value.st_mode) and stat.S_IMODE(value.st_mode) == 0o600,
                "Storage lock must be a private regular file",
            )
        else:
            _require(
                stat.S_ISSOCK(value.st_mode) and not value.st_mode & 0o007,
                "Unsafe local Docker socket",
            )
        return (*_metadata(value), value.st_nlink)

    def verify(self):
        self.parent.verify()
        _require(self._identity() == self.identity, "Special-file identity changed")

    def close(self):
        self.parent.close()


@contextlib.contextmanager
def _storage_lock():
    import fcntl

    leaf = _Leaf(LOCK, "lock")
    fd = None
    try:
        fd = os.open(
            leaf.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            dir_fd=leaf.parent.fd,
        )
        value = os.fstat(fd)
        _require((*_metadata(value), value.st_nlink) == leaf.identity, "Storage lock was replaced")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LabStorageError("Storage snapshot lock is busy") from None
        leaf.verify()
        yield leaf
        value = os.fstat(fd)
        _require((*_metadata(value), value.st_nlink) == leaf.identity, "Held storage lock changed")
        leaf.verify()
    finally:
        if fd is not None:
            os.close(fd)  # Releases the advisory snapshot lock; never creates/writes it.
        leaf.close()


def _remaining(deadline):
    remaining = deadline - time.monotonic()
    _require(remaining > 0, "Local engine observation exceeded deadline")
    return remaining


class _SocketReader(io.RawIOBase):
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline
        self.count, self.header = 0, bytearray()
        self.headers_complete = False

    def readable(self):
        return True

    def readinto(self, buffer):
        self.connection.settimeout(_remaining(self.deadline))
        size = self.connection.recv_into(buffer)
        self.count += size
        _require(self.count <= MAX_WIRE, "Local engine response exceeds wire bound")
        if not self.headers_complete:
            self.header.extend(memoryview(buffer)[:size])
            end = self.header.find(b"\r\n\r\n")
            _require(
                (end + 4 if end >= 0 else len(self.header)) <= MAX_HEADERS,
                "Local engine headers exceed bound",
            )
            first_end = self.header.find(b"\r\n")
            if first_end >= 0:
                _require(
                    re.fullmatch(rb"HTTP/1\.[01] 200(?: [^\r\n]*)?", self.header[:first_end]),
                    "Local engine returned an unsuccessful status",
                )
            if end >= 0:
                self.headers_complete = True
                self.header.clear()
        return size


class _HTTPStream:
    def __init__(self, connection, deadline):
        self.connection, self.deadline = connection, deadline

    def makefile(self, mode):
        _require(mode == "rb", "Unexpected local engine stream mode")
        return io.BufferedReader(_SocketReader(self.connection, self.deadline))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "Duplicate engine response field")
        result[key] = value
    return result


def _invalid_constant(_):
    raise LabStorageError("Invalid local engine JSON constant")


def _classic_store(data):
    _require(data.get("Driver") == "overlay2", "Docker storage backend is unsupported")
    status = data.get("DriverStatus")
    _require(type(status) is list and 0 < len(status) <= 64, "Docker storage status is unavailable")
    keys = set()
    for row in status:
        _require(
            type(row) is list and len(row) == 2 and all(type(v) is str for v in row),
            "Malformed Docker storage status",
        )
        key, value = row
        _require(
            bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.\-]{0,127}", key))
            and key.strip() == key
            and len(value) <= 1024
            and all(ord(c) >= 32 and ord(c) != 127 for c in value),
            "Malformed Docker storage status",
        )
        normalized = key.casefold()
        _require(normalized not in keys, "Duplicate Docker storage status")
        _require(normalized != "driver-type", "Separate snapshotter storage is unsupported")
        keys.add(normalized)
    return {"storage_driver": data["Driver"], "driver_status": status}


def _engine_info(socket_leaf):
    socket_leaf.verify()
    deadline = time.monotonic() + ENGINE_SECONDS
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(_remaining(deadline))
        connection.connect(SOCKET)
        peer = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        _require(peer[0] > 0 and peer[1] == 0, "Local engine peer is not root")
        socket_leaf.verify()
        connection.settimeout(_remaining(deadline))
        connection.sendall(
            b"GET /info HTTP/1.1\r\nHost: localhost\r\n"
            b"Accept: application/json\r\nConnection: close\r\n\r\n"
        )
        with http.client.HTTPResponse(_HTTPStream(connection, deadline)) as response:
            response.begin()
            _require(response.status == 200, "Local engine request failed")
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            _require(
                len(lengths) <= 1 and len(transfers) <= 1 and not (lengths and transfers),
                "Ambiguous local engine response framing",
            )
            if lengths:
                _require(
                    re.fullmatch(r"[0-9]{1,9}", lengths[0]) and int(lengths[0]) <= MAX_BODY,
                    "Local engine body exceeds bound",
                )
            if transfers:
                _require(transfers == ["chunked"], "Unsupported local engine framing")
            body = response.read(MAX_BODY + 1)
            _require(len(body) <= MAX_BODY, "Local engine body exceeds bound")
            _require(
                not lengths or len(body) == int(lengths[0]), "Local engine body length differs"
            )
            _remaining(deadline)
    socket_leaf.verify()
    data = json.loads(
        body.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_invalid_constant
    )
    _require(type(data) is dict and type(data.get("ID")) is str, "Invalid local engine identity")
    _require(
        re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,127}", data["ID"]),
        "Invalid local engine identity",
    )
    return {
        "engine_id": data["ID"],
        "docker_root": _path(data.get("DockerRootDir")),
        "peer": peer,
        **_classic_store(data),
    }


def _space(chain):
    info, value = os.fstat(chain.fd), os.fstatvfs(chain.fd)
    _require(not value.f_flag & os.ST_RDONLY, "Storage descriptor is read-only")
    _require(
        all(
            type(x) is int and x >= 0
            for x in (value.f_blocks, value.f_bfree, value.f_bavail, value.f_fsid)
        )
        and type(value.f_frsize) is int
        and value.f_frsize > 0
        and value.f_bavail <= value.f_bfree <= value.f_blocks,
        "Invalid descriptor capacity counters",
    )
    total, available = value.f_blocks * value.f_frsize, value.f_bavail * value.f_frsize
    _require(
        capacity._byte_count(total) and capacity._byte_count(available),
        "Storage counters exceed bound",
    )
    return {
        "filesystem_id": (
            f"dev:{os.major(info.st_dev)}:{os.minor(info.st_dev)}:fsid:{value.f_fsid:x}"
        ),
        "total_bytes": total,
        "available_bytes": available,
    }


def _disjoint(left, right):
    left, right = PurePosixPath(left), PurePosixPath(right)
    _require(
        left != right and left not in right.parents and right not in left.parents,
        "Docker and evidence storage overlap",
    )


def _disjoint_chains(evidence, docker):
    # Lexically disjoint whole-filesystem aliases can still reach the same tree.
    private = next(item for item in evidence.bindings if item["path"] == PRIVATE_ROOT)
    docker_root = docker.bindings[-1]["metadata"][:2]
    _require(
        docker_root not in {item["metadata"][:2] for item in evidence.bindings}
        and private["metadata"][:2] not in {item["metadata"][:2] for item in docker.bindings},
        "Docker and evidence directory identities overlap",
    )


def _collect_snapshot(context, peaks):
    with _storage_lock() as lock, contextlib.ExitStack() as stack:
        socket_leaf = _Leaf(SOCKET, "socket")
        stack.callback(socket_leaf.close)
        first_engine = _engine_info(socket_leaf)
        _require(first_engine["engine_id"] == context.engine_id, "Local engine identity differs")
        evidence_path = context.generation_root.as_posix()
        _disjoint(PRIVATE_ROOT, first_engine["docker_root"])
        evidence = stack.enter_context(_DirectoryChain(evidence_path, evidence=True, storage=True))
        docker = stack.enter_context(_DirectoryChain(first_engine["docker_root"], storage=True))
        _disjoint_chains(evidence, docker)
        roots = {"evidence": evidence, "docker": docker}
        first_space = {role: _space(chain) for role, chain in roots.items()}
        for chain in roots.values():
            chain.verify()
        _require(_engine_info(socket_leaf) == first_engine, "Local engine binding changed")
        last_space = {role: _space(chain) for role, chain in roots.items()}
        observations = {}
        for role, chain in roots.items():
            chain.verify()
            first, last = first_space[role], last_space[role]
            _require(
                first["filesystem_id"] == last["filesystem_id"]
                and first["total_bytes"] == last["total_bytes"],
                "Filesystem identity or total changed",
            )
            observations[role] = {
                **last,
                "available_bytes": min(first["available_bytes"], last["available_bytes"]),
            }
        socket_leaf.verify()
        lock.verify()
        assessment = capacity.assess(context, observations, peaks=peaks)
        binding = {
            "engine": first_engine,
            "socket": socket_leaf.identity,
            "lock": lock.identity,
            "paths": {role: chain.bindings for role, chain in roots.items()},
        }
        return {
            "schema": SCHEMA,
            "scope": "read_only_storage_observation_and_capacity_arithmetic",
            "context_sha256": context.digest(),
            "bindings_sha256": capacity._digest(binding),
            "capacity_assessment": assessment,
            "snapshot_lock_released_on_return": True,
            "execution_admission": False,
        }


def collect(context, *, peaks):
    """Observe preexisting local roots under a snapshot lock; no admission permit.

    The exact context generation must already exist. This call does not establish
    physical VM/storage provenance or image/source admission. Available space is
    sampled during collection, not reserved; estimate adequacy and freshness after
    return remain unverified. Public output contains hashes/counters, not raw IDs.
    """
    try:
        context = capacity._context(context)
        _require(capacity._fields(peaks, capacity.ROLES), "Explicit upcoming peaks are incomplete")
        detached = {role: peaks[role] for role in capacity.ROLES}
        _require(
            all(capacity._byte_count(value) for value in detached.values())
            and sum(detached.values()) > 0,
            "Invalid upcoming peaks",
        )
        _require(
            sys.platform.startswith("linux") and os.geteuid() == 0,
            "Storage observation requires root on Linux",
        )
        return _collect_snapshot(context, detached)
    except LabStorageError:
        raise
    except Exception:  # noqa: BLE001 -- private OS/daemon/JSON errors must not escape.
        raise LabStorageError(
            "Storage observation failed; reconcile private prerequisites"
        ) from None
