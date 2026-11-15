"""Install only fixed synthetic inputs into a preexisting empty lab generation.

This standalone staging phase acquires the existing operation lock. It must not
run inside the read-only observation session. Guest UUID/boot, descriptors and
locks establish observed consistency, never physical isolation or permission.
Fresh observations/admission are required after the staging lock is released.

No root/generation/host-lock provisioning, Docker, named-volume seed installation,
journal initialization, cleanup, overwrite or resume is implemented. Partial trees
are retained and any nonempty generation refuses. There is no atomic whole-tree
visibility or protection against arbitrary privileged writers. Synchronization
receipts report completed syscalls, not power-loss-tested recovery or backup.
"""

import copy
import hashlib
import json
import os
import stat
import threading

import network_lab_auxiliary as auxiliary
import network_lab_capacity as capacity
import network_lab_fixtures as fixtures
import network_lab_session as session
import network_lab_storage as storage

DIRECTORIES = (
    "configuration",
    "configuration/nginx",
    "configuration/nginx/projects-locations.d",
    "configuration/gateway",
    "configuration/auxiliary",
    "seed",
    "runtime",
    "runtime/nginx-logs",
    "runtime/llm-proxy2-logs",
)
FILES = (
    "configuration/docker-compose.yml",
    "configuration/nginx/nginx.conf",
    "configuration/nginx/projects-locations.d/grok-bridge.conf",
    "configuration/gateway/nginx.conf",
    "configuration/auxiliary/reference.compose.yml",
    "configuration/auxiliary/probe.compose.yml",
    "configuration/auxiliary/probe_idle.py",
    "seed/fixture-generation.txt",
    "seed/reference-generation.txt",
)
INTENT = ".materialization.lab.intent.json"
COMPLETE = ".materialization.lab.complete.json"
AUXILIARY_LOCK = ".auxiliary-state.lab.lock"
MAX_FILE_BYTES = 65536
MAX_RECORD_BYTES = 16384
MAX_TOTAL_BYTES = 262144
SCHEMA = "grok-network-lab-materialization-v1"


class LabMaterializationError(RuntimeError):
    """Sanitized staging refusal; any existing artifacts must be retained."""


class LabMaterializationUncertain(LabMaterializationError):
    """Completion publication was attempted without a complete acknowledgment."""


def _require(condition, message):
    if not condition:
        raise LabMaterializationError(message)


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _record(value):
    data = (
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode()
    _require(len(data) <= MAX_RECORD_BYTES, "Materialization record exceeds bound")
    return data


def _binding(context, binding):
    ctx = capacity._context(context)
    ctx.verify_journal_binding(binding)
    return ctx, copy.deepcopy(binding)


def _inputs(context, binding):
    ctx, retained = _binding(context, binding)
    working = fixtures.original_fixture_inputs(ctx, lab_binding=retained)
    extra = auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=retained)
    _require(type(working) is dict and type(extra) is dict, "Invalid generated material mapping")
    _require(not set(working).intersection(extra), "Generated material paths collide")
    result = {**working, **extra}
    _require(set(result) == set(FILES), "Generated material paths differ")
    _require(
        all(type(value) is bytes and len(value) <= MAX_FILE_BYTES for value in result.values()),
        "Generated material bytes exceed contract",
    )
    _require(sum(map(len, result.values())) <= MAX_TOTAL_BYTES, "Material content exceeds bound")
    current, current_binding = _binding(context, binding)
    _require(
        ctx == current and capacity._exact(retained, current_binding),
        "Materialization input binding changed",
    )
    return ctx, retained, result


