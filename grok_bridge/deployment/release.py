"""Bounded Grok-only release operator. Never run without the reviewed runbook.

Linux/root only at execution time. Importing this module has no side effects.
prepare stages private configuration; deploy takes a NEW quiesced snapshot;
rollback retains both data generations. No pull, build, volume removal or down.
"""

import argparse
import contextlib
import copy
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import time
import uuid
from pathlib import Path, PurePosixPath

import network_compose
import network_docker
import network_nginx
import network_policy
import network_transaction
import yaml

SERVICE = "llm-proxy2-grok-bridge"
ORIGINAL = "sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e"
CANDIDATE = "sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9"
ROOT = Path("/home/dblagbro/docker")
BACKUPS = Path("/mnt/s/backups/codex-private-security-20261109/grok-releases")
LEGACY_NAS_BACKUPS = Path("/mnt/s/backups/codex-security-20261109/grok-releases")
LEGACY_BACKUPS = Path("/var/backups/codex-grok-releases")
MARKER = "# ── from projects-locations.d/grok-bridge.conf ──"
ENV_ADDITIONS = {
    "BRIDGE_PUBLIC_PATH": "/grok-bridge",
    "BRIDGE_PUBLIC_ORIGIN": "https://www.voipguru.org",
    "BRIDGE_AUTH_URL": "http://llm-proxy2:3000/api/auth/me",
}
GIB = 1024**3
NETWORK_RELEASE_SCHEMA = 1


class Refused(RuntimeError):
    """Fail closed; messages must not include configuration or command output."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def private_write(path, data):
    """New/private file only; never follows a symlink or overwrites a file."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def cas_write(path, expected, replacement):
    """Exact-byte CAS preserving inode, ownership and file-bind mount visibility.

    The host maintenance lock serializes our operators; flock is advisory for
    other editors. Every file is rechecked immediately before writing. Never
    overwrite a detected concurrent edit. Nginx is reloaded only after nginx -t.
    """
    import fcntl

    fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "r+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(
            stat.S_ISREG(os.fstat(stream.fileno()).st_mode),
            "Configuration is not regular",
        )
        require(stream.read() == expected, "Configuration CAS conflict")
        stream.seek(0)
        stream.write(replacement)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())
        stream.seek(0)
        require(stream.read() == replacement, "Configuration write verification failed")


def service_span(text):
    """Only the canonical two-space block style is supported; ambiguity refuses."""
    matches = list(re.finditer(r"(?m)^  " + re.escape(SERVICE) + r":\s*\n", text))
    require(len(matches) == 1, "Ambiguous Grok service block")
    start = matches[0].start()
    tail = re.search(r"(?m)^(?:  [A-Za-z0-9_.-]+:|[A-Za-z0-9_.-]+:)", text[matches[0].end() :])
    end = matches[0].end() + tail.start() if tail else len(text)
    return start, end


