"""Isolated simulated collector checks and Linux-only temporary FD/lock tests.

No real collector invocation, real Docker connection or production path access.
Linux FD tests anchor opens in pytest temporary directories and simulate proc
mount observations explicitly; they do not establish actual ext4/xfs admission.
"""

import contextlib
import copy
import json
import os
import socket
import struct
import sys
from types import SimpleNamespace

import network_lab_context as lab
import network_lab_storage as storage
import pytest
from test_network_lab_context import record

GIB = 1024**3
LINUX = sys.platform.startswith("linux")
_ENGINE_INFO = storage._engine_info


def context():
    return lab.from_record(record())


def peaks():
    return {"evidence": 2 * GIB, "docker": 3 * GIB}


def engine_data():
    return {
        "ID": context().engine_id,
        "DockerRootDir": "/var/lib/docker",
        "Driver": "overlay2",
        "DriverStatus": [
            ["Backing Filesystem", "extfs"],
            ["Supports d_type", "true"],
            ["Using metacopy", "false"],
            ["Native Overlay Diff", "true"],
            ["userxattr", "false"],
        ],
    }


class FakeLeaf:
    def __init__(self, kind="socket"):
        self.identity = (1, 2 if kind == "socket" else 3, 0o600, 0, 0, 1)
        self.checks = 0
        self.hook = lambda _: None
        self.closed = False

    def verify(self):
        self.checks += 1
        self.hook(self.checks)

    def close(self):
        self.closed = True


