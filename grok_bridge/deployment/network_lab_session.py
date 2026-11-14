"""Held read-only lab observations, not admission or an execution permit.

The distinct, preexisting operation lock coordinates cooperating controllers only.
It stays held through the caller's scope, including the existing collector's
shorter storage lock. Neither this lock nor guest UUID/boot observations prove
hypervisor isolation, VM eligibility, operator authority, synthetic provenance or
source/image acceptance. External management-plane evidence and explicit operator
scope remain required. Space is sampled, not reserved; estimate adequacy and
freshness after any observation remain unverified. No CLI or runtime dispatch.
"""

import copy
import functools
import os
import re
import stat
import sys
import threading
from pathlib import PurePosixPath

import network_lab_bootstrap as bootstrap
import network_lab_capacity as capacity
import network_lab_storage as storage

SCHEMA = "grok-network-lab-observation-session-v1"
LOCK = "/run/codex-grok-lab-operation.lock"
DMI_UUID = "/sys/devices/virtual/dmi/id/product_uuid"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
MAX_UUID_BYTES = 64
_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"


class LabSessionError(RuntimeError):
    """Sanitized refusal without raw guest, engine, path or daemon details."""


def _require(condition, message):
    if not condition:
        raise LabSessionError(message)


def _sanitized(function):
    @functools.wraps(function)
    def operation(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except LabSessionError:
            raise
        except Exception:  # noqa: BLE001 -- do not expose private OS or input diagnostics.
            raise LabSessionError(
                "Lab observation session refused; review private prerequisites"
            ) from None

    return operation


def _peaks(value):
    _require(capacity._fields(value, capacity.ROLES), "Explicit upcoming peaks are incomplete")
    detached = {role: value[role] for role in capacity.ROLES}
    _require(
        all(capacity._byte_count(item) for item in detached.values())
        and sum(detached.values()) > 0,
        "Invalid upcoming peaks",
    )
    return detached


def _platform():
    _require(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "Lab observations require root on Linux",
    )
    _require(os.uname().machine == "x86_64", "Lab observations require Linux x86_64")


def _file_identity(value):
    return (*storage._metadata(value), value.st_nlink)


def _kernel_uuid(path, filesystem):
    """Read one fixed kernel UUID without following substituted path entries."""
    _require(
        (path, filesystem) in {(DMI_UUID, "sysfs"), (BOOT_ID, "proc")}, "Unknown guest observation"
    )
    location = PurePosixPath(path)
    with storage._DirectoryChain(location.parent.as_posix()) as parent:
        mount = parent.bindings[-1]["mount"]
        _require(
            mount["fstype"] == filesystem and mount["root"] == "/",
            "Guest identity requires its kernel filesystem",
        )
        before = os.stat(location.name, dir_fd=parent.fd, follow_symlinks=False)
        _require(
            stat.S_ISREG(before.st_mode)
            and before.st_uid == 0
            and before.st_nlink == 1
            and not before.st_mode & 0o022,
            "Unsafe guest identity file",
        )
        descriptor = os.open(
            location.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent.fd,
        )
        try:
            expected = _file_identity(before)
            _require(
                _file_identity(os.fstat(descriptor)) == expected, "Guest identity file changed"
            )
            _require(
                storage._fd_mount_id(descriptor) == mount["id"], "Guest identity mount changed"
            )
            pieces, size = [], 0
            while True:
                part = os.read(descriptor, MAX_UUID_BYTES + 1 - size)
                if not part:
                    break
                pieces.append(part)
                size += len(part)
                _require(size <= MAX_UUID_BYTES, "Guest identity exceeds read bound")
            _require(
                _file_identity(os.fstat(descriptor)) == expected, "Guest identity file changed"
            )
            _require(
                storage._fd_mount_id(descriptor) == mount["id"], "Guest identity mount changed"
            )
            current = os.stat(location.name, dir_fd=parent.fd, follow_symlinks=False)
            _require(_file_identity(current) == expected, "Guest identity path changed")
            parent.verify()
            raw = b"".join(pieces).decode("ascii")
            _require(bool(re.fullmatch(_UUID + r"\n?", raw)), "Invalid kernel guest identity")
            value = raw.removesuffix("\n").lower()
            _require(
                value != "00000000-0000-0000-0000-000000000000", "Invalid kernel guest identity"
            )
            return {
                "value": value,
                "file": expected,
                "parents": copy.deepcopy(parent.bindings),
            }
        finally:
            os.close(descriptor)


def _guest():
    _platform()
    return {
        "architecture": "x86_64",
        "uuid": _kernel_uuid(DMI_UUID, "sysfs"),
        "boot": _kernel_uuid(BOOT_ID, "proc"),
    }


class _OperationLock:
    def __init__(self):
        import fcntl

        _require(LOCK != storage.LOCK, "Operation and storage snapshot locks must differ")
        self.leaf, self.fd = None, None
        try:
            self.leaf = storage._Leaf(LOCK, "lock")
            self.fd = os.open(
                self.leaf.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                dir_fd=self.leaf.parent.fd,
            )
            _require(
                _file_identity(os.fstat(self.fd)) == self.leaf.identity,
                "Operation lock changed before acquisition",
            )
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LabSessionError("Lab operation lock is busy") from None
            self.verify()
        except BaseException:
            self.close()
            raise

    def verify(self):
        _require(self.fd is not None and self.leaf is not None, "Lab operation lock is not held")
        _require(
            _file_identity(os.fstat(self.fd)) == self.leaf.identity, "Held operation lock changed"
        )
        self.leaf.verify()

    def binding_sha256(self):
        _require(self.fd is not None and self.leaf is not None, "Lab operation lock is not held")
        return capacity._digest({"leaf": self.leaf.identity, "parents": self.leaf.parent.bindings})

    def close(self):
        descriptor, leaf = self.fd, self.leaf
        self.fd, self.leaf = None, None
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            if leaf is not None:
                leaf.close()


class _ObservationSession:
    """Single-use, single-controller observation scope; never a mutation capability."""

    def __init__(self, context, binding, peaks):
        self._source_context, self._source_binding = context, binding
        self._context = capacity._context(context)
        self._context.verify_journal_binding(binding)
        self._binding = copy.deepcopy(binding)
        self._context_digest = self._context.digest()
        self._initial_peaks = _peaks(peaks)
        self._creator_pid = os.getpid()
        self._entry_mutex = threading.Lock()
        self._lock = None
        self._guest_binding = None
        self._storage_binding = None
        self._bootstrap_binding = None
        self._operation_binding = None
        self._used = self._active = self._failed = False
        self._owner = None
        self._refreshing = False

    def _binding_guard(self):
        _require(os.getpid() == self._creator_pid, "Lab observation session cannot cross processes")
        _require(LOCK != storage.LOCK, "Operation and storage snapshot locks must differ")
        _require(
            capacity._context(self._source_context) == self._context
            and self._context.digest() == self._context_digest,
            "Lab observation context changed",
        )
        self._context.verify_journal_binding(self._binding)
        self._context.verify_journal_binding(self._source_binding)
        _require(
            capacity._exact(self._binding, self._source_binding), "Lab retained binding changed"
        )

    def _guard(self):
        _require(
            self._active is True and self._failed is False, "Lab observation session is inactive"
        )
        _require(
            threading.get_ident() == self._owner,
            "Lab observation session belongs to another controller",
        )
        self._binding_guard()
        _platform()
        self._lock.verify()
        _require(
            self._lock.binding_sha256() == self._operation_binding, "Operation lock binding changed"
        )
        guest = _guest()
        _require(
            capacity._exact(guest, self._guest_binding), "Observed guest identity or boot changed"
        )
        self._lock.verify()
        _require(
            self._lock.binding_sha256() == self._operation_binding, "Operation lock binding changed"
        )
        self._binding_guard()

    @_sanitized
    def __enter__(self):
        _require(os.getpid() == self._creator_pid, "Lab observation session cannot cross processes")
        with self._entry_mutex:
            _require(self._used is False, "Lab observation session cannot be reused")
            self._used = True
        self._binding_guard()
        _platform()
        try:
            self._lock = _OperationLock()
            self._operation_binding = self._lock.binding_sha256()
            self._owner = threading.get_ident()
            self._guest_binding = copy.deepcopy(_guest())
            _require(
                self._guest_binding["uuid"]["value"] == self._context.vm_uuid,
                "Observed guest VM identity differs",
            )
            self._active = True
            self.refresh(peaks=self._initial_peaks)
            return self
        except BaseException:
            self._active = False
            if self._lock is not None:
                self._lock.close()
            raise

    def _refresh_observation(self, detached):
        self._guard()
        result = storage.collect(self._context, peaks=detached)
        self._guard()
        _require(
            type(result) is dict
            and set(result)
            == {
                "schema",
                "scope",
                "context_sha256",
                "bindings_sha256",
                "capacity_assessment",
                "snapshot_lock_released_on_return",
                "execution_admission",
            }
            and result["schema"] == storage.SCHEMA
            and result["scope"] == "read_only_storage_observation_and_capacity_arithmetic"
            and result["context_sha256"] == self._context_digest
            and type(result["bindings_sha256"]) is str
            and bool(re.fullmatch(r"[0-9a-f]{64}", result["bindings_sha256"]))
            and result["snapshot_lock_released_on_return"] is True
            and result["execution_admission"] is False,
            "Unexpected storage observation receipt",
        )
        identity = result["bindings_sha256"]
        arithmetic = result["capacity_assessment"]
        _require(
            type(arithmetic) is dict
            and set(arithmetic)
            == {
                "schema",
                "mode",
                "status",
                "scope",
                "context_sha256",
                "inputs_sha256",
                "filesystems",
            }
            and arithmetic["schema"] == capacity.SCHEMA
            and arithmetic["mode"] == "lab"
            and arithmetic["status"] == "supplied_capacity_arithmetic_satisfied"
            and arithmetic["scope"] == "supplied_values_only_not_execution_admission"
            and arithmetic["context_sha256"] == self._context_digest,
            "Unexpected nested capacity context or scope",
        )
        _require(
            self._storage_binding is None or self._storage_binding == identity,
            "Observed storage binding changed during session",
        )
        self._storage_binding = identity
        return {
            "schema": SCHEMA,
            "scope": "read_only_guest_storage_observation_under_operation_lock",
            "context_sha256": self._context_digest,
            "guest_binding_sha256": capacity._digest(self._guest_binding),
            "storage_observation": copy.deepcopy(result),
            "operation_lock_held_at_observation": True,
            "operation_lock_binding_sha256": self._operation_binding,
            "execution_admission": False,
        }

    @_sanitized
    def refresh(self, *, peaks):
        detached = _peaks(peaks)
        _require(os.getpid() == self._creator_pid, "Lab observation session cannot cross processes")
        _require(
            threading.get_ident() == self._owner,
            "Lab observation session belongs to another controller",
        )
        _require(self._refreshing is False, "Lab observation refresh is already active")
        self._refreshing = True
        try:
            return self._refresh_observation(detached)
        except BaseException:
            self._failed = True
            raise
        finally:
            self._refreshing = False

    @_sanitized
    def observe_bootstrap_engine(self, *, peaks):
        """Sample only an empty bootstrap engine while retaining this advisory lock.

        Fresh storage surrounds the two inventory samples. Neither matching
        samples nor the held lock exclude transient changes or grant admission.
        Existing/retained containers, volumes or custom networks are not adopted.
        """
        detached = _peaks(peaks)
        _require(os.getpid() == self._creator_pid, "Lab observation session cannot cross processes")
        _require(
            threading.get_ident() == self._owner,
            "Lab observation session belongs to another controller",
        )
        _require(self._refreshing is False, "Lab observation refresh is already active")
        self._refreshing = True
        try:
            before = self._refresh_observation(detached)
            self._guard()
            observed = bootstrap.collect(self._context, lab_binding=self._binding)
            self._guard()
            _require(
                type(observed) is dict
                and set(observed)
                == {
                    "schema",
                    "scope",
                    "context_sha256",
                    "engine_socket_peer_sha256",
                    "inventory_before_sha256",
                    "inventory_after_sha256",
                    "observed_counts",
                    "required_image_inspections",
                    "unrelated_images_observed",
                    "execution_admission",
                }
                and observed["schema"] == bootstrap.SCHEMA
                and observed["scope"] == bootstrap.SCOPE
                and observed["context_sha256"] == self._context_digest
                and all(
                    type(observed[key]) is str
                    and bool(re.fullmatch(r"[0-9a-f]{64}", observed[key]))
                    for key in (
                        "engine_socket_peer_sha256",
                        "inventory_before_sha256",
                        "inventory_after_sha256",
                    )
                )
                and observed["inventory_before_sha256"] == observed["inventory_after_sha256"]
                and capacity._exact(
                    observed["observed_counts"],
                    {"containers": 0, "volumes": 0, "networks": 3, "endpoints": 0},
                )
                and type(observed["required_image_inspections"]) is int
                and observed["required_image_inspections"] == 3
                and observed["unrelated_images_observed"] is False
                and observed["execution_admission"] is False,
                "Unexpected bootstrap observation receipt",
            )
            observed = copy.deepcopy(observed)
            binding = (observed["engine_socket_peer_sha256"], observed["inventory_after_sha256"])
            _require(
                self._bootstrap_binding is None or self._bootstrap_binding == binding,
                "Observed bootstrap binding changed during session",
            )
            after = self._refresh_observation(detached)
            self._guard()
            self._bootstrap_binding = binding
            return {
                "schema": SCHEMA,
                "scope": "read_only_empty_engine_bootstrap_under_operation_lock",
                "context_sha256": self._context_digest,
                "guest_binding_sha256": capacity._digest(self._guest_binding),
                "storage_before": before["storage_observation"],
                "storage_after": after["storage_observation"],
                "bootstrap_observation": observed,
                "operation_lock_held_at_observation": True,
                "operation_lock_binding_sha256": self._operation_binding,
                "execution_admission": False,
            }
        except BaseException:
            self._failed = True
            raise
        finally:
            self._refreshing = False

    @_sanitized
    def __exit__(self, exc_type, exc_value, traceback):
        _require(
            os.getpid() != self._creator_pid or threading.get_ident() == self._owner,
            "Only the owning controller can close its observation session",
        )
        try:
            _require(
                os.getpid() == self._creator_pid, "Lab observation session cannot cross processes"
            )
            if exc_type is None and not self._failed:
                self._guard()
        finally:
            self._active = False
            if self._lock is not None:
                self._lock.close()
        return False


@_sanitized
def observation_session(context, *, lab_binding, peaks):
    """Prepare a single-use scope; acquire the existing lock only on entry.

    Entry observes the guest and storage once before yielding. refresh repeats
    real observations with an explicit new peak estimate while the lock remains
    held. observe_bootstrap_engine additionally samples an empty engine and its
    three required images, surrounded by fresh storage observations. These calls
    share the same non-reentrant controller guard. The lock is released on
    exit, including BaseException; returned records then remain historical only.
    The complete caller-supplied binding is verified, never generated here.
    Nothing in this API admits a VM, verifies consent, or authorizes allocation.
    """
    return _ObservationSession(context, lab_binding, peaks)
