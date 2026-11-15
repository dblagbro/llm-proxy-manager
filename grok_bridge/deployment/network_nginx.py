"""Pre-cutover staged Nginx checks with one retained, exactly owned fixture.

Caller holds the release lock/capacity guard. Host.run captures diagnostics only
in the private release log and selects the local Docker socket. No production
container is started/stopped/reconfigured. The fixture uses the retained default
network only; it never joins the dedicated network or borrows a namespace.

This checks configuration parsing before original Grok is stopped. It does not
prove candidate reachability; post-cutover nginx -t/reload and real acceptance
must use the production Nginx container. All returned errors are sanitized.

An explicit lab descriptor selects a separate context-bound ledger and physical
resource names. This is consistency checking only: it does not observe VM or
engine identity or grant isolation, storage, image or execution admission.
"""

import functools
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

import network_nginx_inputs as inputs

DEFAULT = "docker_default"
DEDICATED = "docker_grok_security"
BRIDGE = "llm-proxy2-grok-bridge"
OWNER = "com.tmr.security.grok-nginx-validation"
LEDGER = "nginx-validation.private.json"
LAB_LEDGER = "nginx-validation.lab.private.json"
LAB_OWNER = OWNER + ".lab-context"
MEMORY = 128 * 1024 * 1024
# nginx -t assigns its new temporary cache directories to the configured worker.
# Only owned scratch/tmpfs is writable; every original bind is recursively read-only.
CAP_ADD = ["CAP_CHOWN"]
TMPFS = {
    "/tmp": "rw,noexec,nosuid,size=16m",
    "/var/cache/nginx": "rw,noexec,nosuid,size=16m",
    "/var/run": "rw,noexec,nosuid,size=2m",
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TX = re.compile(r"[0-9a-f]{32}\Z")


class NginxValidationError(RuntimeError):
    """Private diagnostics belong in the caller's log, never this exception."""


class LabBindingError(NginxValidationError):
    """Lost lab consistency; do not reconcile a fixture after this refusal."""


def _lab_context(context):
    from network_lab_context import LabContext, from_record

    try:
        if type(context) is not LabContext or from_record(context.to_record()) != context:
            raise ValueError
        return context
    except Exception:
        raise LabBindingError("Invalid lab validation context") from None


def _lab_logical_path(value, generation, *, exact=False):
    if not isinstance(value, (str, Path, PurePosixPath)):
        raise LabBindingError("Invalid lab validation path")
    text = str(value)
    path = PurePosixPath(text)
    if (
        str(path) != text
        or any(character in text for character in "\\,\r\n\0")
        or not path.is_absolute()
        or ".." in path.parts
        or (path != generation if exact else generation not in path.parents)
    ):
        raise LabBindingError("Lab validation path is outside its exact generation")
    return path


def _lab_filesystem_path(logical, generation):
    """Pin every ancestor without resolving symlinks or changing any permissions."""
    if os.name != "posix":
        raise LabBindingError("Lab validation paths require POSIX filesystem semantics")
    path = Path(logical)
    chain = []
    for item in [*reversed(path.parents), path]:
        info = item.lstat()
        inside = item == Path(generation) or Path(generation) in item.parents
        if (
            stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & (0o077 if inside else 0o022)
            or (item != path and not stat.S_ISDIR(info.st_mode))
            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
            or (
                inside
                and stat.S_IMODE(info.st_mode) != (0o700 if stat.S_ISDIR(info.st_mode) else 0o600)
            )
        ):
            raise LabBindingError("Lab validation path ownership or type requires review")
        chain.append((info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid))
    return path, tuple(chain)


def _opposite_ledger(folder, lab):
    opposite = folder / (LEDGER if lab else LAB_LEDGER)
    if opposite.exists() or opposite.is_symlink():
        raise LabBindingError("Opposite-mode validation ledger requires review")


class _LabScope:
    """Consistency guard only: no VM, engine, isolation or execution admission."""

    def __init__(self, context, folder, *, host=None, nginx=None, bridge=None):
        self.context = _lab_context(context)
        self.context_digest = context.digest()
        self.logical_folder = _lab_logical_path(folder, context.generation_root, exact=True)
        self.host = host
        self.originals = []
        self.paths = {}
        pins = dict(context.images)
        names = context.resource_map()
        for value, role, pin in ((nginx, "nginx", "nginx"), (bridge, BRIDGE, "original")):
            if value is None:
                continue
            if (
                value.get("Name") != "/" + names[role]
                or not _hex(value.get("Id"))
                or value.get("Image") != pins[pin]
                or set(value.get("NetworkSettings", {}).get("Networks", {})) != {names[DEFAULT]}
            ):
                raise LabBindingError("Original lab runtime identity differs")
            self.originals.append((value, _digest(value)))
        if nginx is not None:
            mounts = nginx.get("Mounts")
            if type(mounts) is not list or not mounts or len(mounts) > 32:
                raise LabBindingError("Invalid lab Nginx mount inventory")
            for mount in mounts:
                if type(mount) is not dict or mount.get("Type") != "bind":
                    raise LabBindingError("Lab Nginx requires generation-owned bind sources")
                source = _lab_logical_path(mount.get("Source"), context.generation_root)
                self.paths[source] = None
        self.folder, fingerprint = _lab_filesystem_path(
            self.logical_folder, context.generation_root
        )
        if not self.folder.is_dir():
            raise LabBindingError("Lab validation generation is not a directory")
        self.paths[self.logical_folder] = fingerprint
        for path in self.paths:
            self.paths[path] = _lab_filesystem_path(path, context.generation_root)[1]
        _opposite_ledger(self.folder, True)
        state = _load(self.folder, lab_context=context)
        self.state = state
        self.expected_ledger = state.get("_ledger_sha256") if state else None
        self.guard()

    def guard(self):
        try:
            if _lab_context(self.context).digest() != self.context_digest:
                raise ValueError
            if self.host is not None:
                _lab_logical_path(self.host.folder, self.context.generation_root, exact=True)
            if any(_digest(value) != digest for value, digest in self.originals):
                raise ValueError
            for path, expected in self.paths.items():
                if _lab_filesystem_path(path, self.context.generation_root)[1] != expected:
                    raise ValueError
            _opposite_ledger(self.folder, True)
            _expected_ledger(self.folder / LAB_LEDGER, self.expected_ledger)
        except Exception:
            raise LabBindingError(
                "Lab validation binding changed; retained artifacts require review"
            ) from None


class _LabHost:
    def __init__(self, host, scope):
        self._host, self._scope = host, scope

    def _call(self, method, *args, **kwargs):
        self._scope.guard()
        try:
            return getattr(self._host, method)(*args, **kwargs)
        finally:
            self._scope.guard()

    def inspect(self, name):
        return self._call("inspect", name)

    def image(self, image_id):
        return self._call("image", image_id)

    def run(self, argv, timeout):
        return self._call("run", argv, timeout=timeout)


def _sanitized(function):
    @functools.wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except NginxValidationError:
            raise
        except Exception:  # noqa: BLE001 -- host errors may contain private diagnostics.
            raise NginxValidationError(
                "Validation operation failed; inspect private ledger/log"
            ) from None

    return call


def _require(condition, message):
    if not condition:
        raise NginxValidationError(message)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _hex(value):
    return type(value) is str and bool(_HEX.fullmatch(value))


def _config(value):
    return _digest(
        {key: value.get(key) for key in ("Image", "Config", "HostConfig", "Mounts", "Path", "Args")}
    )


def _fixture_config(value):
    """Normalize only Docker's observed false/null OOM default on fixture start."""
    fields = {
        key: value.get(key) for key in ("Image", "Config", "HostConfig", "Mounts", "Path", "Args")
    }
    fields["HostConfig"] = dict(fields["HostConfig"])
    oom = fields["HostConfig"].get("OomKillDisable")
    _require(oom is None or oom is False, "OOM kill protection must remain enabled")
    fields["HostConfig"]["OomKillDisable"] = False
    return _digest(fields)


def _private_regular(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _require(stat.S_ISREG(info.st_mode), "Private input is not a regular file")
        _require(info.st_size <= 16 * 1024 * 1024, "Private input exceeds reviewed bound")
        value = stream.read(16 * 1024 * 1024 + 1)
        _require(len(value) <= 16 * 1024 * 1024, "Private input exceeds reviewed bound")
        return value


def _folder(folder):
    folder = Path(folder)
    _require(
        folder.is_absolute() and not folder.is_symlink(),
        "Release directory requires review",
    )
    info = folder.stat()
    _require(stat.S_ISDIR(info.st_mode), "Release directory is absent")
    if os.name == "posix":
        _require(info.st_mode & 0o077 == 0, "Release directory must remain private")
    return folder


def _expected_ledger(path, expected):
    _require(not path.is_symlink(), "Validation ledger must not be a symlink")
    if expected is None:
        _require(not path.exists(), "Validation ledger appeared concurrently")
    else:
        _require(
            path.exists() and hashlib.sha256(_private_regular(path)).hexdigest() == expected,
            "Validation ledger changed concurrently",
        )


def _write_pending(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def _save(folder, state, *, scope=None):
    """Atomic journal write under caller's lock; expected digest is never rebased.

    Failed pending files are retained privately. Compare again after the pending
    file is durable. The caller's lock serializes cooperating controllers; this
    is not a security boundary against a concurrent filesystem administrator.
    """
    _ledger_binding(state, scope.context if scope is not None else None)
    if scope is not None:
        scope.guard()
        if state.get("_ledger_sha256") != scope.expected_ledger:
            raise LabBindingError("Lab validation save receipt differs")
    path = folder / (LAB_LEDGER if scope is not None else LEDGER)
    _opposite_ledger(folder, scope is not None)
    expected = state.get("_ledger_sha256")
    _expected_ledger(path, expected)
    payload = {key: value for key, value in state.items() if key != "_ledger_sha256"}
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    pending = folder / ("nginx-validation.pending-" + uuid.uuid4().hex + ".private.json")
    _write_pending(pending, data)
    if scope is not None:
        scope.guard()
    _expected_ledger(path, expected)
    os.replace(pending, path)
    if os.name == "posix":
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    state["_ledger_sha256"] = hashlib.sha256(data).hexdigest()
    if scope is not None:
        # Only this operation's completed owned save advances the expected receipt.
        scope.expected_ledger = state["_ledger_sha256"]
        scope.guard()


def _ledger_binding(value, context):
    if context is None:
        if value.get("schema") == 2 or "mode" in value or "lab_context" in value:
            raise LabBindingError("Lab validation ledger cannot be used in production")
        _require(
            type(value.get("schema")) is int and value["schema"] == 1,
            "Invalid validation ledger",
        )
        return
    try:
        context = _lab_context(context)
        if (
            type(value.get("schema")) is not int
            or value["schema"] != 2
            or value.get("mode") != "lab"
        ):
            raise ValueError
        context.verify_journal_binding(value.get("lab_context"))
        if value.get("image") != dict(context.images)["nginx"]:
            raise ValueError
    except Exception:
        raise LabBindingError("Lab validation ledger context differs") from None


def _load(folder, *, lab_context=None):
    if lab_context is not None:
        _lab_context(lab_context)
    else:
        from network_lab_context import ROOT

        if PurePosixPath(folder) == ROOT or ROOT in PurePosixPath(folder).parents:
            raise LabBindingError("Lab validation directory requires its exact context")
    _opposite_ledger(folder, lab_context is not None)
    path = folder / (LAB_LEDGER if lab_context is not None else LEDGER)
    _require(not path.is_symlink(), "Validation ledger must not be a symlink")
    if not path.exists():
        _require(not path.is_symlink(), "Validation ledger requires review")
        return None
    try:
        raw = _private_regular(path)

        def unique(pairs):
            result = {}
            for key, item in pairs:
                _require(key not in result, "Invalid private validation ledger")
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique)
        _ledger_binding(value, lab_context)
        _require("_ledger_sha256" not in value, "Invalid private validation ledger")
        _require(
            type(value["schema"]) is int
            and value["schema"] == (2 if lab_context is not None else 1)
            and _TX.fullmatch(value["transaction"]),
            "Invalid validation ledger",
        )
        _require(
            value["name"] == "codex-grok-nginxcheck-" + value["transaction"],
            "Invalid fixture name",
        )
        _require(_hex(value["default_network_id"]), "Invalid validation network identity")
        _require(_hex(value["config_sha256"]), "Invalid staged configuration fingerprint")
        _require(value.get("id") is None or _hex(value["id"]), "Invalid fixture receipt")
        value["_ledger_sha256"] = hashlib.sha256(raw).hexdigest()
        return value
    except (KeyError, TypeError, ValueError):
        raise NginxValidationError("Invalid private validation ledger") from None


def _same_runtime(current, original, name, dedicated_id=None, *, lab_context=None):
    names = lab_context.resource_map() if lab_context is not None else {}
    default, dedicated = names.get(DEFAULT, DEFAULT), names.get(DEDICATED, DEDICATED)
    _require(
        current.get("Id") == original.get("Id")
        and _hex(current.get("Id"))
        and current.get("Name") == "/" + name
        and current.get("State", {}).get("Running") is True
        and _config(current) == _config(original),
        "Pre-cutover runtime identity or configuration changed",
    )
    old = original["NetworkSettings"]["Networks"]
    actual = current["NetworkSettings"]["Networks"]
    _require(set(old) == {default}, "Original network topology requires review")
    _require(
        default in actual and _digest(actual[default]) == _digest(old[default]),
        "Original endpoint changed",
    )
    _require(_hex(old[default].get("NetworkID")), "Original network identity is invalid")
    allowed = {default}
    if dedicated_id is not None:
        _require(
            name == names.get("nginx", "nginx") and _hex(dedicated_id),
            "Dedicated validation binding is invalid",
        )
        allowed.add(dedicated)
        if dedicated in actual:
            _require(
                actual[dedicated].get("NetworkID") == dedicated_id,
                "Dedicated endpoint differs",
            )
    _require(set(actual) <= allowed, "Unreviewed runtime network attachment")


def _runtime(host, original_nginx, original_bridge, dedicated_id, *, lab_context=None):
    names = lab_context.resource_map() if lab_context is not None else {}
    nginx_name, bridge_name = names.get("nginx", "nginx"), names.get(BRIDGE, BRIDGE)
    default = names.get(DEFAULT, DEFAULT)
    nginx, bridge = host.inspect(nginx_name), host.inspect(bridge_name)
    _same_runtime(nginx, original_nginx, nginx_name, dedicated_id, lab_context=lab_context)
    _same_runtime(bridge, original_bridge, bridge_name, lab_context=lab_context)
    _require(
        nginx["NetworkSettings"]["Networks"][default]["NetworkID"]
        == bridge["NetworkSettings"]["Networks"][default]["NetworkID"],
        "Original upstream is not on the retained default network",
    )
    return _digest([nginx["Id"], nginx["NetworkSettings"], bridge["Id"], bridge["NetworkSettings"]])


def _path(value):
    _require(
        type(value) is str and not any(char in value for char in ",\r\n\0"),
        "Unsafe mount path",
    )
    path = PurePosixPath(value)
    _require(
        path.is_absolute() and ".." not in path.parts and value != "/",
        "Unreviewed mount path",
    )
    _require(
        not any(
            path == root or root in path.parents
            for root in map(PurePosixPath, ("/proc", "/sys", "/dev", "/run"))
        ),
        "Special host path is not allowed",
    )
    _require(value != "/var/run/docker.sock", "Docker socket mount is not allowed")
    return value


def _mounts(nginx, config, logs):
    result = []
    for item in nginx["Mounts"]:
        target = _path(item["Destination"])
        source = _path(item["Source"])
        _require(item.get("Type") in {"bind", "volume"}, "Unreviewed production mount type")
        if target == "/etc/nginx/nginx.conf":
            source = str(config)
        elif target == "/var/log/nginx":
            source = str(logs)
        _require(target not in TMPFS, "Production mount conflicts with validation tmpfs")
        result.append(
            {
                "Source": source,
                "Destination": target,
                "RW": target == "/var/log/nginx",
                "Type": "bind",
            }
        )
    targets = [row["Destination"] for row in result]
    _require(
        len(targets) == len(set(targets)) and len(targets) <= 32,
        "Ambiguous validation mounts",
    )
    _require(
        {"/etc/nginx/nginx.conf", "/var/log/nginx"} <= set(targets),
        "Required Nginx mounts are absent",
    )
    return sorted(result, key=lambda row: row["Destination"])


def _input_overrides(folder, state, nginx):
    expected = {
        item["Destination"]: item["Source"]
        for item in nginx["Mounts"]
        if item["Destination"] in inputs.TARGETS
    }
    record = state.get("config_inputs")
    if not expected:
        _require(record is None, "Unexpected private include snapshot")
        _require(
            not (folder / inputs.COPY_ROOT).exists(),
            "Unexpected include copy directory",
        )
        return {}
    _require(type(record) is dict, "Private include snapshot is missing")
    trees = record.get("trees")
    _require(type(trees) is dict and set(trees) == set(expected), "Include targets changed")
    _require(
        all(trees[target].get("source") == source for target, source in expected.items()),
        "Original include binding changed",
    )
    return inputs.overrides(folder, record)


def _reviewed_mounts(folder, state, nginx):
    mounts = _mounts(nginx, folder / "nginx.after", folder / "nginx-validation-logs")
    overrides = _input_overrides(folder, state, nginx)
    for mount in mounts:
        if mount["Destination"] in overrides:
            mount["Source"] = _path(overrides[mount["Destination"]])
    return mounts


def _verify_validation_inputs(folder, original_nginx, state):
    if state is None:
        _require(not (folder / inputs.COPY_ROOT).exists(), "Incomplete include copy retained")
        return
    _require(
        state["original_nginx_id"] == original_nginx["Id"]
        and state["original_nginx_sha256"] == _config(original_nginx),
        "Include snapshot original runtime differs",
    )
    _require(
        _reviewed_mounts(folder, state, original_nginx) == state["mounts"],
        "Private include mount binding changed",
    )


@_sanitized
def verify_validation_inputs(folder, original_nginx, *, lab_context=None):
    """Read-only input consistency audit; a lab descriptor is not admission."""
    scope = (
        _LabScope(lab_context, folder, nginx=original_nginx) if lab_context is not None else None
    )
    if lab_context is None and str(original_nginx.get("Name", "")).startswith(
        "/codex-grok-nettest-"
    ):
        raise LabBindingError("Lab validation runtime requires its exact context")
    folder = scope.folder if scope else _folder(folder)
    state = scope.state if scope else _load(folder)
    if scope:
        scope.guard()
    _verify_validation_inputs(folder, original_nginx, state)
    if scope:
        scope.guard()


def _fixture(value, state, *, lab_context=None):
    _ledger_binding(state, lab_context)
    default = lab_context.resource_map()[DEFAULT] if lab_context is not None else DEFAULT
    _require(
        _hex(value.get("Id")) and value.get("Name") == "/" + state["name"],
        "Validation identity differs",
    )
    _require(state.get("id") in (None, value["Id"]), "Validation container was replaced")
    _require(
        value.get("Image") == state["image"]
        and value.get("Path") == "nginx"
        and value.get("Args") == ["-t"],
        "Validation executable differs",
    )
    config, hc = value["Config"], value["HostConfig"]
    defaults = state["image_defaults"]
    labels = dict(defaults.get("Labels") or {})
    labels[OWNER] = state["transaction"]
    if lab_context is not None:
        labels[LAB_OWNER] = lab_context.digest()
    _require(config.get("Labels") == labels, "Validation ownership differs")
    _require(
        config.get("Image") == state["image"]
        and config.get("Hostname") == state["name"]
        and not config.get("Domainname"),
        "Validation image or hostname declaration differs",
    )
    for key in (
        "Env",
        "User",
        "WorkingDir",
        "ExposedPorts",
        "Healthcheck",
        "StopSignal",
    ):
        expected = defaults.get(key)
        # Docker image inspection can omit User, while container creation records
        # the unchanged default as an empty string. Do not normalize a named user.
        if key == "User" and expected is None:
            expected = ""
        _require(config.get(key) == expected, "Validation image defaults changed")
    _require(
        config.get("Entrypoint") == ["nginx"]
        and config.get("Cmd") == ["-t"]
        and not config.get("Volumes"),
        "Validation command or volumes differ",
    )
    exact = {
        "NetworkMode": state["default_network_id"],
        "ReadonlyRootfs": True,
        "Memory": MEMORY,
        "NanoCpus": 500000000,
        "PidsLimit": 64,
        "CapDrop": ["ALL"],
        "CapAdd": CAP_ADD,
        "SecurityOpt": ["no-new-privileges:true"],
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "Runtime": "runc",
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Tmpfs": TMPFS,
        "LogConfig": {
            "Type": "json-file",
            "Config": {"max-size": "1m", "max-file": "1"},
        },
    }
    _require(
        all(hc.get(key) == val for key, val in exact.items()),
        "Validation execution bounds changed",
    )
    for key in (
        "Privileged",
        "PublishAllPorts",
        "AutoRemove",
        "PortBindings",
        "Devices",
        "DeviceRequests",
        "DeviceCgroupRules",
        "Binds",
        "VolumesFrom",
        "Links",
        "ExtraHosts",
        "Dns",
        "DnsSearch",
        "DnsOptions",
        "UsernsMode",
        "CgroupParent",
        "Sysctls",
        "OomKillDisable",
        "PidMode",
        "UTSMode",
    ):
        _require(not hc.get(key), "Unreviewed validation host setting")
    actual_mounts = []
    for item in value["Mounts"]:
        if item.get("Type") == "tmpfs":
            _require(
                item.get("Destination") in TMPFS and not item.get("Source"),
                "Unexpected tmpfs mount",
            )
        else:
            actual_mounts.append(
                {key: item.get(key) for key in ("Source", "Destination", "RW", "Type")}
            )
    _require(
        sorted(actual_mounts, key=lambda row: row["Destination"]) == state["mounts"],
        "Validation mount differs",
    )
    declarations = hc.get("Mounts")
    _require(type(declarations) is list, "Validation bind declarations are absent")
    declared_mounts = []
    for mount in declarations:
        _require(
            set(mount) <= {"Type", "Source", "Target", "ReadOnly", "BindOptions"}
            and mount.get("Type") == "bind"
            and type(mount.get("ReadOnly", False)) is bool,
            "Unreviewed validation bind declaration",
        )
        options = mount.get("BindOptions")
        _require(
            type(options) is dict
            and set(options)
            <= {
                "Propagation",
                "ReadOnlyForceRecursive",
                "ReadOnlyNonRecursive",
                "NonRecursive",
                "CreateMountpoint",
            }
            and options.get("Propagation") == "rprivate",
            "Unreviewed bind propagation",
        )
        for option in ("ReadOnlyNonRecursive", "NonRecursive", "CreateMountpoint"):
            _require(
                options.get(option) is None or options.get(option) is False,
                "Unreviewed bind mount option",
            )
        force = options.get("ReadOnlyForceRecursive")
        readonly = mount.get("ReadOnly", False)
        _require(
            force is True if readonly else force is None or force is False,
            "Read-only binds must require recursive read-only enforcement",
        )
        declared_mounts.append(
            {
                "Source": mount.get("Source"),
                "Destination": mount.get("Target"),
                "RW": not readonly,
                "Type": "bind",
            }
        )
    _require(
        sorted(declared_mounts, key=lambda row: row["Destination"]) == state["mounts"],
        "Validation bind declarations differ",
    )
    nets = value["NetworkSettings"]["Networks"]
    _require(set(nets) == {default}, "Validation fixture must use only the default network")
    _require(
        nets[default].get("NetworkID") in (state["default_network_id"], ""),
        "Validation network changed",
    )
    _require(
        not value["State"].get("Running")
        or nets[default].get("NetworkID") == state["default_network_id"],
        "Running validation network is unresolved",
    )
    endpoint = nets[default]
    for key in ("Aliases", "DNSNames"):
        aliases = endpoint.get(key) or []
        _require(
            type(aliases) is list
            and all(alias in {state["name"], value["Id"][:12]} for alias in aliases),
            "Unreviewed validation network alias",
        )
    _require(
        not endpoint.get("IPAMConfig")
        and not endpoint.get("DriverOpts")
        and not endpoint.get("Links")
        and endpoint.get("GwPriority", 0) == 0,
        "Unreviewed validation endpoint option",
    )
    if state.get("fixture_sha256"):
        _require(
            _fixture_config(value) == state["fixture_sha256"],
            "Validation configuration fingerprint changed",
        )
    return value["Id"]


def _lookup(host, name):
    ids = host.run(
        [
            "docker",
            "container",
            "ls",
            "-a",
            "--no-trunc",
            "--filter",
            "name=^/" + name + "$",
            "--format",
            "{{.ID}}",
        ],
        timeout=30,
    ).split()
    _require(len(ids) <= 1 and all(_hex(cid) for cid in ids), "Ambiguous validation lookup")
    return host.inspect(ids[0]) if ids else None


def _create_args(state, *, lab_context=None):
    _ledger_binding(state, lab_context)
    args = [
        "docker",
        "create",
        "--name",
        state["name"],
        "--hostname",
        state["name"],
        "--label",
        OWNER + "=" + state["transaction"],
        "--pull",
        "never",
        "--restart",
        "no",
        "--network",
        state["default_network_id"],
        "--read-only",
        "--cap-drop",
        "ALL",
        "--cap-add",
        "CAP_CHOWN",
        "--security-opt",
        "no-new-privileges:true",
        "--memory",
        "128m",
        "--cpus",
        "0.5",
        "--pids-limit",
        "64",
        "--ipc",
        "private",
        "--cgroupns",
        "private",
        "--runtime",
        "runc",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=1",
        "--entrypoint",
        "nginx",
    ]
    if lab_context is not None:
        args.extend(["--label", LAB_OWNER + "=" + lab_context.digest()])
    for path, options in TMPFS.items():
        args.extend(["--tmpfs", path + ":" + options])
    for mount in state["mounts"]:
        argument = "type=bind,src=" + mount["Source"] + ",dst=" + mount["Destination"]
        argument += ",bind-propagation=rprivate"
        args.extend(
            [
                "--mount",
                argument + ("" if mount["RW"] else ",readonly,bind-recursive=readonly"),
            ]
        )
    return [*args, state["image"], "-t"]


@_sanitized
def retained_validation_ids(folder, inspected_container_list, *, lab_context=None):
    """Exclude only the exact, unchanged, stopped fixture; never prefix-match."""
    scope = _LabScope(lab_context, folder) if lab_context is not None else None
    folder = scope.folder if scope else _folder(folder)
    state = scope.state if scope else _load(folder)
    if scope:
        scope.guard()
    if state is None or state.get("id") is None:
        return set()
    matches = [item for item in inspected_container_list if item.get("Id") == state["id"]]
    _require(len(matches) == 1, "Retained validation fixture is absent or ambiguous")
    _fixture(matches[0], state, lab_context=lab_context)
    _require(
        not matches[0]["State"].get("Running") and not matches[0]["State"].get("Restarting"),
        "Retained validation fixture is not stopped",
    )
    if scope:
        scope.guard()
    return {state["id"]}


@_sanitized
def validate_staged_nginx(
    host, config, original_nginx, original_bridge, *, dedicated_network_id=None, lab_context=None
):
    """Run one retained fixture before cutover; return its exact owned ID.

    Host exposes folder, run(argv, timeout), inspect(name_or_id), image(image_id).
    Only the caller's private log may contain raw Docker/Nginx diagnostics.
    """
    scope = None
    if lab_context is not None:
        scope = _LabScope(
            lab_context, host.folder, host=host, nginx=original_nginx, bridge=original_bridge
        )
        logical_config = _lab_logical_path(config, lab_context.generation_root)
        if logical_config != lab_context.generation_root / "nginx.after":
            raise LabBindingError("Use the exact lab staged Nginx artifact")
        scope.paths[logical_config] = _lab_filesystem_path(
            logical_config, lab_context.generation_root
        )[1]
        folder, config = scope.folder, scope.folder / "nginx.after"
        host = _LabHost(host, scope)
    else:
        if any(
            str(value.get("Name", "")).startswith("/codex-grok-nettest-")
            for value in (original_nginx, original_bridge)
        ):
            raise LabBindingError("Lab validation runtime requires its exact context")
        folder, config = _folder(host.folder), Path(config)
    # Check both ledger modes before inspecting any host or creating private copies.
    state = scope.state if scope else _load(folder)
    if scope:
        scope.guard()
    _require(config == folder / "nginx.after", "Use the reviewed staged Nginx artifact")
    source = _private_regular(config)
    checksum = hashlib.sha256(source).hexdigest()
    baseline = _runtime(
        host, original_nginx, original_bridge, dedicated_network_id, lab_context=lab_context
    )
    if state is None:
        logs = folder / "nginx-validation-logs"
        _require(not logs.exists(), "Validation scratch already exists without a ledger")
        image = host.image(original_nginx["Image"])
        _require(
            image.get("Id") == original_nginx["Image"] and not image["Config"].get("Volumes"),
            "Validation image has unreviewed volumes",
        )
        # Validate the full mount inventory before copying only the three bounded
        # configuration trees. Private copies avoid granting DAC-bypass capabilities or
        # changing the live source's access modes. Failed partial copies remain.
        _mounts(original_nginx, config, logs)
        if scope:
            scope.guard()
        config_inputs = (
            inputs.prepare(folder, original_nginx["Mounts"])
            if any(m["Destination"] in inputs.TARGETS for m in original_nginx["Mounts"])
            else None
        )
        tx = uuid.uuid4().hex
        state = {
            "schema": 1,
            "phase": "create_intent",
            "transaction": tx,
            "name": "codex-grok-nginxcheck-" + tx,
            "image": original_nginx["Image"],
            "image_defaults": image["Config"],
            "config_sha256": checksum,
            "default_network_id": original_nginx["NetworkSettings"]["Networks"][
                lab_context.resource_map()[DEFAULT] if lab_context is not None else DEFAULT
            ]["NetworkID"],
            "config_inputs": config_inputs,
            "id": None,
            "original_nginx_id": original_nginx["Id"],
            "original_bridge_id": original_bridge["Id"],
            "original_nginx_sha256": _config(original_nginx),
            "original_bridge_sha256": _config(original_bridge),
        }
        if scope:
            state.update(schema=2, mode="lab", lab_context=lab_context.journal_binding())
        state["mounts"] = _reviewed_mounts(folder, state, original_nginx)
        _save(folder, state, scope=scope)
    _require(
        state["config_sha256"] == checksum
        and state["original_nginx_id"] == original_nginx["Id"]
        and state["original_bridge_id"] == original_bridge["Id"],
        "Validation inputs changed",
    )
    _require(
        state["original_nginx_sha256"] == _config(original_nginx)
        and state["original_bridge_sha256"] == _config(original_bridge),
        "Original configuration review changed",
    )
    _verify_validation_inputs(folder, original_nginx, state)
    logs = folder / "nginx-validation-logs"
    if not logs.exists():
        _require(not logs.is_symlink(), "Validation scratch requires review")
        if scope:
            scope.guard()
        logs.mkdir(mode=0o700)
    _folder(logs)
    if scope:
        logical_logs = lab_context.generation_root / "nginx-validation-logs"
        scope.paths[logical_logs] = _lab_filesystem_path(logical_logs, lab_context.generation_root)[
            1
        ]
        scope.guard()
    value = _lookup(host, state["name"])
    if value is None:
        _require(state.get("id") is None, "Retained validation fixture disappeared")
        try:
            result = host.run(_create_args(state, lab_context=lab_context), timeout=60).strip()
            _require(_hex(result), "Invalid creation receipt")
            state["id"] = result
            _save(folder, state, scope=scope)
        except LabBindingError:
            raise
        except Exception as exc:  # noqa: BLE001 -- reconcile effect instead of exposing diagnostics.
            # Persisted unique intent plus strict desired inspection can reconcile
            # a daemon-side create whose CLI response was lost.
            state["create_error_class"] = type(exc).__name__
            _save(folder, state, scope=scope)
        value = _lookup(host, state["name"])
        _require(value is not None, "Validation creation did not complete")
    cid = _fixture(value, state, lab_context=lab_context)
    state.update(id=cid, fixture_sha256=_fixture_config(value), phase="created_retained")
    _save(folder, state, scope=scope)
    error = None
    binding_failed = False
    try:
        if value["State"].get("Running"):
            state["phase"] = "stop_interrupted_validation_intent"
            _save(folder, state, scope=scope)
            host.run(["docker", "stop", "--time", "5", cid], timeout=15)
            value = host.inspect(cid)
            _fixture(value, state, lab_context=lab_context)
            _require(
                not value["State"].get("Running"),
                "Interrupted validation remains running",
            )
        _require(
            _runtime(
                host, original_nginx, original_bridge, dedicated_network_id, lab_context=lab_context
            )
            == baseline,
            "Runtime changed before staged validation",
        )
        _require(
            hashlib.sha256(_private_regular(config)).hexdigest() == checksum,
            "Staged artifact changed",
        )
        _verify_validation_inputs(folder, original_nginx, state)
        state["phase"] = "start_intent"
        _save(folder, state, scope=scope)
        host.run(["docker", "start", "-a", cid], timeout=90)
    except LabBindingError:
        binding_failed = True
        raise
    except BaseException as exc:  # noqa: BLE001 -- reconcile own fixture even on interruption.
        error = type(exc).__name__
    finally:
        if binding_failed:
            raise LabBindingError("Lab validation binding refused; fixture retained") from None
        if scope:
            scope.guard()
        current = host.inspect(cid)
        _fixture(current, state, lab_context=lab_context)
        if current["State"].get("Running"):
            if error is None:
                error = "UnexpectedRunningValidation"
            state["phase"] = "stop_intent"
            _save(folder, state, scope=scope)
            try:
                host.run(["docker", "stop", "--time", "5", cid], timeout=15)
            except LabBindingError:
                raise
            except Exception as exc:  # noqa: BLE001 -- a timeout does not establish daemon state.
                state["stop_error_class"] = type(exc).__name__
                _save(folder, state, scope=scope)
            current = host.inspect(cid)
            _fixture(current, state, lab_context=lab_context)
        _require(
            not current["State"].get("Running"),
            "Owned validation fixture remains running",
        )
        state.update(
            phase="stopped_retained",
            error_class=error,
            exit_code=current["State"].get("ExitCode"),
        )
        _save(folder, state, scope=scope)
        _verify_validation_inputs(folder, original_nginx, state)
    _require(
        error is None
        and current["State"].get("ExitCode") == 0
        and not current["State"].get("OOMKilled"),
        "Staged Nginx validation failed; fixture retained",
    )
    _require(
        _runtime(
            host, original_nginx, original_bridge, dedicated_network_id, lab_context=lab_context
        )
        == baseline,
        "Runtime changed during staged validation",
    )
    _require(
        hashlib.sha256(_private_regular(config)).hexdigest() == checksum,
        "Staged artifact changed during validation",
    )
    state["phase"] = "passed_stopped_retained"
    _save(folder, state, scope=scope)
    return cid
