"""Pre-cutover staged Nginx checks with one retained, exactly owned fixture.

Caller holds the release lock/capacity guard. Host.run captures diagnostics only
in the private release log and selects the local Docker socket. No production
container is started/stopped/reconfigured. The fixture uses the retained default
network only; it never joins the dedicated network or borrows a namespace.

This checks configuration parsing before original Grok is stopped. It does not
prove candidate reachability; post-cutover nginx -t/reload and real acceptance
must use the production Nginx container. All returned errors are sanitized.
"""

import functools
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path, PurePosixPath

DEFAULT = "docker_default"
DEDICATED = "docker_grok_security"
BRIDGE = "llm-proxy2-grok-bridge"
OWNER = "com.tmr.security.grok-nginx-validation"
LEDGER = "nginx-validation.private.json"
MEMORY = 128 * 1024 * 1024
TMPFS = {
    "/tmp": "rw,noexec,nosuid,size=16m",
    "/var/cache/nginx": "rw,noexec,nosuid,size=16m",
    "/var/run": "rw,noexec,nosuid,size=2m",
}
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TX = re.compile(r"[0-9a-f]{32}\Z")


class NginxValidationError(RuntimeError):
    """Private diagnostics belong in the caller's log, never this exception."""


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
    _require(folder.is_absolute() and not folder.is_symlink(), "Release directory requires review")
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


def _save(folder, state):
    """Atomic journal write under caller's lock; expected digest is never rebased.

    Failed pending files are retained privately. Compare again after the pending
    file is durable. The caller's lock serializes cooperating controllers; this
    is not a security boundary against a concurrent filesystem administrator.
    """
    path = folder / LEDGER
    expected = state.get("_ledger_sha256")
    _expected_ledger(path, expected)
    payload = {key: value for key, value in state.items() if key != "_ledger_sha256"}
    data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
    pending = folder / ("nginx-validation.pending-" + uuid.uuid4().hex + ".private.json")
    _write_pending(pending, data)
    _expected_ledger(path, expected)
    os.replace(pending, path)
    if os.name == "posix":
        fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    state["_ledger_sha256"] = hashlib.sha256(data).hexdigest()


def _load(folder):
    path = folder / LEDGER
    if not path.exists():
        _require(not path.is_symlink(), "Validation ledger requires review")
        return None
    try:
        raw = _private_regular(path)
        value = json.loads(raw)
        _require("_ledger_sha256" not in value, "Invalid private validation ledger")
        _require(
            value["schema"] == 1 and _TX.fullmatch(value["transaction"]),
            "Invalid validation ledger",
        )
        _require(
            value["name"] == "codex-grok-nginxcheck-" + value["transaction"], "Invalid fixture name"
        )
        _require(_hex(value["default_network_id"]), "Invalid validation network identity")
        _require(_hex(value["config_sha256"]), "Invalid staged configuration fingerprint")
        _require(value.get("id") is None or _hex(value["id"]), "Invalid fixture receipt")
        value["_ledger_sha256"] = hashlib.sha256(raw).hexdigest()
        return value
    except (KeyError, TypeError, ValueError):
        raise NginxValidationError("Invalid private validation ledger") from None


def _same_runtime(current, original, name, dedicated_id=None):
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
    _require(set(old) == {DEFAULT}, "Original network topology requires review")
    _require(
        DEFAULT in actual and _digest(actual[DEFAULT]) == _digest(old[DEFAULT]),
        "Original endpoint changed",
    )
    _require(_hex(old[DEFAULT].get("NetworkID")), "Original network identity is invalid")
    allowed = {DEFAULT}
    if dedicated_id is not None:
        _require(name == "nginx" and _hex(dedicated_id), "Dedicated validation binding is invalid")
        allowed.add(DEDICATED)
        if DEDICATED in actual:
            _require(
                actual[DEDICATED].get("NetworkID") == dedicated_id, "Dedicated endpoint differs"
            )
    _require(set(actual) <= allowed, "Unreviewed runtime network attachment")