@pytest.fixture
def simulated(monkeypatch):
    """All OS/Docker collection seams are fake before calling the public API."""
    state = SimpleNamespace(engines=0, space_calls=0, released=False, chains=[])
    state.engine_hook = lambda _: None
    state.chain_hook = lambda _: None
    state.socket, state.lock = FakeLeaf(), FakeLeaf("lock")
    state.available = [20 * GIB] * 4
    state.totals = [60 * GIB] * 4
    state.identities = ["dev:8:1:fsid:ab"] * 4

    @contextlib.contextmanager
    def lock():
        try:
            yield state.lock
        finally:
            state.released = True

    class Chain:
        def __init__(self, path, *, evidence=False, storage=False):
            self.path, self.role = path, "evidence" if evidence else "docker"
            self.bindings = [{"path": "/", "metadata": (1, 1, 0o755, 0, 0)}]
            if evidence:
                self.bindings.append(
                    {"path": storage_module.PRIVATE_ROOT, "metadata": (1, 4, 0o700, 0, 0)}
                )
            self.bindings.append({"path": path, "metadata": (1, 5 if evidence else 6, 0o700, 0, 0)})
            state.chains.append(self)

        def verify(self):
            state.chain_hook(self)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def engine(_):
        state.engines += 1
        result = {
            "engine_id": context().engine_id,
            "docker_root": "/var/lib/docker",
            "peer": (10, 0, 0),
            "storage_driver": "overlay2",
            "driver_status": engine_data()["DriverStatus"],
        }
        state.engine_hook(result)
        return result

    def space(_):
        index = state.space_calls
        state.space_calls += 1
        return {
            "filesystem_id": state.identities[index],
            "total_bytes": state.totals[index],
            "available_bytes": state.available[index],
        }

    storage_module = storage
    monkeypatch.setattr(storage, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(storage.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(storage, "_storage_lock", lock)
    monkeypatch.setattr(storage, "_Leaf", lambda *_: state.socket)
    monkeypatch.setattr(storage, "_DirectoryChain", Chain)
    monkeypatch.setattr(storage, "_engine_info", engine)
    monkeypatch.setattr(storage, "_space", space)
    return state


def test_simulated_collection_uses_lower_measurement_and_releases_snapshot_lock(simulated):
    simulated.available = [18 * GIB, 17 * GIB, 16 * GIB, 15 * GIB]
    result = storage.collect(context(), peaks=peaks())
    group = result["capacity_assessment"]["filesystems"][0]
    assert group["available_bytes"] == group["required_bytes"] == 15 * GIB
    assert simulated.released and result["snapshot_lock_released_on_return"] is True
    assert result["execution_admission"] is False
    assert simulated.engines == 2 and simulated.space_calls == 4
    assert len(result["bindings_sha256"]) == 64
    encoded = json.dumps(result)
    for private in (
        context().engine_id,
        context().vm_uuid,
        str(context().generation_root),
        "/var/lib/docker",
    ):
        assert private not in encoded


def test_simulated_capacity_one_byte_below_refuses_and_unlocks(simulated):
    simulated.available[-1] = 15 * GIB - 1
    with pytest.raises(storage.LabStorageError):
        storage.collect(context(), peaks=peaks())
    assert simulated.released


@pytest.mark.parametrize("field", ["engine_id", "docker_root", "peer", "driver_status"])
def test_simulated_engine_binding_changes_between_reads_refuse(field, simulated):
    def change(result):
        if simulated.engines == 2:
            result[field] = {
                "engine_id": "other",
                "docker_root": "/other",
                "peer": (11, 0, 0),
                "driver_status": [["Backing Filesystem", "xfs"]],
            }[field]

    simulated.engine_hook = change
    with pytest.raises(storage.LabStorageError, match="binding"):
        storage.collect(context(), peaks=peaks())
    assert simulated.released


def test_wrong_initial_engine_stops_before_opening_storage(simulated):
    simulated.engine_hook = lambda result: result.update(engine_id="foreign")
    with pytest.raises(storage.LabStorageError, match="identity"):
        storage.collect(context(), peaks=peaks())
    assert simulated.chains == [] and simulated.space_calls == 0


@pytest.mark.parametrize("field", ["totals", "identities"])
def test_simulated_filesystem_identity_or_total_drift_refuses(field, simulated):
    getattr(simulated, field)[-1] = 61 * GIB if field == "totals" else "dev:8:2:fsid:cd"
    with pytest.raises(storage.LabStorageError, match="Filesystem"):
        storage.collect(context(), peaks=peaks())


def test_peaks_are_detached_before_any_observation_callback(simulated):
    required = peaks()
    simulated.engine_hook = lambda _: required.update(evidence=0, docker=1)
    result = storage.collect(context(), peaks=required)
    assert result["capacity_assessment"]["filesystems"][0]["peak_bytes"] == 5 * GIB


def test_directory_revalidation_failure_is_not_ignored(simulated):
    simulated.chain_hook = lambda _: (_ for _ in ()).throw(
        storage.LabStorageError("changed ancestor")
    )
    with pytest.raises(storage.LabStorageError, match="ancestor"):
        storage.collect(context(), peaks=peaks())
    assert simulated.released


@pytest.mark.parametrize("bad", [None, {}, True, "lab"])
def test_invalid_context_precedes_host_io(bad, monkeypatch):
    monkeypatch.setattr(storage, "_collect_snapshot", lambda *_: pytest.fail("Host I/O attempted"))
    with pytest.raises(storage.LabStorageError):
        storage.collect(bad, peaks=peaks())


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"evidence": 1},
        {"evidence": True, "docker": 1},
        {"evidence": 1.0, "docker": 1},
        {"evidence": -1, "docker": 1},
        {"evidence": 0, "docker": 0},
    ],
)
def test_invalid_peaks_precede_host_io(bad, monkeypatch):
    monkeypatch.setattr(storage, "_collect_snapshot", lambda *_: pytest.fail("Host I/O attempted"))
    with pytest.raises(storage.LabStorageError):
        storage.collect(context(), peaks=bad)