def _file_identity(info):
    return (
        *storage._metadata(info),
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _private_file(info):
    _require(
        stat.S_ISREG(info.st_mode)
        and info.st_uid == 0
        and stat.S_IMODE(info.st_mode) == 0o600
        and info.st_nlink == 1,
        "Material leaf must be private, root-owned, regular and single-link",
    )


def _split(relative):
    if "/" in relative:
        return relative.rsplit("/", 1)
    return "", relative


class _Installer:
    """One synchronous installation; retained FDs are never exposed as authority."""

    def __init__(self, context, binding, ctx, retained):
        self.source_context, self.source_binding = context, binding
        self.context, self.binding = ctx, retained
        self.operation = self.storage_lock = self.chain = None
        self.directories, self.files, self.opened = {}, {}, []
        self.completion_attempted = False
        try:
            session._platform()
            self.pid, self.thread = os.getpid(), threading.get_ident()
            self.operation = session._OperationLock()
            self.storage_lock = storage._Leaf(storage.LOCK, "lock")
            _require(
                session.LOCK != storage.LOCK
                and self.operation.leaf.identity[:2] != self.storage_lock.identity[:2],
                "Materialization host locks must be distinct",
            )
            self.operation_binding = self.operation.binding_sha256()
            self.storage_binding = capacity._digest(
                {"leaf": self.storage_lock.identity, "parents": self.storage_lock.parent.bindings}
            )
            self.guest = copy.deepcopy(session._guest())
            _require(
                self.guest["uuid"]["value"] == ctx.vm_uuid,
                "Observed materialization guest differs",
            )
            self.chain = storage._DirectoryChain(
                str(ctx.generation_root), evidence=True, storage=True
            )
            self.mount_id = storage._fd_mount_id(self.chain.fd)
            self.directories[""] = (self.chain.fd, storage._metadata(os.fstat(self.chain.fd)))
            self.guard()
            _require(not self.names(""), "Materialization generation must be empty")
        except BaseException:
            self.close()
            raise

    def guard(self):
        _require(
            os.getpid() == self.pid and threading.get_ident() == self.thread,
            "Materialization cannot cross process or thread ownership",
        )
        ctx, binding = _binding(self.source_context, self.source_binding)
        _require(
            ctx == self.context and capacity._exact(binding, self.binding),
            "Materialization context or binding changed",
        )
        _require(capacity._exact(session._guest(), self.guest), "Materialization guest changed")
        self.operation.verify()
        self.storage_lock.verify()
        _require(
            self.operation.binding_sha256() == self.operation_binding
            and capacity._digest(
                {"leaf": self.storage_lock.identity, "parents": self.storage_lock.parent.bindings}
            )
            == self.storage_binding,
            "Materialization host lock binding changed",
        )
        self.chain.verify()
        for relative, (descriptor, identity) in self.directories.items():
            info = os.fstat(descriptor)
            storage._directory(info, private=True)
            _require(
                storage._metadata(info) == identity
                and storage._fd_mount_id(descriptor) == self.mount_id,
                "Material directory identity or mount changed",
            )
            if relative:
                parent, name = _split(relative)
                current = os.stat(name, dir_fd=self.directories[parent][0], follow_symlinks=False)
                _require(storage._metadata(current) == identity, "Material directory path changed")
        for relative, (descriptor, identity, _data) in self.files.items():
            parent, name = _split(relative)
            held = os.fstat(descriptor)
            current = os.stat(name, dir_fd=self.directories[parent][0], follow_symlinks=False)
            _private_file(held)
            _require(
                _file_identity(held) == identity
                and _file_identity(current) == identity
                and storage._fd_mount_id(descriptor) == self.mount_id,
                "Material file identity or mount changed",
            )

    def names(self, relative):
        result = set()
        with os.scandir(self.directories[relative][0]) as entries:
            for entry in entries:
                _require(
                    len(result) < len(DIRECTORIES) + len(FILES) + 3, "Material tree exceeds bound"
                )
                result.add(entry.name)
        return result

    def readback(self, relative):
        self.guard()
        parent, name = _split(relative)
        parent_fd = self.directories[parent][0]
        _held, identity, data = self.files[relative]
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        _private_file(before)  # Refuse devices/FIFOs/links before opening the literal leaf.
        _require(_file_identity(before) == identity, "Material leaf changed before opening")
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent_fd
        )
        try:
            _require(
                _file_identity(os.fstat(descriptor)) == identity
                and storage._fd_mount_id(descriptor) == self.mount_id,
                "Material opened leaf differs",
            )
            chunks, count = [], 0
            while True:
                chunk = os.read(descriptor, min(16384, len(data) + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                _require(count <= len(data), "Material read exceeds expected bound")
            _require(
                b"".join(chunks) == data and _file_identity(os.fstat(descriptor)) == identity,
                "Material readback differs",
            )
        finally:
            os.close(descriptor)
        self.guard()

    def write(self, relative, data):
        self.guard()
        parent, name = _split(relative)
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
            0o600,
            dir_fd=self.directories[parent][0],
        )
        self.opened.append(descriptor)
        info = os.fstat(descriptor)
        _private_file(info)
        identity = (*storage._metadata(info), info.st_nlink)
        count = 0
        while count < len(data):
            self.guard()
            before = os.fstat(descriptor)
            _require(
                (*storage._metadata(before), before.st_nlink) == identity,
                "Material staging identity changed",
            )
            written = os.write(descriptor, data[count:])
            _require(
                type(written) is int and 0 < written <= len(data) - count,
                "Material write did not advance",
            )
            count += written
        self.guard()
        os.fsync(descriptor)
        final = os.fstat(descriptor)
        _private_file(final)
        _require(
            (*storage._metadata(final), final.st_nlink) == identity and final.st_size == len(data),
            "Material staging metadata differs",
        )
        self.files[relative] = (descriptor, _file_identity(final), data)
        self.readback(relative)

    def mkdir(self, relative):
        self.guard()
        parent, name = _split(relative)
        parent_fd = self.directories[parent][0]
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        storage._directory(before, private=True)
        descriptor = storage._open_child(parent_fd, name)
        self.opened.append(descriptor)
        _require(
            storage._metadata(os.fstat(descriptor)) == storage._metadata(before),
            "Created material directory changed before opening",
        )
        self.directories[relative] = (descriptor, storage._metadata(before))
        self.guard()

    def tree(self):
        self.guard()
        expected = {relative: set() for relative in self.directories}
        for relative in (*self.directories, *self.files):
            if relative:
                parent, name = _split(relative)
                expected[parent].add(name)
        for relative, names in expected.items():
            _require(self.names(relative) == names, "Material tree membership differs")
        for relative in self.files:
            self.readback(relative)
        self.guard()

    def sync_directories(self):
        for descriptor, _identity in reversed(tuple(self.directories.values())):
            self.guard()
            os.fsync(descriptor)
        self.guard()

    def fingerprints(self):
        return {
            "files": {
                relative: {
                    "size": len(data),
                    "sha256": _hash(data),
                    "identity_sha256": capacity._digest(identity),
                }
                for relative, (_fd, identity, data) in self.files.items()
            },
            "directories": {
                relative: {"identity_sha256": capacity._digest(identity)}
                for relative, (_fd, identity) in self.directories.items()
            },
        }

    def close(self):
        failure = None
        for descriptor in reversed(self.opened):
            try:
                os.close(descriptor)
            except BaseException as error:
                if failure is None:
                    failure = error
        self.opened.clear()
        for name in ("chain", "storage_lock", "operation"):
            resource = getattr(self, name)
            setattr(self, name, None)
            if resource is not None:
                try:
                    resource.close()
                except BaseException as error:
                    if failure is None:
                        failure = error
        if failure is not None:
            raise failure

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def materialize(context, *, lab_binding):
    """Stage fixed files only; no returned record authorizes subsequent execution."""
    installer = None
    try:
        ctx, binding, generated = _inputs(context, lab_binding)
        expected = {**generated, AUXILIARY_LOCK: b""}
        intent = _record(
            {
                "schema": SCHEMA,
                "kind": "intent",
                "lab_context": binding,
                "directories": {name: {"mode": "0700"} for name in DIRECTORIES},
                "files": {
                    name: {"mode": "0600", "size": len(data), "sha256": _hash(data)}
                    for name, data in expected.items()
                },
            }
        )
        _require(
            sum(map(len, generated.values())) + len(intent) + MAX_RECORD_BYTES <= MAX_TOTAL_BYTES,
            "Materialization content and completion reserve exceed bound",
        )
        with _Installer(context, lab_binding, ctx, binding) as installer:
            installer.write(INTENT, intent)
            installer.sync_directories()  # Durable intent precedes every child mutation.
            installer.write(AUXILIARY_LOCK, b"")
            for relative in DIRECTORIES:
                installer.mkdir(relative)
            for relative in FILES:
                installer.write(relative, generated[relative])
            installer.tree()
            installer.sync_directories()
            installer.tree()
            completion = _record(
                {
                    "schema": SCHEMA,
                    "kind": "completion_marker_only",
                    "lab_context": binding,
                    "intent_sha256": _hash(intent),
                    "observed_tree": installer.fingerprints(),
                }
            )
            _require(
                sum(map(len, generated.values())) + len(intent) + len(completion)
                <= MAX_TOTAL_BYTES,
                "Materialization total content exceeds bound",
            )
            installer.completion_attempted = True
            installer.write(COMPLETE, completion)
            installer.sync_directories()
            installer.tree()
            installer.guard()
            return {
                "schema": SCHEMA,
                "scope": "file_materialization_only",
                "context_sha256": ctx.digest(),
                "guest_sha256": capacity._digest(installer.guest),
                "operation_lock_sha256": installer.operation_binding,
                "storage_lock_sha256": installer.storage_binding,
                "intent_sha256": _hash(intent),
                "completion_sha256": _hash(completion),
                "observed_tree": installer.fingerprints(),
                "completed_steps": [
                    "intent_file_and_directory_fsync",
                    "material_file_fsync_and_readback",
                    "material_directory_fsync_and_exact_tree_readback",
                    "completion_file_and_directory_fsync",
                    "final_exact_tree_readback_and_guards",
                ],
                "execution_admission": False,
            }
    except Exception as error:  # noqa: BLE001 -- never disclose private OS/guest/input diagnostics.
        if installer is not None and installer.completion_attempted:
            raise LabMaterializationUncertain(
                "Materialization completion is unacknowledged; retain the entire generation"
            ) from None
        if isinstance(error, LabMaterializationError):
            raise
        raise LabMaterializationError(
            "Materialization refused; retain any partial generation for review"
        ) from None