def environment_mapping(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    require(isinstance(value, list), "Unsupported Compose environment")
    require(
        all(isinstance(x, str) and "=" in x for x in value),
        "Unresolved environment entry",
    )
    result = dict(x.split("=", 1) for x in value)
    require(len(result) == len(value), "Duplicate environment key")
    return result


def _compose_parameters(data_path, lab_context):
    """Pure context consistency, never observed isolation or execution authority."""
    network_policy._identity(lab_context)
    if lab_context is None:
        from network_lab_context import ROOT as LAB_ROOT

        if isinstance(data_path, (str, PurePosixPath, Path)):
            path = PurePosixPath(data_path)
            require(
                path != LAB_ROOT and LAB_ROOT not in path.parents,
                "Lab candidate path requires its exact context",
            )
        return CANDIDATE, ENV_ADDITIONS
    require(
        type(data_path) is str and data_path == str(lab_context.generation_root / "candidate-data"),
        "Use the exact lab candidate-data path",
    )
    return dict(lab_context.images)["candidate"], {
        **ENV_ADDITIONS,
        "BRIDGE_PUBLIC_ORIGIN": "https://grok-lab.invalid",
    }


def _lab_compose_identity(model, lab_context):
    if lab_context is None:
        if type(model) is dict:
            require(
                not str(model.get("name", "")).startswith("codex-grok-nettest-"),
                "Lab project requires its exact context",
            )
        return
    names = network_policy._identity(lab_context)
    require(type(model) is dict and model.get("name") == lab_context.project, "Lab project differs")
    services = model.get("services")
    require(type(services) is dict, "Missing lab service mapping")
    for role in network_policy.MEMBERS:
        require(
            type(services.get(role)) is dict
            and services[role].get("container_name") == names[role],
            "Lab physical service identity differs",
        )
    pins = dict(lab_context.images)
    require(
        services[SERVICE].get("image") == pins["original"]
        and services["nginx"].get("image") == pins["nginx"],
        "Original lab image pins differ",
    )
    _lab_data_source(services[SERVICE])


def _lab_data_source(service):
    """Concrete named-volume syntax only; this does not establish volume ownership."""
    mounts = service.get("volumes")
    require(type(mounts) is list, "Missing original lab data volume")
    sources = []
    for mount in mounts:
        if type(mount) is str:
            fields = mount.split(":")
            if len(fields) > 1 and fields[1] == "/data":
                require(
                    len(fields) == 2 or (len(fields) == 3 and fields[2] == "rw"),
                    "Invalid original lab data options",
                )
                sources.append(fields[0])
        elif type(mount) is dict and mount.get("target") == "/data":
            require(
                mount.get("type", "volume") == "volume", "Original lab data must be a named volume"
            )
            sources.append(mount.get("source"))
    require(
        len(sources) == 1
        and type(sources[0]) is str
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", sources[0])),
        "Original lab data requires a concrete named-volume token",
    )
    return sources[0]


def _lab_compose_source(content, lab_context):
    require(
        type(content) is bytes and 0 < len(content) <= 8 * 1024 * 1024,
        "Invalid lab source size or type",
    )
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise Refused("Lab source must be UTF-8") from None
    require("\r" not in text and not text.startswith("\ufeff"), "Invalid lab source encoding")
    before, _ = network_compose._parse(text)
    _lab_compose_identity(before, lab_context)
    return text, before


def candidate_service(before, data_path, *, lab_context=None):
    candidate, additions = _compose_parameters(data_path, lab_context)
    if lab_context is not None:
        require(
            type(before) is dict
            and before.get("image") == dict(lab_context.images)["original"]
            and before.get("container_name") == lab_context.resource_map()[SERVICE],
            "Original lab service identity or image differs",
        )
        _lab_data_source(before)
    else:
        require(
            not str(before.get("container_name", "")).startswith("codex-grok-nettest-"),
            "Lab service requires its exact context",
        )
    require(PurePosixPath(data_path).is_absolute(), "Candidate data path must be absolute")
    result = copy.deepcopy(before)
    result["image"] = candidate
    env = environment_mapping(result.get("environment", {}))
    require(bool(env.get("BRIDGE_TOKEN")), "Missing existing service token")
    require(
        env.get("BRIDGE_PUBLIC_PATH") == "/grok-bridge",
        "Unexpected existing public path",
    )
    env.update(additions)
    result["environment"] = env
    mounts = result.get("volumes", [])
    found = []
    for index, item in enumerate(mounts):
        if isinstance(item, str):
            parts = item.split(":")
            if len(parts) > 1 and parts[1] == "/data":
                require(len(parts) == 2 or parts[2] == "rw", "Unexpected data mount options")
                found.append(index)
        elif isinstance(item, dict) and item.get("target") == "/data":
            require(
                item.get("type", "volume") == "volume" and not item.get("read_only"),
                "Original data must be a writable named volume",
            )
            require(
                set(item) <= {"type", "source", "target", "read_only", "volume"},
                "Unreviewed data mount options",
            )
            require(not item.get("volume"), "Unreviewed named-volume options")
            found.append(index)
    require(len(found) == 1, "Expected exactly one original /data volume")
    mounts[found[0]] = {
        "type": "bind",
        "source": data_path,
        "target": "/data",
        "bind": {"create_host_path": False},
    }
    return result


def render_compose(content, data_path, *, lab_context=None):
    _compose_parameters(data_path, lab_context)
    if lab_context is None:
        text = content.decode("utf-8")
        require("\r" not in text, "Unexpected Compose line endings")
        before = yaml.safe_load(text)
        _lab_compose_identity(before, None)
    else:
        text, before = _lab_compose_source(content, lab_context)
    require(
        isinstance(before, dict) and SERVICE in before.get("services", {}),
        "Missing service",
    )
    start, end = service_span(text)
    service = candidate_service(before["services"][SERVICE], data_path, lab_context=lab_context)
    rendered = yaml.safe_dump({SERVICE: service}, sort_keys=False, allow_unicode=True)
    block = "".join("  " + line + "\n" for line in rendered.splitlines())
    output = (text[:start] + block + text[end:]).encode()
    expected = copy.deepcopy(before)
    expected["services"][SERVICE] = service
    require(
        network_policy._same_json(yaml.safe_load(output), expected), "Non-Grok Compose modification"
    )
    return output


def assert_resolved_scope(before, after, data_path, *, lab_context=None):
    _compose_parameters(data_path, lab_context)
    _lab_compose_identity(before, lab_context)
    expected = copy.deepcopy(before)
    expected["services"][SERVICE] = candidate_service(
        before["services"][SERVICE], data_path, lab_context=lab_context
    )
    # Compose adds the default read_only:false on bind mounts in some versions.
    actual = copy.deepcopy(after)

    # Compose omits a now-unreferenced volume from resolved output. Its raw
    # canonical declaration remains preserved by render_compose; no volume is removed.
    def volume_source(mount):
        if isinstance(mount, dict):
            return mount.get("source") if mount.get("type", "volume") == "volume" else None
        return mount.split(":", 1)[0] if isinstance(mount, str) else None

    old_data = next(
        m
        for m in before["services"][SERVICE]["volumes"]
        if (m.get("target") if isinstance(m, dict) else m.split(":")[1]) == "/data"
    )
    old_source = volume_source(old_data)
    referenced = {
        volume_source(m)
        for service in actual["services"].values()
        for m in service.get("volumes", [])
    }
    if old_source not in actual.get("volumes", {}) and old_source not in referenced:
        expected.get("volumes", {}).pop(old_source, None)
        if not expected.get("volumes") and "volumes" not in actual:
            expected.pop("volumes", None)
    for config in (expected, actual):
        for mount in config["services"][SERVICE]["volumes"]:
            if (
                isinstance(mount, dict)
                and mount.get("target") == "/data"
                and mount.get("read_only") is False
            ):
                mount.pop("read_only")
    require(
        network_policy._same_json(actual, expected),
        "Resolved Compose scope changed outside reviewed Grok fields",
    )


def render_network_compose(content, resolved, data_path, *, lab_context=None, review_receipt=None):
    """Pure rendering; a lab context/receipt establishes no provenance or admission."""
    _compose_parameters(data_path, lab_context)
    _lab_compose_identity(resolved, lab_context)
    planned, expected_receipt = network_policy.plan_networks(resolved, lab_context=lab_context)
    network_policy._review_binding(review_receipt, expected_receipt, lab_context=lab_context)
    if lab_context is not None:
        _, raw_before = _lab_compose_source(content, lab_context)
        require(
            _lab_data_source(raw_before["services"][SERVICE])
            == _lab_data_source(resolved["services"][SERVICE]),
            "Raw and resolved original lab data sources differ",
        )
    specification = {
        "original_networks": {
            role: resolved["services"][role]["networks"] for role in network_policy.MEMBERS
        },
        "desired_networks": {
            role: planned["services"][role]["networks"] for role in network_policy.MEMBERS
        },
        "dedicated_definition": planned["networks"][network_policy.NETWORK],
        "lab_context": lab_context,
        "review_receipt": review_receipt,
    }
    # Reject ambiguous original YAML before the older service renderer could
    # collapse duplicate keys inside the Grok block.
    network_compose.render_networks(content, **specification)
    return network_compose.render_networks(
        render_compose(content, data_path, lab_context=lab_context), **specification
    )


def assert_network_resolved_scope(
    before, after, data_path, *, lab_context=None, review_receipt=None
):
    _compose_parameters(data_path, lab_context)
    _lab_compose_identity(before, lab_context)
    planned, expected_receipt = network_policy.plan_networks(before, lab_context=lab_context)
    network_policy._review_binding(review_receipt, expected_receipt, lab_context=lab_context)
    actual = copy.deepcopy(after)
    require(
        isinstance(actual.get("networks"), dict) and network_policy.NETWORK in actual["networks"],
        "Dedicated network declaration is absent",
    )
    definition = actual["networks"].pop(network_policy.NETWORK)
    require(type(definition) is dict, "Invalid dedicated network declaration")
    if "ipam" in definition:
        require(
            type(definition["ipam"]) is dict and definition["ipam"] == {},
            "Dedicated IPAM requires review",
        )
        definition.pop("ipam")
    definition.setdefault("internal", False)
    require(
        network_policy._same_json(definition, planned["networks"][network_policy.NETWORK]),
        "Dedicated network declaration differs",
    )
    for role in network_policy.MEMBERS:
        require(
            network_policy._same_json(
                actual.get("services", {}).get(role, {}).get("networks"),
                planned["services"][role]["networks"],
            ),
            "Resolved service network differs from reviewed policy",
        )
        actual["services"][role]["networks"] = before["services"][role]["networks"]
    assert_resolved_scope(before, actual, data_path, lab_context=lab_context)


def render_nginx(content, fragment):
    text, body = content.decode(), fragment.decode()
    require(text.count(MARKER) == 1, "Missing or duplicate Grok Nginx marker")
    require(
        ":6080" not in body and ":8443/vnc/websockify" in body,
        "Unexpected browser proxy destination",
    )
    start = text.rfind("\n", 0, text.index(MARKER)) + 1
    next_marker = text.find("# ── from projects-locations.d/", text.index(MARKER) + len(MARKER))
    require(next_marker >= 0, "No following Nginx marker; boundary requires review")
    end = text.rfind("\n", 0, next_marker) + 1
    indent = text[start : text.index(MARKER)]
    require(not indent.strip(), "Unexpected marker prefix")
    block = (
        indent
        + MARKER
        + "\n"
        + "".join(indent + "    " + x + "\n" for x in body.splitlines())
        + "\n"
    )
    return (text[:start] + block + text[end:]).encode()


def archive_manifest(archive):
    """Reject traversal, devices and link-parent tricks before GNU tar extraction."""
    rows, seen, links = [], set(), set()
    with tarfile.open(archive) as tar:
        members = tar.getmembers()
        require(len(members) <= 250000, "Snapshot entry count exceeds reviewed bound")
        for item in members:
            p = PurePosixPath(item.name)
            require(not p.is_absolute() and ".." not in p.parts, "Unsafe archive member")
            name = str(p)
            require(name not in seen, "Duplicate archive member")
            seen.add(name)
            require(
                item.isfile() or item.isdir() or item.issym() or item.islnk(),
                "Unsupported archive type",
            )
            if item.issym() or item.islnk():
                links.add(name)
        for item in members:
            p = PurePosixPath(item.name)
            require(
                not any(str(x) in links for x in p.parents),
                "Archive member under a link",
            )
            row = {
                "name": str(p),
                "mode": item.mode,
                "uid": item.uid,
                "gid": item.gid,
                "type": item.type.decode("ascii"),
                "size": item.size,
                "link": item.linkname,
            }
            if item.issym() or item.islnk():
                target = p.parent / item.linkname if item.issym() else PurePosixPath(item.linkname)
                require(
                    not target.is_absolute() and ".." not in target.parts,
                    "Unsafe archive link",
                )
                require(
                    not any(str(x) in links for x in (target, *target.parents)),
                    "Chained archive link",
                )
                require(str(target) in seen, "Dangling archive link")
            if item.isfile():
                h = hashlib.sha256()
                with tar.extractfile(item) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        h.update(chunk)
                row["sha256"] = h.hexdigest()
            rows.append(row)
    return sorted(rows, key=lambda x: x["name"])


def verify_restore(directory, manifest):
    root = Path(directory).resolve()
    for row in manifest:
        target = root / row["name"]
        require(target.resolve().is_relative_to(root), "Restored link leaves candidate data")
        info = target.lstat()
        if "sha256" in row:
            require(
                target.is_file() and file_digest(target) == row["sha256"],
                "Restored file differs",
            )
        elif row["type"] == "5":
            require(target.is_dir(), "Restored directory missing")
        elif row["type"] == "2":
            require(
                target.is_symlink() and os.readlink(target) == row["link"],
                "Restored symlink differs",
            )
        elif row["type"] == "1":
            require(
                os.path.samefile(target, root / row["link"]),
                "Restored hardlink differs",
            )
        if not target.is_symlink():
            require(stat.S_IMODE(info.st_mode) == row["mode"], "Restored file mode differs")
        if os.name == "posix":
            require(
                info.st_uid == row["uid"] and info.st_gid == row["gid"],
                "Restored ownership differs",
            )


def assert_separate_volume(original, candidate):
    old, new = Path(original).resolve(), Path(candidate).resolve()
    require(
        old != new and not old.is_relative_to(new) and not new.is_relative_to(old),
        "Original and candidate data overlap",
    )


def capacity(path, peak=0):
    usage = shutil.disk_usage(path)
    reserve = max(64 * GIB, math.ceil(usage.total * 0.15) + 2 * GIB)
    require(usage.free >= reserve + peak, "Insufficient capacity reserve")


@contextlib.contextmanager
def operation_lock():
    import fcntl

    require(
        os.geteuid() == 0 and socket.gethostname().split(".")[0] == "tmrwww01",
        "Operator requires root on tmrwww01",
    )
    parent = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().split(b"\0")
    require(
        any(Path(os.fsdecode(x)).name == "capacity_guard.py" for x in parent if x),
        "Run directly under the shared capacity guard",
    )
    fd = os.open("/run/codex-grok-release.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        capacity(ROOT)
        yield


class Host:
    def __init__(self, folder):
        self.folder = Path(folder)

    def run(self, args, timeout=180):
        if args and args[0] == "docker":
            args = [network_docker.DOCKER, "--host", network_docker.DOCKER_SOCKET, *args[1:]]
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Host command exceeded deadline; reconcile actual state") from exc
        with (self.folder / "commands.private.log").open("ab") as stream:
            stream.write(result.stdout + result.stderr)
        require(result.returncode == 0, "Host command failed; inspect private log")
        return result.stdout.decode()

    def network_adapter(self, state=None, *, mutate=False):
        """The sanitized adapter captures inspection without private command logs."""
        if state is None:
            require(not mutate, "Initial network inspection must be read-only")
            allowed = {role: self.inspect(role)["Id"] for role in network_policy.CONSUMERS}
            return network_docker.DockerHost(allowed_consumers=allowed, compose_project="docker")
        require(state.get("project") == "docker", "Network project requires review")
        journal = state["network_transaction"]
        return network_docker.DockerHost(
            enable_mutations=mutate,
            transaction_id=journal["transaction_id"],
            allowed_consumers={role: item["id"] for role, item in journal["consumers"].items()},
            allowed_bridge_id=journal["bridge_id"],
            compose_project="docker",
        )

    def inspect(self, name):
        return json.loads(self.run(["docker", "inspect", name]))[0]

    def optional_service(self):
        # A successful daemon-backed listing distinguishes absence from a daemon
        # failure. Never convert an arbitrary inspect/transport error into None.
        ids = self.run(
            [
                "docker",
                "container",
                "ls",
                "-a",
                "--no-trunc",
                "--filter",
                "name=^/" + SERVICE + "$",
                "--format",
                "{{.ID}}",
            ]
        ).split()
        require(len(ids) <= 1, "Ambiguous service identity")
        return self.inspect(ids[0]) if ids else None

    def image(self, image):
        value = json.loads(self.run(["docker", "image", "inspect", image]))[0]
        require(value["Id"] == image, "Immutable local image mismatch")
        return value

    def compose(self, path, project, *command):
        return self.run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(ROOT),
                "-p",
                project,
                "-f",
                str(path),
                *command,
            ]
        )

    def others(self):
        ids = self.run(["docker", "ps", "-aq"]).split()
        values = json.loads(self.run(["docker", "inspect", *ids])) if ids else []
        owned = network_nginx.retained_validation_ids(self.folder, values)
        return {
            x["Name"]: [x["Id"], x["State"]["Status"]]
            for x in values
            if x["Name"] != "/" + SERVICE and x["Id"] not in owned
        }

    def exclusive_volume(self, source, owner):
        ids = self.run(["docker", "ps", "-aq"]).split()
        values = json.loads(self.run(["docker", "inspect", *ids])) if ids else []
        consumers = [x["Id"] for x in values if any(m["Source"] == source for m in x["Mounts"])]
        require(consumers == [owner], "Original volume has another container consumer")

    def healthy(self, image, exact_id=None):
        until = time.monotonic() + 180
        while time.monotonic() < until:
            item = self.inspect(SERVICE)
            require(item["Image"] == image, "Unexpected live image")
            if exact_id:
                require(item["Id"] == exact_id, "Container identity changed")
            if (
                item["State"]["Running"]
                and item["State"].get("Health", {}).get("Status") == "healthy"
            ):
                return item
            time.sleep(2)
        raise Refused("Health deadline exceeded")

    def stop(self, cid, allow_unclean=False):
        # A timed-out CLI does not imply that the daemon rejected the stop.
        try:
            self.run(["docker", "stop", "--time", "120", cid], timeout=180)
        except (TimeoutError, Refused):
            pass
        until = time.monotonic() + 180
        while time.monotonic() < until:
            item = self.inspect(cid)
            require(item["Id"] == cid, "Stop identity mismatch")
            if not item["State"]["Running"]:
                require(
                    allow_unclean
                    or (item["State"]["ExitCode"] == 0 and not item["State"]["OOMKilled"]),
                    "Container did not stop cleanly",
                )
                return
            time.sleep(2)
        raise Refused("Stop remains ambiguous; recovery must reconcile the original")

    def start_original(self, cid):
        item = self.inspect(SERVICE)
        require(
            item["Id"] == cid and item["Image"] == ORIGINAL,
            "Original runtime identity changed",
        )
        if not item["State"]["Running"]:
            self.run(["docker", "start", cid])
        self.healthy(ORIGINAL, cid)

    def validate_nginx(self, config, original_nginx, original_bridge):
        return network_nginx.validate_staged_nginx(self, config, original_nginx, original_bridge)

    def reload_nginx(self, original_nginx):
        require(
            self.inspect("nginx")["Id"] == original_nginx["Id"],
            "Nginx identity changed",
        )
        self.run(["docker", "exec", original_nginx["Id"], "nginx", "-t"])
        self.run(["docker", "exec", original_nginx["Id"], "nginx", "-s", "reload"])