@pytest.mark.parametrize("platform,uid", [("win32", 0), ("linux", 1000)])
def test_real_linux_root_is_required_before_collection(platform, uid, monkeypatch):
    monkeypatch.setattr(storage, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(storage.os, "geteuid", lambda: uid, raising=False)
    monkeypatch.setattr(storage, "_collect_snapshot", lambda *_: pytest.fail("Host I/O attempted"))
    with pytest.raises(storage.LabStorageError, match="root on Linux"):
        storage.collect(context(), peaks=peaks())


def test_private_os_or_daemon_failures_are_sanitized(simulated, monkeypatch):
    def fail(*_):
        raise RuntimeError("private-engine-id /private/path secret-response")

    monkeypatch.setattr(storage, "_engine_info", fail)
    with pytest.raises(storage.LabStorageError) as failure:
        storage.collect(context(), peaks=peaks())
    assert "private-engine" not in str(failure.value) and "/private/" not in str(failure.value)
    assert simulated.released


def response(body=None, *, status=b"200 OK", headers=None):
    body = body if body is not None else json.dumps(engine_data()).encode()
    headers = (
        headers if headers is not None else b"Content-Length: " + str(len(body)).encode() + b"\r\n"
    )
    return b"HTTP/1.1 " + status + b"\r\n" + headers + b"\r\n" + body


@pytest.fixture
def unix_http(monkeypatch):
    state = SimpleNamespace(
        now=0.0, tick=0, chunk=4096, peer=(20, 0, 0), sent=[], timeouts=[], wire=response()
    )

    class Socket:
        def __init__(self, *args):
            assert args == (1, socket.SOCK_STREAM)
            self.position = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, value):
            state.timeouts.append(value)

        def connect(self, path):
            assert path == "/run/docker.sock"

        def getsockopt(self, *_):
            return struct.pack("3i", *state.peer)

        def sendall(self, data):
            state.sent.append(data)

        def recv_into(self, buffer):
            state.now += state.tick
            data = state.wire[self.position : self.position + min(len(buffer), state.chunk)]
            buffer[: len(data)] = data
            self.position += len(data)
            return len(data)

    monkeypatch.setattr(
        storage,
        "socket",
        SimpleNamespace(
            AF_UNIX=1,
            SOCK_STREAM=socket.SOCK_STREAM,
            SOL_SOCKET=socket.SOL_SOCKET,
            SO_PEERCRED=17,
            socket=Socket,
        ),
    )
    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=lambda: state.now))
    return state


def test_readonly_unix_info_has_fixed_request_and_root_peer(unix_http):
    leaf = FakeLeaf()
    result = storage._engine_info(leaf)
    assert result == {
        "engine_id": context().engine_id,
        "docker_root": "/var/lib/docker",
        "peer": (20, 0, 0),
        "storage_driver": "overlay2",
        "driver_status": engine_data()["DriverStatus"],
    }
    assert leaf.checks == 3
    assert len(unix_http.sent) == 1 and unix_http.sent[0].startswith(b"GET /info HTTP/1.1\r\n")
    assert all(0 < value <= storage.ENGINE_SECONDS for value in unix_http.timeouts)


def test_chunked_info_is_bounded_and_supported(unix_http):
    body = json.dumps(engine_data()).encode()
    chunks = hex(len(body))[2:].encode() + b"\r\n" + body + b"\r\n0\r\n\r\n"
    unix_http.wire = response(chunks, headers=b"Transfer-Encoding: chunked\r\n")
    assert storage._engine_info(FakeLeaf())["engine_id"] == context().engine_id