def _runtime(host, original_nginx, original_bridge, dedicated_id):
    nginx, bridge = host.inspect("nginx"), host.inspect(BRIDGE)
    _same_runtime(nginx, original_nginx, "nginx", dedicated_id)
    _same_runtime(bridge, original_bridge, BRIDGE)
    _require(
        nginx["NetworkSettings"]["Networks"][DEFAULT]["NetworkID"]
        == bridge["NetworkSettings"]["Networks"][DEFAULT]["NetworkID"],
        "Original upstream is not on the retained default network",
    )
    return _digest([nginx["Id"], nginx["NetworkSettings"], bridge["Id"], bridge["NetworkSettings"]])


def _path(value):
    _require(
        type(value) is str and not any(char in value for char in ",\r\n\0"), "Unsafe mount path"
    )
    path = PurePosixPath(value)
    _require(
        path.is_absolute() and ".." not in path.parts and value != "/", "Unreviewed mount path"
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
        len(targets) == len(set(targets)) and len(targets) <= 32, "Ambiguous validation mounts"
    )
    _require(
        {"/etc/nginx/nginx.conf", "/var/log/nginx"} <= set(targets),
        "Required Nginx mounts are absent",
    )
    return sorted(result, key=lambda row: row["Destination"])


def _fixture(value, state):
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
    _require(config.get("Labels") == labels, "Validation ownership differs")
    _require(
        config.get("Image") == state["image"]
        and config.get("Hostname") == state["name"]
        and not config.get("Domainname"),
        "Validation image or hostname declaration differs",
    )
    for key in ("Env", "User", "WorkingDir", "ExposedPorts", "Healthcheck", "StopSignal"):
        _require(config.get(key) == defaults.get(key), "Validation image defaults changed")
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
        "SecurityOpt": ["no-new-privileges:true"],
        "IpcMode": "private",
        "CgroupnsMode": "private",
        "Runtime": "runc",
        "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
        "Tmpfs": TMPFS,
        "LogConfig": {"Type": "json-file", "Config": {"max-size": "1m", "max-file": "1"}},
    }
    _require(
        all(hc.get(key) == val for key, val in exact.items()), "Validation execution bounds changed"
    )
    for key in (
        "Privileged",
        "PublishAllPorts",
        "AutoRemove",
        "CapAdd",
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
    nets = value["NetworkSettings"]["Networks"]
    _require(set(nets) == {DEFAULT}, "Validation fixture must use only the default network")
    _require(
        nets[DEFAULT].get("NetworkID") in (state["default_network_id"], ""),
        "Validation network changed",
    )
    _require(
        not value["State"].get("Running")
        or nets[DEFAULT].get("NetworkID") == state["default_network_id"],
        "Running validation network is unresolved",
    )
    endpoint = nets[DEFAULT]
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
            _config(value) == state["fixture_sha256"],
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


def _create_args(state):
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
    for path, options in TMPFS.items():
        args.extend(["--tmpfs", path + ":" + options])
    for mount in state["mounts"]:
        argument = "type=bind,src=" + mount["Source"] + ",dst=" + mount["Destination"]
        args.extend(["--mount", argument + ("" if mount["RW"] else ",readonly")])
    return [*args, state["image"], "-t"]


@_sanitized
def retained_validation_ids(folder, inspected_container_list):
    """Exclude only the exact, unchanged, stopped fixture; never prefix-match."""
    state = _load(_folder(folder))
    if state is None or state.get("id") is None:
        return set()
    matches = [item for item in inspected_container_list if item.get("Id") == state["id"]]
    _require(len(matches) == 1, "Retained validation fixture is absent or ambiguous")
    _fixture(matches[0], state)
    _require(
        not matches[0]["State"].get("Running") and not matches[0]["State"].get("Restarting"),
        "Retained validation fixture is not stopped",
    )
    return {state["id"]}


@_sanitized
def validate_staged_nginx(
    host, config, original_nginx, original_bridge, *, dedicated_network_id=None
):
    """Run one retained fixture before cutover; return its exact owned ID.

    Host exposes folder, run(argv, timeout), inspect(name_or_id), image(image_id).
    Only the caller's private log may contain raw Docker/Nginx diagnostics.
    """
    folder = _folder(host.folder)
    config = Path(config)
    _require(config == folder / "nginx.after", "Use the reviewed staged Nginx artifact")
    source = _private_regular(config)
    checksum = hashlib.sha256(source).hexdigest()
    baseline = _runtime(host, original_nginx, original_bridge, dedicated_network_id)
    state = _load(folder)
    if state is None:
        logs = folder / "nginx-validation-logs"
        _require(not logs.exists(), "Validation scratch already exists without a ledger")
        image = host.image(original_nginx["Image"])
        _require(
            image.get("Id") == original_nginx["Image"] and not image["Config"].get("Volumes"),
            "Validation image has unreviewed volumes",
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
            "default_network_id": original_nginx["NetworkSettings"]["Networks"][DEFAULT][
                "NetworkID"
            ],
            "mounts": _mounts(original_nginx, config, logs),
            "id": None,
            "original_nginx_id": original_nginx["Id"],
            "original_bridge_id": original_bridge["Id"],
            "original_nginx_sha256": _config(original_nginx),
            "original_bridge_sha256": _config(original_bridge),
        }
        _save(folder, state)
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
    logs = folder / "nginx-validation-logs"
    if not logs.exists():
        _require(not logs.is_symlink(), "Validation scratch requires review")
        logs.mkdir(mode=0o700)
    _folder(logs)
    value = _lookup(host, state["name"])
    if value is None:
        _require(state.get("id") is None, "Retained validation fixture disappeared")
        try:
            result = host.run(_create_args(state), timeout=60).strip()
            _require(_hex(result), "Invalid creation receipt")
            state["id"] = result
            _save(folder, state)
        except Exception as exc:  # noqa: BLE001 -- reconcile effect instead of exposing diagnostics.
            # Persisted unique intent plus strict desired inspection can reconcile
            # a daemon-side create whose CLI response was lost.
            state["create_error_class"] = type(exc).__name__
            _save(folder, state)
        value = _lookup(host, state["name"])
        _require(value is not None, "Validation creation did not complete")
    cid = _fixture(value, state)
    state.update(id=cid, fixture_sha256=_config(value), phase="created_retained")
    _save(folder, state)
    error = None
    try:
        if value["State"].get("Running"):
            state["phase"] = "stop_interrupted_validation_intent"
            _save(folder, state)
            host.run(["docker", "stop", "--time", "5", cid], timeout=15)
            value = host.inspect(cid)
            _fixture(value, state)
            _require(not value["State"].get("Running"), "Interrupted validation remains running")
        _require(
            _runtime(host, original_nginx, original_bridge, dedicated_network_id) == baseline,
            "Runtime changed before staged validation",
        )
        _require(
            hashlib.sha256(_private_regular(config)).hexdigest() == checksum,
            "Staged artifact changed",
        )
        state["phase"] = "start_intent"
        _save(folder, state)
        host.run(["docker", "start", "-a", cid], timeout=90)
    except BaseException as exc:  # noqa: BLE001 -- reconcile own fixture even on interruption.
        error = type(exc).__name__
    finally:
        current = host.inspect(cid)
        _fixture(current, state)
        if current["State"].get("Running"):
            if error is None:
                error = "UnexpectedRunningValidation"
            state["phase"] = "stop_intent"
            _save(folder, state)
            try:
                host.run(["docker", "stop", "--time", "5", cid], timeout=15)
            except Exception as exc:  # noqa: BLE001 -- a timeout does not establish daemon state.
                state["stop_error_class"] = type(exc).__name__
                _save(folder, state)
            current = host.inspect(cid)
            _fixture(current, state)
        _require(not current["State"].get("Running"), "Owned validation fixture remains running")
        state.update(
            phase="stopped_retained", error_class=error, exit_code=current["State"].get("ExitCode")
        )
        _save(folder, state)
    _require(
        error is None
        and current["State"].get("ExitCode") == 0
        and not current["State"].get("OOMKilled"),
        "Staged Nginx validation failed; fixture retained",
    )
    _require(
        _runtime(host, original_nginx, original_bridge, dedicated_network_id) == baseline,
        "Runtime changed during staged validation",
    )
    _require(
        hashlib.sha256(_private_regular(config)).hexdigest() == checksum,
        "Staged artifact changed during validation",
    )
    state["phase"] = "passed_stopped_retained"
    _save(folder, state)
    return cid