LAB_JOURNAL = "state.lab.private.json"
LAB_JOURNAL_MAX_BYTES = 16 * 1024 * 1024
LAB_RELEASE_SCHEMA = 1


def _journal_errors(function):
    import functools

    @functools.wraps(function)
    def operation(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Refused:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            network_policy.NetworkPolicyError,
            network_transaction.TransactionError,
            network_nginx.NginxValidationError,
        ):
            if kwargs.get("lab_context") is not None or kwargs.get("lab_private") is True:
                raise Refused(
                    "Lab private journal operation refused; retain private evidence"
                ) from None
            raise

    return operation


def _journal_binding(context, binding):
    network_policy._identity(context)
    if context is None:
        require(binding is None, "Lab journal binding requires its exact context")
        return
    try:
        context.verify_journal_binding(binding)
    except ValueError:
        raise Refused("Lab release journal binding differs") from None


def _journal_production(state):
    require(type(state) is dict, "Invalid private journal")
    require(
        not {"release_schema", "mode", "lab_context"} & set(state),
        "Lab journal cannot be used in production",
    )
    nested = state.get("network_transaction")
    if type(nested) is dict:
        require(
            nested.get("schema") != 2 and not {"mode", "lab_context"} & set(nested),
            "Lab network journal cannot be used in production",
        )
    require(
        not str(state.get("project", "")).startswith("codex-grok-nettest-"),
        "Lab project requires its exact context",
    )
    from network_lab_context import ROOT as LAB_ROOT

    paths = [state.get(key) for key in ("original_data", "candidate_data")]
    for key in ("files", "inputs"):
        values = state.get(key)
        if type(values) is dict:
            paths.extend(values.values() if key == "files" else values)
    for value in paths:
        if type(value) is str:
            path = PurePosixPath(value)
            require(
                path != LAB_ROOT and LAB_ROOT not in path.parents,
                "Lab journal paths require their exact context",
            )


def _journal_generation_path(value, context, *, configuration=False):
    _runtime_lab_path(value)
    root = context.configuration_root if configuration else context.generation_root
    require(root in PurePosixPath(value).parents, "Lab journal path is outside its generation")


def _journal_lab_envelope(state, context, binding):
    """Validate retained records, not observations, authority or recovery success."""
    _journal_binding(context, binding)
    network_policy._json_value(state)
    require(type(state) is dict, "Invalid lab release journal")
    required = {
        "release_schema",
        "mode",
        "lab_context",
        "phase",
        "project",
        "original_id",
        "original_image",
        "candidate_image",
        "original_data",
        "candidate_data",
        "files",
        "inputs",
        "artifacts",
        "others",
        "stop_intent",
        "cutover_intent",
        "deployed",
        "network_contract",
        "network_transaction",
        "network_intent",
        "original_network_projection",
    }
    optional = {
        "updated_utc",
        "_journal_sha256",
        "candidate_id",
        "rollback_id",
        "snapshot",
        "managed_environment",
        "configuration_conflicts",
        "error_class",
        "soak_seconds",
        "provider_acceptance",
        "public_browser_acceptance",
    }
    require(required <= set(state) <= required | optional, "Invalid lab release journal fields")
    require(
        type(state["release_schema"]) is int
        and state["release_schema"] == LAB_RELEASE_SCHEMA
        and state["mode"] == "lab",
        "Invalid lab release journal schema or mode",
    )
    _journal_binding(context, state["lab_context"])
    require(
        network_policy._same_json(state["lab_context"], binding),
        "Lab release journal binding changed",
    )
    require(state["project"] == context.project, "Lab release project differs")
    pins = dict(context.images)
    require(
        state["original_image"] == pins["original"]
        and state["candidate_image"] == pins["candidate"],
        "Lab release image pins differ",
    )
    for key in ("original_id", "candidate_id", "rollback_id", "_journal_sha256"):
        if key in state:
            require(network_transaction._hex(state[key]), "Invalid lab release identity or receipt")
    candidate, rollback = state.get("candidate_id"), state.get("rollback_id")
    require(
        candidate is None or candidate not in {state["original_id"], rollback},
        "Contradictory lab container identities",
    )
    for key in ("network_intent", "stop_intent", "cutover_intent", "deployed"):
        require(type(state[key]) is bool, "Invalid lab release intent flag")
    require(
        not state["stop_intent"] or state["network_intent"], "Stop intent has no network intent"
    )
    require(
        not state["cutover_intent"] or state["stop_intent"], "Cutover intent has no stop intent"
    )
    require(
        candidate is None or state["cutover_intent"], "Candidate identity precedes cutover intent"
    )
    require(rollback is None or state["stop_intent"], "Rollback identity precedes stop intent")
    require(
        not state["deployed"] or (state["cutover_intent"] and candidate is not None),
        "Deployed journal lacks candidate intent",
    )
    phases = {
        "prepared_not_deployed",
        "attaching_consumers",
        "quiescing_original",
        "cutover_intent",
        "deployed_local_checks_passed",
        "deployment_failed",
        "rollback_intent",
        "network_only_rolled_back",
        "original_runtime_recovered",
        "rolled_back",
        "original_runtime_recovered_config_conflict",
        "pre_cutover_failed_original_restarted",
        "pre_stop_failed_network_recovered",
    }
    require(type(state["phase"]) is str and state["phase"] in phases, "Unknown lab release phase")
    if state["phase"] == "prepared_not_deployed":
        require(
            not any(
                state[key]
                for key in ("network_intent", "stop_intent", "cutover_intent", "deployed")
            )
            and candidate is None
            and rollback is None,
            "Prepared lab journal contains lifecycle effects",
        )
    _runtime_lab_path(state["original_data"])
    _compose_parameters(state["candidate_data"], context)
    require(state["original_data"] != state["candidate_data"], "Lab data paths collide")
    files = state["files"]
    require(
        type(files) is dict and set(files) == {"compose", "fragment", "nginx"},
        "Invalid lab configuration paths",
    )
    for value in files.values():
        _journal_generation_path(value, context, configuration=True)
    require(len(set(files.values())) == len(files), "Lab configuration paths collide")
    require(
        type(state["inputs"]) is dict and type(state["artifacts"]) is dict,
        "Invalid lab input fingerprints",
    )
    for path, checksum in state["inputs"].items():
        _journal_generation_path(path, context)
        require(
            checksum is None or network_transaction._hex(checksum), "Invalid lab input fingerprint"
        )
    for name, checksum in state["artifacts"].items():
        require(
            type(name) is str
            and name not in {".", ".."}
            and bool(re.fullmatch(r"[A-Za-z0-9_.-]+", name))
            and network_transaction._hex(checksum),
            "Invalid lab artifact fingerprint",
        )
    require(
        type(state["network_contract"]) is int
        and state["network_contract"] == NETWORK_RELEASE_SCHEMA,
        "Invalid lab network contract",
    )
    network_transaction.NetworkTransaction._validate_state(
        state["network_transaction"], lab_context=context
    )
    projection = network_transaction._container(state["original_network_projection"], SERVICE)
    require(
        projection["id"] == state["original_id"]
        and set(projection["networks"]) == {network_policy.DEFAULT_NAME},
        "Lab original network binding differs",
    )
    nested = state["network_transaction"]
    if not state["network_intent"]:
        require(
            nested["phase"] == "prepared"
            and nested["network"]["phase"] == "planned"
            and nested["bridge_id"] is None
            and all(
                entry["phase"] == "pending" and not entry["owned"]
                for entry in nested["consumers"].values()
            ),
            "Lab network effects lack retained intent",
        )
    require(
        nested["bridge_id"] is None or nested["bridge_id"] == candidate,
        "Lab candidate network binding differs",
    )
    consumer_ids = {entry["id"] for entry in nested["consumers"].values()}
    require(
        not consumer_ids & {state["original_id"], candidate, rollback},
        "Lab consumer and bridge identities collide",
    )
    default_id = projection["networks"][network_policy.DEFAULT_NAME]["id"]
    require(
        all(entry["default_network_id"] == default_id for entry in nested["consumers"].values()),
        "Lab default network identities differ",
    )
    require(
        nested["network"]["id"] != default_id,
        "Lab dedicated and default network identities collide",
    )
    if state["cutover_intent"]:
        require(
            nested["network"]["phase"] == "owned", "Lab cutover lacks retained network ownership"
        )