@pytest.mark.parametrize(
    "bad",
    [
        "duplicate",
        "missing",
        "wrong_type",
        "traversal",
        "nan",
        "malformed",
        "redirect",
        "failure",
        "duplicate_length",
        "short_body",
        "oversize_body",
        "oversize_headers",
        "ambiguous_framing",
    ],
)
def test_http_identity_and_framing_refusals(bad, unix_http):
    bodies = {
        "duplicate": b'{"ID":"x","ID":"fixture-engine:123","DockerRootDir":"/var/lib/docker"}',
        "missing": b'{"ID":"fixture-engine:123"}',
        "wrong_type": b'{"ID":false,"DockerRootDir":"/var/lib/docker"}',
        "traversal": b'{"ID":"fixture-engine:123","DockerRootDir":"/var/../private"}',
        "nan": b'{"ID":"fixture-engine:123","DockerRootDir":"/var/lib/docker","other":NaN}',
        "malformed": b"private-not-json",
    }
    if bad in bodies:
        unix_http.wire = response(bodies[bad])
    elif bad in {"redirect", "failure"}:
        unix_http.wire = response(
            status=b"302 Found" if bad == "redirect" else b"500 Internal Error"
        )
    elif bad == "duplicate_length":
        unix_http.wire = response(headers=b"Content-Length: 1\r\nContent-Length: 1\r\n")
    elif bad == "short_body":
        unix_http.wire = response(
            headers=b"Content-Length: 1000\r\n"
        )  # Valid short JSON, truncated framing.
    elif bad == "oversize_body":
        unix_http.wire = response(b"x" * (storage.MAX_BODY + 1), headers=b"")
    elif bad == "oversize_headers":
        unix_http.wire = response(headers=b"X-Padding: " + b"x" * storage.MAX_HEADERS + b"\r\n")
    else:
        unix_http.wire = response(headers=b"Content-Length: 1\r\nTransfer-Encoding: chunked\r\n")
    with pytest.raises((storage.LabStorageError, ValueError, OSError)):
        storage._engine_info(FakeLeaf())


def test_absolute_deadline_stops_a_slow_byte_stream(unix_http):
    unix_http.tick, unix_http.chunk = 0.8, 1
    with pytest.raises(storage.LabStorageError, match="deadline"):
        storage._engine_info(FakeLeaf())
    assert unix_http.now <= storage.ENGINE_SECONDS + 0.8
    assert len(unix_http.timeouts) < 20


def test_nonroot_peer_refuses_before_sending_request(unix_http):
    unix_http.peer = (20, 1000, 1000)
    with pytest.raises(storage.LabStorageError, match="peer"):
        storage._engine_info(FakeLeaf())
    assert unix_http.sent == []


def test_socket_replacement_after_response_refuses(unix_http):
    leaf = FakeLeaf()

    def changed(count):
        if count == 3:
            raise storage.LabStorageError("Socket changed")

    leaf.hook = changed
    with pytest.raises(storage.LabStorageError, match="changed"):
        storage._engine_info(leaf)


@pytest.mark.parametrize(
    "bad",
    [
        "overlayfs",
        "containerd",
        "unknown",
        "missing_driver",
        "missing_status",
        "empty_status",
        "malformed_status",
        "malformed_row",
        "duplicate_row",
        "snapshotter",
        "snapshotter_case",
    ],
)
def test_separate_or_unknown_docker_storage_refuses_before_storage_open(
    bad, unix_http, simulated, monkeypatch
):
    data = engine_data()
    if bad in {"overlayfs", "containerd", "unknown"}:
        data["Driver"] = bad
    elif bad == "missing_driver":
        data.pop("Driver")
    elif bad == "missing_status":
        data.pop("DriverStatus")
    elif bad == "empty_status":
        data["DriverStatus"] = []
    elif bad == "malformed_status":
        data["DriverStatus"] = {"driver-type": "io.containerd.snapshotter.v1"}
    elif bad == "malformed_row":
        data["DriverStatus"] = [["Backing Filesystem", False]]
    elif bad == "duplicate_row":
        data["DriverStatus"].append(data["DriverStatus"][0])
    else:
        key = "driver-type" if bad == "snapshotter" else "DRIVER-TYPE"
        data["DriverStatus"].append([key, "io.containerd.snapshotter.v1"])
    unix_http.wire = response(json.dumps(data).encode())
    monkeypatch.setattr(storage, "_engine_info", _ENGINE_INFO)
    with pytest.raises(storage.LabStorageError):
        storage.collect(context(), peaks=peaks())
    assert simulated.chains == [] and simulated.space_calls == 0 and simulated.released


@pytest.mark.parametrize(
    "bad",
    [
        "/var/../docker",
        "/var//docker",
        "/var/./docker",
        "//var/docker",
        "relative",
        "/var\nsecret",
        "/var/docker/",
    ],
)
def test_noncanonical_paths_refuse(bad):
    with pytest.raises(storage.LabStorageError):
        storage._path(bad)


@pytest.mark.parametrize(
    "right", [storage.PRIVATE_ROOT, storage.PRIVATE_ROOT + "/nested", "/var/lib", "/"]
)
def test_lexical_storage_overlap_refuses(right):
    with pytest.raises(storage.LabStorageError):
        storage._disjoint(storage.PRIVATE_ROOT, right)


def test_whole_filesystem_alias_cannot_hide_inode_overlap():
    evidence = SimpleNamespace(
        bindings=[
            {"path": storage.PRIVATE_ROOT, "metadata": (1, 4)},
            {"path": "/generation", "metadata": (1, 5)},
        ]
    )
    docker = SimpleNamespace(
        bindings=[
            {"path": "/alias", "metadata": (1, 4)},
            {"path": "/alias/docker", "metadata": (1, 6)},
        ]
    )
    with pytest.raises(storage.LabStorageError, match="identities overlap"):
        storage._disjoint_chains(evidence, docker)


def test_statvfs_uses_unprivileged_available_bytes(monkeypatch):
    values = SimpleNamespace(
        f_flag=0, f_blocks=100, f_bfree=80, f_bavail=70, f_frsize=4096, f_fsid=7
    )
    monkeypatch.setattr(
        storage,
        "os",
        SimpleNamespace(
            fstat=lambda _: SimpleNamespace(st_dev=2049),
            major=lambda _: 8,
            minor=lambda _: 1,
            ST_RDONLY=1,
            fstatvfs=lambda _: values,
        ),
    )
    result = storage._space(SimpleNamespace(fd=10))
    assert result["available_bytes"] == 70 * 4096
    values.f_bavail = 81
    with pytest.raises(storage.LabStorageError, match="counters"):
        storage._space(SimpleNamespace(fd=10))
    values.f_bavail, values.f_flag = 70, 1
    with pytest.raises(storage.LabStorageError, match="read-only"):
        storage._space(SimpleNamespace(fd=10))


@pytest.fixture
def linux_tree(tmp_path, monkeypatch):
    if not LINUX:
        pytest.skip("Requires Linux directory-FD/flock support; ownership is simulated")
    tmp_path.chmod(0o700)
    for relative in (
        "var/lib/codex-security-lab/grok-network-lifecycle/example",
        "var/lib/docker",
        "run",
    ):
        directory = tmp_path
        for part in relative.split("/"):
            directory = directory / part
            directory.mkdir(mode=0o700, exist_ok=True)
    (tmp_path / "var/lib/docker").chmod(0o711)
    lock = tmp_path / "run/codex-grok-lab-storage.lock"
    lock.write_bytes(b"")
    lock.chmod(0o600)
    device = tmp_path.stat().st_dev
    line = f"90 1 {os.major(device)}:{os.minor(device)} / / rw,relatime - ext4 /dev/test rw\n"
    table = storage._parse_mountinfo(line.encode())
    state = SimpleNamespace(root=tmp_path, table=table, mid=90, lock=lock, foreign_owners=set())

    def anchored_fd(fd):
        observed = os.readlink(f"/proc/self/fd/{fd}")
        assert observed == str(tmp_path) or observed.startswith(str(tmp_path) + "/")
        return observed

    def owner_view(value, path):
        return SimpleNamespace(
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_mode=value.st_mode,
            st_uid=1000 if path in state.foreign_owners else 0,
            st_gid=value.st_gid,
            st_nlink=value.st_nlink,
        )

    class AnchoredOwnership:
        """Simulate ownership only; all FD/path/type/mode/lock mechanics are real."""

        def __getattr__(self, name):
            return getattr(os, name)

        def fstat(self, fd):
            return owner_view(os.fstat(fd), anchored_fd(fd))

        def stat(self, path, *, dir_fd, follow_symlinks):
            parent = anchored_fd(dir_fd)
            assert not os.path.isabs(path) and "/" not in path and path not in {".", ".."}
            return owner_view(
                os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks), parent + "/" + path
            )

    monkeypatch.setattr(storage, "os", AnchoredOwnership())
    monkeypatch.setattr(
        storage,
        "_open_root",
        lambda: os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW),
    )
    monkeypatch.setattr(storage, "_mounts", lambda: copy.deepcopy(state.table))
    monkeypatch.setattr(storage, "_fd_mount_id", lambda _: state.mid)
    return state