def _journal_lab_progress(previous, working):
    """Keep retained preparation evidence while allowing lifecycle progress."""
    fixed = {
        "release_schema",
        "mode",
        "lab_context",
        "project",
        "original_id",
        "original_image",
        "candidate_image",
        "original_data",
        "candidate_data",
        "files",
        "inputs",
        "artifacts",
        "others",
        "managed_environment",
        "original_network_projection",
        "network_contract",
    }
    for key in fixed | {"candidate_id", "snapshot"}:
        if key in fixed or key in previous:
            require(
                (key in previous) == (key in working)
                and network_policy._same_json(previous.get(key), working.get(key)),
                "Lab retained preparation or receipt changed",
            )
    old_network, new_network = previous["network_transaction"], working["network_transaction"]
    require(
        old_network["transaction_id"] == new_network["transaction_id"],
        "Lab transaction identity changed",
    )
    if old_network["network"]["id"] is not None:
        require(
            all(
                network_policy._same_json(old_network["network"][key], new_network["network"][key])
                for key in ("id", "proof")
            ),
            "Lab retained network ownership changed",
        )
    if old_network["bridge_id"] is not None:
        require(
            old_network["bridge_id"] == new_network["bridge_id"],
            "Lab retained bridge identity changed",
        )
    for role in old_network["consumers"]:
        before, after = old_network["consumers"][role], new_network["consumers"][role]
        for key in (
            "id",
            "config_sha256",
            "default_route_sha256",
            "default_network_id",
            "default_attachment_sha256",
        ):
            require(
                network_policy._same_json(before[key], after[key]),
                "Lab retained consumer baseline changed",
            )


def _journal_file_metadata(info):
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_nlink == 1
        and info.st_uid == 0
        and stat.S_IMODE(info.st_mode) == 0o600,
        "Lab journal must be a private single-link root-owned regular file",
    )