def test_linux_real_directory_fds_allow_0711_docker_and_ignore_nested_overlay(linux_tree):
    linux_tree.table[91] = {
        **linux_tree.table[90],
        "id": 91,
        "fstype": "overlay",
        "target": "/var/lib/docker/overlay2/test/merged",
        "device": "0:99",
    }
    with storage._DirectoryChain("/var/lib/docker", storage=True) as chain:
        chain.verify()
        assert os.fstat(chain.fd).st_ino == (linux_tree.root / "var/lib/docker").stat().st_ino


@pytest.mark.parametrize("fstype", ["nfs", "cifs", "overlay", "tmpfs"])
def test_linux_queried_unsupported_filesystem_refuses(fstype, linux_tree):
    linux_tree.table[90]["fstype"] = fstype
    with pytest.raises(storage.LabStorageError, match="mount"):
        storage._DirectoryChain("/var/lib/docker", storage=True)


@pytest.mark.parametrize("change", ["subtree", "readonly"])
def test_linux_subtree_mount_and_readonly_mount_refuse(change, linux_tree):
    if change == "subtree":
        linux_tree.table[90]["root"] = "/subtree"
    else:
        linux_tree.table[90]["super_options"] = ["ro"]
    with pytest.raises(storage.LabStorageError, match="mount"):
        storage._DirectoryChain("/var/lib/docker", storage=True)


def test_linux_evidence_private_mode_and_symlink_ancestry_refuse(linux_tree):
    root = linux_tree.root / "var/lib/codex-security-lab"
    root.chmod(0o711)
    with pytest.raises(storage.LabStorageError, match="private"):
        storage._DirectoryChain(
            "/var/lib/codex-security-lab/grok-network-lifecycle/example",
            evidence=True,
            storage=True,
        )
    root.chmod(0o700)
    original = linux_tree.root / "var/lib/docker"
    original.rename(original.with_name("docker-old"))
    original.symlink_to("docker-old", target_is_directory=True)
    with pytest.raises(OSError):
        storage._DirectoryChain("/var/lib/docker", storage=True)


@pytest.mark.parametrize("change", ["metadata", "replacement", "mount"])
def test_linux_ancestor_revalidation_detects_same_device_substitutions(change, linux_tree):
    with storage._DirectoryChain("/var/lib/docker", storage=True) as chain:
        if change == "metadata":
            (linux_tree.root / "var").chmod(
                0o755
            )  # Still safe mode, but changed identity metadata.
        elif change == "replacement":
            original = linux_tree.root / "var/lib"
            original.rename(original.with_name("lib-old"))
            original.mkdir(mode=0o700)
            (original / "docker").mkdir(mode=0o700)
        else:
            linux_tree.mid = 92
            linux_tree.table[92] = {**linux_tree.table[90], "id": 92}
        with pytest.raises(storage.LabStorageError, match="changed"):
            chain.verify()


def test_linux_preexisting_readonly_lock_is_exclusive_and_released(linux_tree):
    original = linux_tree.lock.read_bytes()
    with (
        storage._storage_lock(),
        pytest.raises(storage.LabStorageError, match="busy"),
        storage._storage_lock(),
    ):
        pytest.fail("Concurrent snapshot entered")
    with storage._storage_lock():
        pass
    assert linux_tree.lock.read_bytes() == original


@pytest.mark.parametrize("change", ["replacement", "hardlink", "symlink", "mode"])
def test_linux_lock_identity_refusals(change, linux_tree):
    if change == "replacement":
        with pytest.raises(storage.LabStorageError, match="changed"), storage._storage_lock():
            linux_tree.lock.rename(linux_tree.lock.with_name("retained-lock"))
            linux_tree.lock.write_bytes(b"")
            linux_tree.lock.chmod(0o600)
        return
    if change == "hardlink":
        os.link(linux_tree.lock, linux_tree.lock.with_name("another-link"))
    elif change == "symlink":
        linux_tree.lock.rename(linux_tree.lock.with_name("retained-lock"))
        linux_tree.lock.symlink_to("retained-lock")
    else:
        linux_tree.lock.chmod(0o640)
    with pytest.raises(storage.LabStorageError), storage._storage_lock():
        pytest.fail("Unsafe lock admitted")


def test_linux_socket_type_and_replacement_refuse(linux_tree):
    path = linux_tree.root / "run/docker.sock"
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            # Keep the actual fixture socket pathname within Linux's sun_path bound.
            connection.bind(f"/proc/self/fd/{parent}/docker.sock")
            path.chmod(0o600)
            leaf = storage._Leaf(storage.SOCKET, "socket")
            try:
                leaf.verify()
                path.rename(path.with_name("retained-socket"))
                path.symlink_to("retained-socket")
                with pytest.raises(storage.LabStorageError):
                    leaf.verify()
            finally:
                leaf.close()
    finally:
        os.close(parent)


@pytest.mark.parametrize("kind", ["directory", "lock"])
def test_linux_nonroot_ownership_is_refused_despite_private_modes(kind, linux_tree):
    if kind == "directory":
        linux_tree.foreign_owners.add(str(linux_tree.root / "var/lib/docker"))
        with pytest.raises(storage.LabStorageError, match="ownership"):
            storage._DirectoryChain("/var/lib/docker", storage=True)
    else:
        linux_tree.foreign_owners.add(str(linux_tree.lock))
        with pytest.raises(storage.LabStorageError, match="ownership"), storage._storage_lock():
            pytest.fail("Foreign lock admitted")


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"invalid",
        b"1 0 8:1 / / rw - ext4 /dev/a rw\n" * 2,
        b"1 0 8:1 /bad\\999 / rw - ext4 /dev/a rw\n",
    ],
)
def test_malformed_mount_tables_refuse(bad):
    with pytest.raises(storage.LabStorageError):
        storage._parse_mountinfo(bad)


def test_mount_escape_and_distinct_mount_ids_are_preserved():
    table = storage._parse_mountinfo(
        b"1 0 8:1 / / rw - ext4 /dev/a rw\n2 1 8:1 / /space\\040name rw - ext4 /dev/a rw\n"
    )
    assert table[2]["target"] == "/space name"
    assert table[1]["device"] == table[2]["device"]
    assert table[1]["id"] != table[2]["id"]


@pytest.mark.parametrize("body", [b"pos: 0\n", b"mnt_id: 1\nmnt_id: 2\n", b"mnt_id: -1\n"])
def test_ambiguous_descriptor_mount_ids_refuse(body, monkeypatch):
    monkeypatch.setattr(storage, "_read_file", lambda *_: body)
    with pytest.raises(storage.LabStorageError):
        storage._fd_mount_id(10)