def _journal_metadata(info):
    return tuple(
        getattr(info, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def _journal_sync_directory(folder):
    if os.name == "posix":
        fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class _ReleaseJournalScope:
    """Cooperating-controller scope; caller still holds its lifecycle operation lock."""

    def __init__(self, folder, context, binding):
        _journal_binding(context, binding)
        self.context, self.binding = context, binding
        self.lab = context is not None
        if self.lab:
            self.context_digest = context.digest()
            self.logical = _runtime_lab_path(str(folder))
            require(
                self.logical == str(context.generation_root), "Use the exact lab release generation"
            )
            self.folder, self.fingerprint = network_nginx._lab_filesystem_path(
                PurePosixPath(self.logical), context.generation_root
            )
            require(self.folder.is_dir(), "Lab release generation must already exist")
        else:
            from network_lab_context import ROOT as LAB_ROOT

            path = PurePosixPath(str(folder))
            require(
                path != LAB_ROOT and LAB_ROOT not in path.parents,
                "Lab release generation requires its exact context",
            )
            self.folder = Path(folder)
        self.path = self.folder / (LAB_JOURNAL if self.lab else "state.private.json")
        self.check()

    def check(self):
        _journal_binding(self.context, self.binding)
        if self.lab:
            require(self.context.digest() == self.context_digest, "Lab release context changed")
            current, fingerprint = network_nginx._lab_filesystem_path(
                PurePosixPath(self.logical), self.context.generation_root
            )
            require(
                current == self.folder and fingerprint == self.fingerprint,
                "Lab release path identity changed",
            )
        require(not self.path.is_symlink(), "Private journal must be regular")
        opposite = self.folder / ("state.private.json" if self.lab else LAB_JOURNAL)
        require(
            not opposite.exists() and not opposite.is_symlink(),
            "Opposite-mode release journal requires review",
        )

    def read(self, path=None):
        self.check()
        value = read_journal(
            self.path if path is None else path,
            max_bytes=LAB_JOURNAL_MAX_BYTES if self.lab else None,
            lab_private=self.lab,
        )
        self.check()
        return value


def _journal_parse(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def nonfinite(value):
        raise ValueError

    try:
        state = json.loads(data, object_pairs_hook=unique, parse_constant=nonfinite)
    except (ValueError, UnicodeError, RecursionError):
        raise Refused("Invalid private journal JSON") from None
    require(type(state) is dict and "_journal_sha256" not in state, "Invalid private journal")
    return state


@_journal_errors
def read_journal(path, *, max_bytes=None, lab_private=False):
    require(type(lab_private) is bool, "Invalid journal read mode")
    if lab_private and max_bytes is None:
        max_bytes = LAB_JOURNAL_MAX_BYTES
    require(
        max_bytes is None or (type(max_bytes) is int and 0 < max_bytes <= LAB_JOURNAL_MAX_BYTES),
        "Invalid journal read bound",
    )
    require(not path.is_symlink(), "Private journal must be regular")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        if lab_private:
            flags |= getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except OSError:
        raise Refused("Cannot safely read private journal") from None
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        require(stat.S_ISREG(before.st_mode), "Private journal must be regular")
        if lab_private:
            _journal_file_metadata(before)
        require(
            max_bytes is None or before.st_size <= max_bytes, "Private journal exceeds read bound"
        )
        data = stream.read() if max_bytes is None else stream.read(max_bytes + 1)
        require(max_bytes is None or len(data) <= max_bytes, "Private journal exceeds read bound")
        if lab_private:
            after = os.fstat(stream.fileno())
            _journal_file_metadata(after)
            require(
                _journal_metadata(before) == _journal_metadata(after)
                and len(data) == after.st_size,
                "Lab journal changed during read",
            )
            require(
                _journal_metadata(after) == _journal_metadata(path.lstat()),
                "Lab journal path changed during read",
            )
        return data


@_journal_errors
def save(folder, state, *, lab_context=None, lab_binding=None):
    _journal_binding(lab_context, lab_binding)
    if lab_context is None:
        _journal_production(state)
    else:
        _journal_lab_envelope(state, lab_context, lab_binding)
    scope = _ReleaseJournalScope(folder, lab_context, lab_binding)
    retained = copy.deepcopy(state) if scope.lab else None
    working = copy.deepcopy(state) if scope.lab else state
    working["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = scope.path
    serialized = {key: value for key, value in working.items() if key != "_journal_sha256"}
    data = (json.dumps(serialized, indent=2, allow_nan=False) + "\n").encode()
    require(not scope.lab or len(data) <= LAB_JOURNAL_MAX_BYTES, "Lab journal exceeds write bound")
    previous = None
    if path.exists():
        require(not path.is_symlink() and path.is_file(), "Private journal must be regular")
        previous = scope.read()
        require(state.get("_journal_sha256") == digest(previous), "Private journal CAS conflict")
        if scope.lab:
            previous_state = _journal_parse(previous)
            _journal_lab_envelope(previous_state, lab_context, lab_binding)
            _journal_lab_progress(previous_state, working)
    else:
        require("_journal_sha256" not in state, "Private journal disappeared")

    # The journal is not a bind-mounted configuration: atomic replacement is
    # necessary so interruption cannot leave a truncated recovery journal.
    def guard():
        scope.check()
        if scope.lab:
            require(network_policy._same_json(state, retained), "Lab journal caller state changed")
            _journal_lab_envelope(state, lab_context, lab_binding)

    guard()
    temporary = path.with_name(
        (".state.lab.private-" if scope.lab else ".state.private-") + uuid.uuid4().hex + ".tmp"
    )
    private_write(temporary, data)
    guard()
    if scope.lab:
        require(scope.read(temporary) == data, "Lab pending journal changed")
    require(
        scope.read() == previous if previous is not None else not path.exists(),
        "Private journal CAS conflict",
    )
    guard()
    os.replace(temporary, path)
    _journal_sync_directory(scope.folder)
    if scope.lab:
        guard()
        require(scope.read() == data, "Lab durable journal changed before receipt")
        guard()
        state["updated_utc"] = working["updated_utc"]
    state["_journal_sha256"] = digest(data)


@_journal_errors
def load_state(folder, *, lab_context=None, lab_binding=None):
    scope = _ReleaseJournalScope(folder, lab_context, lab_binding)
    path = scope.path
    require(not path.is_symlink() and path.is_file(), "Private journal must be regular")
    data = scope.read()
    state = _journal_parse(data)
    if scope.lab:
        _journal_lab_envelope(state, lab_context, lab_binding)
    else:
        _journal_production(state)
    scope.check()
    state["_journal_sha256"] = digest(data)
    return state


def network_save(folder, state):
    def persist(journal):
        state["network_transaction"] = copy.deepcopy(journal)
        save(folder, state)

    return persist


def prepare_network(folder, state, host):
    require(state["project"] == "docker", "Network project requires review")
    adapter = host.network_adapter()
    current = adapter.container(SERVICE)
    require(
        current["id"] == state["original_id"]
        and set(current["networks"]) == {network_policy.DEFAULT_NAME},
        "Original bridge network differs",
    )
    state["network_contract"] = NETWORK_RELEASE_SCHEMA
    state["original_network_projection"] = current
    state["network_intent"] = False
    network_transaction.NetworkTransaction.begin(
        adapter, network_save(folder, state), uuid.uuid4().hex
    )


def network_contract_present(state):
    markers = {
        "network_contract",
        "network_transaction",
        "original_network_projection",
        "network_intent",
    }
    if not (markers & set(state)):
        return False  # Genuine pre-network journal only; never infer from malformed schema.
    require(
        type(state.get("network_contract")) is int
        and state["network_contract"] == NETWORK_RELEASE_SCHEMA
        and markers <= set(state)
        and type(state["network_intent"]) is bool,
        "Missing reviewed network preparation; prepare a new transaction",
    )
    network_transaction.NetworkTransaction._validate_state(state["network_transaction"])
    original = network_transaction._container(state["original_network_projection"], SERVICE)
    require(
        original["id"] == state["original_id"]
        and set(original["networks"]) == {network_policy.DEFAULT_NAME},
        "Original network journal differs",
    )
    return True


def network_engine(folder, state, host, *, mutate=False):
    require(
        network_contract_present(state), "Missing network preparation; prepare a new transaction"
    )
    return network_transaction.NetworkTransaction(
        host.network_adapter(state, mutate=mutate),
        network_save(folder, state),
        state["network_transaction"],
    )


def audit_network(folder, state, host):
    """Read-only audit; never try to repair a drifted consumer or owned endpoint."""
    engine = network_engine(folder, state, host)
    engine._audit(engine.host.network(network_policy.NETWORK_NAME))


def audit_prepared_original(state, host, old):
    current = host.inspect(SERVICE)
    require(
        current["State"]["Running"]
        and current["Id"] == state["original_id"]
        and all(
            network_policy._same_json(current.get(key), old.get(key))
            for key in (
                "Image",
                "Config",
                "HostConfig",
                "Mounts",
                "NetworkSettings",
                "RestartCount",
            )
        )
        and current["State"].get("StartedAt") == old["State"].get("StartedAt"),
        "Original runtime changed since preparation",
    )
    projection = host.network_adapter(state).container(SERVICE)
    require(
        network_policy._same_json(projection, state["original_network_projection"]),
        "Original route or endpoint changed since preparation",
    )


def record_candidate(folder, state, host, old, live):
    """Verify the complete reviewed candidate before allowing its exact network ID."""
    require(
        type(live.get("Id")) is str and bool(re.fullmatch("[0-9a-f]{64}", live["Id"])),
        "Candidate identity is invalid",
    )
    require(
        state.get("candidate_id") in (None, live["Id"]),
        "Candidate rollback identity mismatch",
    )
    labels = live.get("Config", {}).get("Labels", {})
    require(
        labels.get("com.docker.compose.project") == state["project"]
        and labels.get("com.docker.compose.service") == SERVICE,
        "Candidate Compose ownership differs",
    )
    network_id = state["network_transaction"]["network"]["id"]
    require(bool(network_id), "Owned network identity is absent")
    assert_runtime(
        old,
        live,
        state["candidate_data"],
        CANDIDATE,
        state.get("managed_environment"),
        network_id=network_id,
    )
    state["candidate_id"] = live["Id"]
    save(folder, state)
    # record_bridge_id does not inspect/mutate Docker. Only after it is durable
    # may an adapter recognize the new bridge in network membership audits.
    network_engine(folder, state, host).record_bridge_id(live["Id"])
    audit_network(folder, state, host)


def assert_inputs(state):
    for path, checksum in state["inputs"].items():
        if checksum is None:
            require(not Path(path).exists(), "Previously absent private input appeared")
        else:
            require(
                Path(path).is_file() and file_digest(path) == checksum,
                "Private input/configuration changed",
            )


def assert_artifacts(folder, state):
    for relative, checksum in state["artifacts"].items():
        require(
            file_digest(folder / relative) == checksum,
            "Prepared release artifact changed",
        )


def input_hashes(compose_bytes):
    paths = {ROOT / ".env", ROOT / "private.env"}
    raw = yaml.safe_load(compose_bytes)
    for service in raw["services"].values():
        entries = service.get("env_file", [])
        for entry in [entries] if isinstance(entries, str) else entries:
            value = entry if isinstance(entry, str) else entry.get("path")
            require(value and "$" not in value, "Interpolated env_file requires review")
            path = (ROOT / value).resolve()
            require(path.is_file(), "Missing env_file")
            paths.add(path)
    # Track absence too, so creating .env later cannot silently alter resolution.
    return {str(path): file_digest(path) if path.exists() else None for path in paths}


def configuration_entries(folder, state):
    return [
        (
            Path(path),
            (folder / (key + ".before")).read_bytes(),
            (folder / (key + ".after")).read_bytes(),
        )
        for key, path in state["files"].items()
    ]


def assert_files(folder, state, side):
    for path, before, after in configuration_entries(folder, state):
        require(
            path.read_bytes() == (before if side == "before" else after),
            "Configuration CAS conflict",
        )


def gate(record):
    require(record.get("candidate_image") == CANDIDATE, "Release evidence image mismatch")
    for key in (
        "source_merged",
        "required_checks_passed",
        "grok_release_gate_satisfied",
        "browser_acceptance_passed",
        "private_recovery_passed",
        "network_plan_reviewed",
        "network_recovery_acceptance_passed",
        "network_outsider_denial_passed",
        "multi_network_nginx_validation_passed",
        "operator_deployment_approved",
    ):
        require(record.get(key) is True, "Unresolved release evidence: " + key)
    require(
        bool(re.fullmatch("[0-9a-f]{40}", record.get("source_commit", ""))),
        "Missing source commit",
    )
    require(
        bool(record.get("gate_evidence_reference")),
        "Missing Grok-specific gate provenance",
    )


def literal_compose_values(value):
    """Escape resolved dollars so Compose does not interpolate private values twice."""
    if isinstance(value, str):
        return value.replace("$", "$$")
    if isinstance(value, list):
        return [literal_compose_values(x) for x in value]
    if isinstance(value, dict):
        return {k: literal_compose_values(v) for k, v in value.items()}
    return value


def prepare(folder, fragment, host):
    old, nginx = host.inspect(SERVICE), host.inspect("nginx")
    require(
        old["Image"] == ORIGINAL and old["State"]["Running"],
        "Unexpected original runtime",
    )
    host.healthy(ORIGINAL, old["Id"])
    image_defaults = host.image(ORIGINAL)
    host.image(CANDIDATE)
    project = old["Config"]["Labels"]["com.docker.compose.project"]
    require(project == "docker", "Network project requires review")
    compose = ROOT / "docker-compose.yml"
    canonical_fragment = ROOT / "config/nginx/projects-locations.d/grok-bridge.conf"
    mounts = [m for m in old["Mounts"] if m["Destination"] == "/data"]
    require(
        len(mounts) == 1 and mounts[0]["Type"] == "volume" and mounts[0]["RW"],
        "Unexpected original /data",
    )
    original_data = Path(mounts[0]["Source"]).resolve()
    require(
        original_data.is_relative_to("/var/lib/docker/volumes") and original_data.name == "_data",
        "Unexpected original volume source",
    )
    host.exclusive_volume(str(original_data), old["Id"])
    compiled = [m["Source"] for m in nginx["Mounts"] if m["Destination"] == "/etc/nginx/nginx.conf"]
    require(len(compiled) == 1, "Ambiguous Nginx configuration mount")
    data = folder / "candidate-data"
    assert_separate_volume(original_data, data)
    cb, fb, nb = (
        compose.read_bytes(),
        canonical_fragment.read_bytes(),
        Path(compiled[0]).read_bytes(),
    )
    inputs = input_hashes(cb)
    proposal = fragment.read_bytes()
    resolved = json.loads(host.compose(compose, project, "config", "--format", "json"))
    staged = {
        "compose": (compose, cb, render_network_compose(cb, resolved, str(data))),
        "fragment": (canonical_fragment, fb, proposal),
        "nginx": (Path(compiled[0]), nb, render_nginx(nb, proposal)),
    }
    for key, (_, before, after) in staged.items():
        private_write(folder / (key + ".before"), before)
        private_write(folder / (key + ".after"), after)
    candidate = json.loads(
        host.compose(folder / "compose.after", project, "config", "--format", "json")
    )
    assert_network_resolved_scope(resolved, candidate, str(data))
    assert_original_environment(image_defaults, resolved["services"][SERVICE], old)
    assert_original_commands_and_volume(image_defaults, resolved, old)
    frozen = copy.deepcopy(resolved)
    frozen["services"][SERVICE]["image"] = ORIGINAL
    private_write(
        folder / "original-resolved.json", json.dumps(literal_compose_values(frozen)).encode()
    )
    rollback_resolved = json.loads(
        host.compose(folder / "original-resolved.json", project, "config", "--format", "json")
    )
    require(
        rollback_resolved == frozen, "Frozen rollback configuration does not round-trip exactly"
    )
    private_write(folder / "original-inspect.json", json.dumps(old).encode())
    private_write(folder / "nginx-inspect.json", json.dumps(nginx).encode())
    state = {
        "phase": "prepared_not_deployed",
        "project": project,
        "original_id": old["Id"],
        "original_image": ORIGINAL,
        "candidate_image": CANDIDATE,
        "original_data": str(original_data),
        "candidate_data": str(data),
        "files": {k: str(v[0]) for k, v in staged.items()},
        "inputs": inputs,
        "others": host.others(),
        "stop_intent": False,
        "cutover_intent": False,
        "deployed": False,
    }
    state["managed_environment"] = resolved["services"][SERVICE].get("environment", {})
    state["artifacts"] = {
        p.name: file_digest(p)
        for p in folder.iterdir()
        if p.name.endswith((".before", ".after"))
        or p.name in ("original-resolved.json", "original-inspect.json", "nginx-inspect.json")
    }
    host.validate_nginx(folder / "nginx.after", nginx, old)
    assert_files(folder, state, "before")
    assert_inputs(state)
    prepare_network(folder, state, host)
    require(
        state["network_transaction"]["consumers"]["nginx"]["id"] == nginx["Id"],
        "Nginx changed during preparation",
    )
    audit_prepared_original(state, host, old)
    return state


def fresh_snapshot(folder, state, host):
    source, target = Path(state["original_data"]), Path(state["candidate_data"])
    assert_separate_volume(source, target)
    require(not target.exists(), "Candidate data already exists; inspect previous attempt")
    size = int(host.run(["du", "-sb", str(source)]).split()[0])
    require(size <= 4 * GIB, "Profile exceeds reviewed four-GiB bound")
    capacity(folder, 2 * size + GIB)
    archive = folder / "data.private.tar"
    require(not archive.exists(), "Snapshot already exists; do not reuse across cutovers")
    original = host.inspect(SERVICE)
    require(
        original["Id"] == state["original_id"] and not original["State"]["Running"],
        "Original must remain quiesced while snapshotting",
    )
    host.run(
        [
            "tar",
            "--acls",
            "--xattrs",
            "--numeric-owner",
            "--sparse",
            "-cpf",
            str(archive),
            "-C",
            str(source),
            ".",
        ],
        timeout=300,
    )
    require(
        not host.inspect(state["original_id"])["State"]["Running"],
        "Original restarted during snapshot",
    )
    manifest = archive_manifest(archive)
    target.mkdir(mode=0o700)
    host.run(
        [
            "tar",
            "--acls",
            "--xattrs",
            "--numeric-owner",
            "--same-owner",
            "-xpf",
            str(archive),
            "-C",
            str(target),
        ],
        timeout=300,
    )
    verify_restore(target, manifest)
    manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
    private_write(folder / "manifest.private.json", manifest_bytes)
    state["snapshot"] = {
        "archive_sha256": file_digest(archive),
        "manifest_sha256": digest(manifest_bytes),
        "files": sum("sha256" in row for row in manifest),
        "restore_verified": True,
    }
    save(folder, state)


def assert_original_environment(image, service, live):
    expected = dict(x.split("=", 1) for x in image["Config"].get("Env", []))
    explicit = service.get("environment", {})
    require(
        all(value is not None for value in explicit.values()),
        "Unresolved managed environment",
    )
    expected.update({key: str(value) for key, value in explicit.items()})
    actual = dict(x.split("=", 1) for x in live["Config"]["Env"])
    require(
        actual == expected,
        "Effective original environment differs from canonical source",
    )


def assert_original_commands_and_volume(image, resolved, live):
    service = resolved["services"][SERVICE]
    for key, docker_key in (("command", "Cmd"), ("entrypoint", "Entrypoint")):
        expected = service.get(key)
        if expected is None:
            expected = image["Config"].get(docker_key)
        require(
            expected == live["Config"].get(docker_key),
            "Canonical command/entrypoint differs from live runtime",
        )
    data = [m for m in service.get("volumes", []) if m.get("target") == "/data"]
    require(
        len(data) == 1 and data[0].get("type") == "volume",
        "Canonical /data must be a named volume",
    )
    key = data[0]["source"]
    name = resolved.get("volumes", {}).get(key, {}).get("name")
    require(bool(name), "Resolved volume name missing")
    actual = [m for m in live["Mounts"] if m["Destination"] == "/data"]
    require(
        len(actual) == 1 and actual[0].get("Name") == name,
        "Canonical original volume differs from live volume",
    )


def _runtime_reject_lab(old, live, data, binding):
    require(binding is None, "Lab runtime binding requires its exact context")
    from network_lab_context import ROOT as LAB_ROOT

    paths = [data]
    for item in (old, live):
        labels = item.get("Config", {}).get("Labels") or {}
        require(
            not str(item.get("Name", "")).startswith("/codex-grok-nettest-")
            and not str(labels.get("com.docker.compose.project", "")).startswith(
                "codex-grok-nettest-"
            ),
            "Lab runtime identity requires its exact context",
        )
        paths.extend(m.get("Source") for m in item.get("Mounts", []))
    for value in paths:
        if isinstance(value, (str, PurePosixPath, Path)):
            path = PurePosixPath(value)
            require(
                path != LAB_ROOT and LAB_ROOT not in path.parents,
                "Lab runtime path requires its exact context",
            )


def _runtime_lab_path(value):
    require(type(value) is str, "Lab runtime path must be a canonical string")
    path = PurePosixPath(value)
    require(
        path.is_absolute()
        and str(path) == value
        and value != "/"
        and not value.startswith("//")
        and ".." not in path.parts
        and not any(character in value for character in "\\\r\n\0"),
        "Lab runtime path must be canonical and absolute",
    )
    return value


def _runtime_lab_policy(old, live, data, image, network_id, context, binding):
    """Check supplied records/binding only, without observing or admitting a host."""
    names = network_policy._identity(context)
    try:
        context.verify_journal_binding(binding)
    except ValueError:
        raise Refused("Lab runtime journal binding differs") from None
    network_policy._json_value(old)
    network_policy._json_value(live)
    pins = dict(context.images)
    require(
        type(image) is str and image in (pins["original"], pins["candidate"]),
        "Lab runtime image is not pinned",
    )
    for item in (old, live):
        require(type(item) is dict, "Invalid lab runtime record")
        require(
            type(item.get("Id")) is str
            and bool(re.fullmatch(r"[0-9a-f]{64}", item["Id"]))
            and item.get("Name") == "/" + names[SERVICE],
            "Lab runtime physical identity differs",
        )
        require(
            type(item.get("Config")) is dict and type(item.get("HostConfig")) is dict,
            "Invalid lab runtime configuration",
        )
        labels = item["Config"].get("Labels")
        require(
            type(labels) is dict
            and labels.get("com.docker.compose.project") == context.project
            and labels.get("com.docker.compose.service") == SERVICE,
            "Lab runtime Compose ownership differs",
        )
        require(
            type(item.get("RestartCount", 0)) is int and item.get("RestartCount", 0) >= 0,
            "Invalid lab restart count",
        )
        mounts = item.get("Mounts")
        require(
            type(mounts) is list and all(type(m) is dict for m in mounts),
            "Invalid lab runtime mounts",
        )
        require(
            all(
                type(m.get("Source")) is str
                and type(m.get("Type")) is str
                and type(m.get("RW")) is bool
                for m in mounts
            ),
            "Malformed lab mount fields",
        )
        destinations = [mount.get("Destination") for mount in mounts]
        require(
            all(type(target) is str for target in destinations)
            and len(destinations) == len(set(destinations)),
            "Ambiguous lab mount destinations",
        )
        environment = item["Config"].get("Env")
        require(
            type(environment) is list
            and all(
                type(entry) is str and "=" in entry and entry.split("=", 1)[0]
                for entry in environment
            ),
            "Invalid lab runtime environment",
        )
        keys = [entry.split("=", 1)[0] for entry in environment]
        require(len(keys) == len(set(keys)), "Duplicate lab environment keys")
        require(type(item.get("NetworkSettings")) is dict, "Invalid lab network settings")
        require(
            type(item["NetworkSettings"].get("Networks")) is dict, "Invalid lab network mappings"
        )
        require(
            all(
                type(name) is str and type(endpoint) is dict
                for name, endpoint in item["NetworkSettings"]["Networks"].items()
            ),
            "Malformed lab network endpoint",
        )
        require(type(item["NetworkSettings"].get("Ports", {})) is dict, "Invalid lab port mappings")
    require(
        old.get("Image") == pins["original"] and live.get("Image") == image,
        "Lab runtime immutable image differs",
    )
    old_networks = old["NetworkSettings"]["Networks"]
    default = names[network_policy.DEFAULT_NAME]
    require(
        set(old_networks) == {default} and type(old_networks[default]) is dict,
        "Original lab default network differs",
    )
    original_network_id = old_networks[default].get("NetworkID")
    require(
        type(original_network_id) is str
        and bool(re.fullmatch(r"[0-9a-f]{64}", original_network_id))
        and old["HostConfig"].get("NetworkMode") == default,
        "Original lab network identity differs",
    )
    require(
        not old["HostConfig"].get("PortBindings")
        and not any(old["NetworkSettings"].get("Ports", {}).values()),
        "Original lab service ports must be unpublished",
    )
    original_mounts = [m for m in old["Mounts"] if m["Destination"] == "/data"]
    current_mounts = [m for m in live["Mounts"] if m["Destination"] == "/data"]
    require(len(original_mounts) == len(current_mounts) == 1, "Missing or duplicate lab data mount")
    original, current = original_mounts[0], current_mounts[0]
    require(
        original.get("Type") == "volume"
        and original.get("RW") is True
        and type(original.get("Name")) is str
        and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", original["Name"])),
        "Original lab data requires a writable named volume",
    )
    _runtime_lab_path(original.get("Source"))
    _runtime_lab_path(current.get("Source"))
    _runtime_lab_path(data)
    require(current.get("RW") is True, "Lab data mount must remain writable")
    if image == pins["candidate"]:
        candidate, additions = _compose_parameters(data, context)
        require(
            type(network_id) is str
            and bool(re.fullmatch(r"[0-9a-f]{64}", network_id))
            and network_id != original_network_id,
            "Lab candidate requires its exact owned dedicated network ID",
        )
        require(live["Id"] != old["Id"], "Candidate cannot reuse the original container identity")
        require(
            current.get("Type") == "bind" and not current.get("Name") and current["Source"] == data,
            "Lab candidate data binding differs",
        )
    else:
        require(network_id is None, "Original lab runtime cannot use a candidate network ID")
        require(
            data == original["Source"]
            and all(
                network_policy._same_json(current.get(key), original[key])
                for key in ("Name", "Source", "Type", "RW")
            ),
            "Original lab named-volume binding changed",
        )
        candidate, additions = _compose_parameters(
            str(context.generation_root / "candidate-data"), context
        )
    require(
        bool(dict(entry.split("=", 1) for entry in old["Config"]["Env"]).get("BRIDGE_TOKEN")),
        "Original lab token is missing",
    )
    return candidate, additions, default, names[network_policy.NETWORK_NAME]


def assert_runtime(
    old,
    live,
    data,
    image,
    managed_environment=None,
    *,
    network_id=None,
    lab_context=None,
    lab_binding=None,
):
    """Compare supplied configuration; lab binding is not observed identity or admission."""
    candidate, additions = CANDIDATE, ENV_ADDITIONS
    default, dedicated = network_policy.DEFAULT_NAME, network_policy.NETWORK_NAME
    if lab_context is None:
        _runtime_reject_lab(old, live, data, lab_binding)
    else:
        candidate, additions, default, dedicated = _runtime_lab_policy(
            old, live, data, image, network_id, lab_context, lab_binding
        )
        if managed_environment is not None:
            network_policy._json_value(managed_environment)
            require(type(managed_environment) is dict, "Invalid lab managed environment")
    require(live["Image"] == image, "Runtime image mismatch")
    expected_restarts = old.get("RestartCount", 0) if live["Id"] == old["Id"] else 0
    require(
        live.get("RestartCount", 0) == expected_restarts,
        "Runtime restarted unexpectedly",
    )
    for key in ("Cmd", "Entrypoint", "Healthcheck"):
        require(
            (
                network_policy._same_json(old["Config"].get(key), live["Config"].get(key))
                if lab_context is not None
                else old["Config"].get(key) == live["Config"].get(key)
            ),
            "Command/entrypoint/healthcheck drift",
        )
    keys = (
        "Memory",
        "MemorySwap",
        "NanoCpus",
        "CpuShares",
        "PidsLimit",
        "RestartPolicy",
        "ShmSize",
        "Privileged",
        "CapAdd",
        "CapDrop",
        "SecurityOpt",
        "ReadonlyRootfs",
        "PortBindings",
    )
    require(
        all(
            network_policy._same_json(old["HostConfig"].get(k), live["HostConfig"].get(k))
            if lab_context is not None
            else old["HostConfig"].get(k) == live["HostConfig"].get(k)
            for k in keys
        ),
        "Runtime limits/security drift",
    )
    old_networks, live_networks = (
        old["NetworkSettings"]["Networks"],
        live["NetworkSettings"]["Networks"],
    )
    if network_id is not None:
        require(
            image == candidate
            and type(network_id) is str
            and bool(re.fullmatch("[0-9a-f]{64}", network_id))
            and set(old_networks) == {default}
            and set(live_networks) == {dedicated}
            and live_networks[dedicated].get("NetworkID") == network_id
            and live["HostConfig"].get("NetworkMode") == dedicated
            and not live["HostConfig"].get("PortBindings")
            and not any(live["NetworkSettings"].get("Ports", {}).values()),
            "Candidate network differs from the owned dedicated network",
        )
    else:
        require(
            old["HostConfig"].get("NetworkMode") == live["HostConfig"].get("NetworkMode")
            and set(old_networks) == set(live_networks)
            and all(
                old_networks[name].get("NetworkID") == live_networks[name].get("NetworkID")
                for name in old_networks
            ),
            "Network drift",
        )
    old_mounts = {
        (m["Destination"], m["Type"], m["Source"], m["RW"])
        for m in old["Mounts"]
        if m["Destination"] != "/data"
    }
    new_mounts = {
        (m["Destination"], m["Type"], m["Source"], m["RW"])
        for m in live["Mounts"]
        if m["Destination"] != "/data"
    }
    require(old_mounts == new_mounts, "Unrelated mount drift")
    mounts = [m for m in live["Mounts"] if m["Destination"] == "/data"]
    require(
        len(mounts) == 1
        and mounts[0]["RW"]
        and (
            mounts[0]["Source"] == data
            if lab_context is not None
            else Path(mounts[0]["Source"]).resolve() == Path(data).resolve()
        ),
        "Wrong runtime data mount",
    )
    require(
        mounts[0]["Type"] == ("bind" if image == candidate else "volume"),
        "Wrong runtime volume type",
    )
    previous = dict(x.split("=", 1) for x in old["Config"]["Env"])
    current = dict(x.split("=", 1) for x in live["Config"]["Env"])
    require(
        current.get("BRIDGE_TOKEN") == previous.get("BRIDGE_TOKEN"),
        "Service token changed",
    )
    expected = {key: str(value) for key, value in (managed_environment or {}).items()}
    if image == candidate:
        expected.update(additions)
    require(
        all(current.get(key) == value for key, value in expected.items()),
        "Managed environment drift",
    )
    if image == candidate:
        require(
            all(current.get(k) == v for k, v in additions.items()),
            "Bridge origin/auth drift",
        )


def nonprovider_probe(host, live):
    # Token is read inside the container, never interpolated in argv or logs.
    # Empty chat input is rejected before any browser/provider action. The exact
    # detail distinguishes successful machine authentication from a generic 400.
    script = """import json,os,urllib.request,urllib.error,socket
base='http://'+socket.gethostbyname(socket.gethostname())+':8443'
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,*args,**kwargs):return None
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
def request(path,token=None,body=None):
 headers={'X-Bridge-Token':token} if token is not None else {}
 if body is not None:headers['Content-Type']='application/json'
 r=urllib.request.Request(base+path,headers=headers,data=body)
 try:
  with opener.open(r,timeout=5) as response:return response.status,response.read(65537)
 except urllib.error.HTTPError as error:
  with error:return error.code,error.read(65537)
token=os.environ['BRIDGE_TOKEN']
assert token and token.strip()
assert request('/healthz')[0]==200
assert request('/api/status')[0]==401
assert request('/api/status',token)[0]==401
assert request('/api/chat',body=b'{}')[0]==401
wrong=token+'-invalid-probe-value'
assert request('/api/chat',wrong,b'{}')[0]==401
status,body=request('/api/chat',token,b'{}')
assert status==400 and len(body)<=65536
assert json.loads(body)=={'detail':'conversation_id is required'}
print('nonprovider-probe-passed')
"""
    require(
        host.run(["docker", "exec", live["Id"], "python", "-c", script]).strip()
        == "nonprovider-probe-passed",
        "Non-provider access check failed",
    )


def restore_configs(folder, state):
    conflicts = []
    for path, before, after in reversed(configuration_entries(folder, state)):
        if path.read_bytes() == before:
            continue
        try:
            cas_write(path, after, before)
        except (Refused, OSError):
            conflicts.append(str(path))
    return conflicts


def rollback(folder, state, host):
    return _rollback_transaction(folder, state, host)


def _rollback_transaction(folder, state, host):
    """Production recovery stays independent of deployment approval evidence."""
    _production_deployment_scope(folder, state)
    dependencies = _ProductionDeployment(folder, state, host)
    has_network = dependencies.has_network()
    assert_artifacts(folder, state)
    old = json.loads((folder / "original-inspect.json").read_text())
    nginx = json.loads((folder / "nginx-inspect.json").read_text())
    host.image(ORIGINAL)
    state["phase"] = "rollback_intent"
    dependencies.persist()
    live = host.optional_service()
    require(
        live is None or live["Image"] in (ORIGINAL, CANDIDATE),
        "Unexpected rollback runtime; refuse to stop it",
    )
    if has_network:
        if (
            live is not None
            and live["Image"] == CANDIDATE
            and state.get("cutover_intent")
            and state["network_transaction"]["bridge_id"] is None
        ):
            # Reconcile a crash between candidate-id persistence and the nested
            # bridge binding; full runtime validation precedes any network audit.
            dependencies.record_candidate(old, live)
        if not state.get("stop_intent"):
            require(state.get("network_intent") is True, "No network or stop action was requested")
            # A pre-stop interruption must not stop/recreate the healthy original.
            assert_files(folder, state, "before")
            dependencies.audit_original(old)
            dependencies.network(mutate=True).rollback()
            state.update(phase="network_only_rolled_back", deployed=False)
            dependencies.persist()
            require(host.others() == state["others"], "Unrelated container changed during rollback")
            return
        dependencies.audit_network()
    if live is not None and live["Image"] == CANDIDATE:
        require(
            state.get("candidate_id") == live["Id"],
            "Candidate rollback identity mismatch",
        )
        host.stop(live["Id"], allow_unclean=True)
    conflicts = dependencies.restore_configs()
    # Frozen resolved config preserves the original token, image and named volume
    # even if another operator changed a canonical env/config file meanwhile.
    if live is not None and live["Image"] == ORIGINAL:
        require(
            live["Id"] in (state["original_id"], state.get("rollback_id")),
            "Unknown original-image container appeared during rollback",
        )
        host.start_original(live["Id"])
    else:
        host.compose(
            folder / "original-resolved.json",
            state["project"],
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            SERVICE,
        )
    live = host.healthy(ORIGINAL)
    dependencies.verify_original(old, live)
    # Persist the newly recovered original before any later check/reload can fail;
    # a second controller must recognize this exact rollback container.
    state.update(rollback_id=live["Id"], phase="original_runtime_recovered")
    dependencies.persist()
    if not conflicts:
        host.reload_nginx(nginx)
        if has_network:
            # Only the original bridge on its original network may remain when
            # consumer attachments are detached. The engine refuses a bridge member.
            dependencies.network(mutate=True).rollback()
    state.update(
        phase="rolled_back" if not conflicts else "original_runtime_recovered_config_conflict",
        deployed=False,
        rollback_id=live["Id"],
        configuration_conflicts=conflicts,
    )
    dependencies.persist()
    require(host.others() == state["others"], "Unrelated container changed during rollback")
    require(
        not conflicts,
        "Original runtime recovered; configuration conflict requires reconciliation",
    )


def deploy(folder, state, host, evidence):
    return _deploy_transaction(folder, state, host, evidence)


def _production_deployment_scope(folder, state, evidence=None):
    """Reject lab inputs; omitted evidence imposes no deployment gate on recovery."""
    from network_lab_context import ROOT as LAB_ROOT

    _journal_production(state)
    for record in (state,) if evidence is None else (state, evidence):
        require(
            not {"lab_context", "lab_binding", "execution_admission"} & set(record)
            and record.get("mode") != "lab"
            and not str(record.get("schema", "")).startswith("grok-network-lab-"),
            "Lab evidence or state cannot dispatch a production deployment",
        )
    path = PurePosixPath(str(folder).replace("\\", "/"))
    require(
        path != LAB_ROOT and LAB_ROOT not in path.parents,
        "Lab generation cannot dispatch production",
    )


class _ProductionDeployment:
    """Fixed delegation, not an admission token or a Python tampering boundary.

    Only the internal production entry constructs this dispatcher. There is no
    lab implementation, caller-supplied callback, or configurable policy/pin.
    """

    def __init__(self, folder, state, host):
        self.folder, self.state, self.host = folder, state, host

    def persist(self):
        return save(self.folder, self.state)

    def network(self, *, mutate=False):
        return network_engine(self.folder, self.state, self.host, mutate=mutate)

    def audit_network(self):
        return audit_network(self.folder, self.state, self.host)

    def audit_original(self, old):
        return audit_prepared_original(self.state, self.host, old)

    def record_candidate(self, old, live):
        return record_candidate(self.folder, self.state, self.host, old, live)

    def verify_candidate(self, old, live):
        return assert_runtime(
            old,
            live,
            self.state["candidate_data"],
            CANDIDATE,
            self.state.get("managed_environment"),
            network_id=self.state["network_transaction"]["network"]["id"],
        )

    def snapshot(self):
        return fresh_snapshot(self.folder, self.state, self.host)

    def capacity(self):
        return capacity(self.folder)

    def rollback(self):
        return rollback(self.folder, self.state, self.host)

    def has_network(self):
        return network_contract_present(self.state)

    def restore_configs(self):
        return restore_configs(self.folder, self.state)

    def verify_original(self, old, live):
        return assert_runtime(
            old,
            live,
            self.state["original_data"],
            ORIGINAL,
            self.state.get("managed_environment"),
        )


def _deploy_transaction(folder, state, host, evidence):
    """One production algorithm; future lab admission/dispatch remains unimplemented."""
    gate(evidence)
    _production_deployment_scope(folder, state, evidence)
    dependencies = _ProductionDeployment(folder, state, host)
    require(state["phase"] == "prepared_not_deployed", "Not a fresh prepared transaction")
    assert_artifacts(folder, state)
    assert_files(folder, state, "before")
    assert_inputs(state)
    host.image(ORIGINAL)
    host.image(CANDIDATE)
    require(
        host.others() == state["others"],
        "Unrelated containers changed since preparation",
    )
    host.healthy(ORIGINAL, state["original_id"])
    host.exclusive_volume(state["original_data"], state["original_id"])
    old = json.loads((folder / "original-inspect.json").read_text())
    nginx = json.loads((folder / "nginx-inspect.json").read_text())
    dependencies.network()
    dependencies.audit_network()
    dependencies.audit_original(old)
    # Resolve production upstream DNS while the original is still available.
    host.validate_nginx(folder / "nginx.after", nginx, old)
    assert_files(folder, state, "before")
    assert_inputs(state)
    try:
        # The nested journal persists intent before every network action. This
        # outer marker makes pre-stop interruptions explicitly recoverable.
        state.update(phase="attaching_consumers", network_intent=True)
        dependencies.persist()
        dependencies.network(mutate=True).attach_consumers()
        dependencies.audit_original(old)
        assert_files(folder, state, "before")
        assert_inputs(state)
        require(host.others() == state["others"], "Unrelated runtime changed before stop")
        # Persist BEFORE submitting the stop, including in timeout/error paths.
        state.update(phase="quiescing_original", stop_intent=True)
        dependencies.persist()
        host.stop(state["original_id"])
        dependencies.snapshot()
        assert_files(folder, state, "before")
        assert_inputs(state)
        state.update(phase="cutover_intent", cutover_intent=True)
        dependencies.persist()
        for path, before, after in configuration_entries(folder, state):
            cas_write(path, before, after)
        assert_files(folder, state, "after")
        host.compose(
            Path(state["files"]["compose"]),
            state["project"],
            "up",
            "-d",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            "--force-recreate",
            SERVICE,
        )
        live = host.inspect(SERVICE)
        require(live["Image"] == CANDIDATE, "Candidate creation selected wrong image")
        dependencies.record_candidate(old, live)
        live = host.healthy(CANDIDATE, live["Id"])
        dependencies.verify_candidate(old, live)
        dependencies.audit_network()
        nonprovider_probe(host, live)
        host.reload_nginx(nginx)
        for _ in range(3):
            time.sleep(10)
            live = host.healthy(CANDIDATE, state["candidate_id"])
            dependencies.verify_candidate(old, live)
            dependencies.audit_network()
            nonprovider_probe(host, live)
            require(
                host.others() == state["others"],
                "Unrelated runtime changed during soak",
            )
            assert_files(folder, state, "after")
            assert_inputs(state)
        dependencies.capacity()
        state.update(
            phase="deployed_local_checks_passed",
            deployed=True,
            soak_seconds=30,
            provider_acceptance=False,
            public_browser_acceptance=False,
        )
        dependencies.persist()
    except BaseException as error:
        state.update(phase="deployment_failed", error_class=type(error).__name__)
        dependencies.persist()
        if state["cutover_intent"]:
            # Reconcile an ambiguous compose command without trusting a name alone.
            live = host.optional_service()
            if (
                live is not None
                and live["Image"] == CANDIDATE
                and (
                    "candidate_id" not in state or state["network_transaction"]["bridge_id"] is None
                )
            ):
                dependencies.record_candidate(old, live)
            dependencies.rollback()
        else:
            dependencies.rollback()
            state.update(
                phase=(
                    "pre_cutover_failed_original_restarted"
                    if state["stop_intent"]
                    else "pre_stop_failed_network_recovered"
                ),
                deployed=False,
            )
            dependencies.persist()
        raise
    return state


def validate_release_path(folder, mode):
    # New preparations/deploy snapshots stay on the requested NAS. Existing
    # local generations remain available for rollback without moving any data.
    roots = (BACKUPS, LEGACY_NAS_BACKUPS, LEGACY_BACKUPS) if mode == "rollback" else (BACKUPS,)
    require(
        any(folder.is_relative_to(root) and folder != root for root in roots),
        "Use a dedicated private NAS release directory (legacy paths are rollback-only)",
    )


def require_nas_storage():
    nas = Path("/mnt/s")
    require(
        os.path.ismount(nas) and nas.stat().st_dev != Path("/").stat().st_dev,
        "NAS is not mounted separately; refusing new root-filesystem snapshots",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "deploy", "rollback"))
    parser.add_argument("release_dir", type=Path)
    parser.add_argument("--fragment", type=Path)
    parser.add_argument("--release-evidence", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    folder = args.release_dir.resolve()
    validate_release_path(folder, args.mode)
    with operation_lock():
        if args.mode != "rollback":
            require_nas_storage()
        if args.mode == "prepare":
            require(
                args.fragment is not None and not folder.exists(),
                "Prepare needs fragment and a new directory",
            )
            folder.mkdir(parents=True, mode=0o700)
            require(
                folder.stat().st_uid == 0 and stat.S_IMODE(folder.stat().st_mode) == 0o700,
                "New release directory must be root-only before private inputs are saved",
            )
            state = prepare(folder, args.fragment, Host(folder))
        else:
            require(
                folder.stat().st_uid == 0 and stat.S_IMODE(folder.stat().st_mode) == 0o700,
                "Release directory must be root-only",
            )
            state = load_state(folder)
            if args.mode == "deploy":
                require(
                    args.release_evidence is not None,
                    "Reviewed release evidence is required",
                )
                state = deploy(
                    folder,
                    state,
                    Host(folder),
                    json.loads(args.release_evidence.read_text()),
                )
            else:
                require(
                    state.get("stop_intent") or state.get("network_intent"),
                    "No network, stop or cutover action was requested",
                )
                rollback(folder, state, Host(folder))
        print(
            json.dumps(
                {
                    "phase": state["phase"],
                    "deployed": state["deployed"],
                    "provider_acceptance": False,
                }
            )
        )


if __name__ == "__main__":
    main()
