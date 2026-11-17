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
from pathlib import Path, PurePosixPath

import yaml

SERVICE = "llm-proxy2-grok-bridge"
ORIGINAL = "sha256:be048c4e54812ef811f14520a0fed1bc71486ad7619296b11c189d482846856e"
CANDIDATE = "sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9"
ROOT = Path("/home/dblagbro/docker")
BACKUPS = Path("/mnt/s/backups/codex-security-20261109/grok-releases")
LEGACY_BACKUPS = Path("/var/backups/codex-grok-releases")
MARKER = "# ── from projects-locations.d/grok-bridge.conf ──"
ENV_ADDITIONS = {
    "BRIDGE_PUBLIC_PATH": "/grok-bridge",
    "BRIDGE_PUBLIC_ORIGIN": "https://www.voipguru.org",
    "BRIDGE_AUTH_URL": "http://llm-proxy2:3000/api/auth/me",
}
GIB = 1024**3


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


def candidate_service(before, data_path):
    require(PurePosixPath(data_path).is_absolute(), "Candidate data path must be absolute")
    result = copy.deepcopy(before)
    result["image"] = CANDIDATE
    env = environment_mapping(result.get("environment", {}))
    require(bool(env.get("BRIDGE_TOKEN")), "Missing existing service token")
    require(
        env.get("BRIDGE_PUBLIC_PATH") == "/grok-bridge",
        "Unexpected existing public path",
    )
    env.update(ENV_ADDITIONS)
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


def render_compose(content, data_path):
    text = content.decode("utf-8")
    require("\r" not in text, "Unexpected Compose line endings")
    before = yaml.safe_load(text)
    require(
        isinstance(before, dict) and SERVICE in before.get("services", {}),
        "Missing service",
    )
    start, end = service_span(text)
    service = candidate_service(before["services"][SERVICE], data_path)
    rendered = yaml.safe_dump({SERVICE: service}, sort_keys=False, allow_unicode=True)
    block = "".join("  " + line + "\n" for line in rendered.splitlines())
    output = (text[:start] + block + text[end:]).encode()
    expected = copy.deepcopy(before)
    expected["services"][SERVICE] = service
    require(yaml.safe_load(output) == expected, "Non-Grok Compose modification")
    return output


def assert_resolved_scope(before, after, data_path):
    expected = copy.deepcopy(before)
    expected["services"][SERVICE] = candidate_service(before["services"][SERVICE], data_path)
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
        actual == expected,
        "Resolved Compose scope changed outside reviewed Grok fields",
    )


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
        try:
            result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Host command exceeded deadline; reconcile actual state") from exc
        with (self.folder / "commands.private.log").open("ab") as stream:
            stream.write(result.stdout + result.stderr)
        require(result.returncode == 0, "Host command failed; inspect private log")
        return result.stdout.decode()

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
        return {
            x["Name"]: [x["Id"], x["State"]["Status"]] for x in values if x["Name"] != "/" + SERVICE
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

    def validate_nginx(self, config, original_nginx):
        current = self.inspect("nginx")
        require(
            current["Id"] == original_nginx["Id"] and current["Image"] == original_nginx["Image"],
            "Nginx identity changed",
        )
        networks = list(current["NetworkSettings"]["Networks"])
        require(len(networks) == 1, "Nginx network needs review")
        logs = self.folder / "nginx-validation-logs"
        logs.mkdir(mode=0o700, exist_ok=True)
        args = [
            "docker",
            "create",
            "--network",
            networks[0],
            "--memory",
            "128m",
            "--entrypoint",
            "nginx",
        ]
        for mount in current["Mounts"]:
            source, destination = mount["Source"], mount["Destination"]
            if destination == "/etc/nginx/nginx.conf":
                source = str(config)
            elif destination == "/var/log/nginx":
                source = str(logs)
            args += [
                "-v",
                source + ":" + destination + ("" if destination == "/var/log/nginx" else ":ro"),
            ]
        cid = self.run([*args, current["Image"], "-t"]).strip()
        require(bool(re.fullmatch("[0-9a-f]{64}", cid)), "Invalid owned validation ID")
        try:
            self.run(["docker", "start", "-a", cid], timeout=90)
            require(self.inspect(cid)["State"]["ExitCode"] == 0, "Staged nginx -t failed")
        finally:
            # Only the immutable ID returned by this invocation is removable.
            self.run(["docker", "rm", "-f", cid])

    def reload_nginx(self, original_nginx):
        require(
            self.inspect("nginx")["Id"] == original_nginx["Id"],
            "Nginx identity changed",
        )
        self.run(["docker", "exec", original_nginx["Id"], "nginx", "-t"])
        self.run(["docker", "exec", original_nginx["Id"], "nginx", "-s", "reload"])


def save(folder, state):
    state["updated_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    path = Path(folder) / "state.private.json"
    data = (json.dumps(state, indent=2) + "\n").encode()
    if path.exists():
        cas_write(path, path.read_bytes(), data)
    else:
        private_write(path, data)


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
    staged = {
        "compose": (compose, cb, render_compose(cb, str(data))),
        "fragment": (canonical_fragment, fb, proposal),
        "nginx": (Path(compiled[0]), nb, render_nginx(nb, proposal)),
    }
    for key, (_, before, after) in staged.items():
        private_write(folder / (key + ".before"), before)
        private_write(folder / (key + ".after"), after)
    resolved = json.loads(host.compose(compose, project, "config", "--format", "json"))
    candidate = json.loads(
        host.compose(folder / "compose.after", project, "config", "--format", "json")
    )
    assert_resolved_scope(resolved, candidate, str(data))
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
    host.validate_nginx(folder / "nginx.after", nginx)
    assert_files(folder, state, "before")
    assert_inputs(state)
    save(folder, state)
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


def assert_runtime(old, live, data, image, managed_environment=None):
    require(live["Image"] == image, "Runtime image mismatch")
    expected_restarts = old.get("RestartCount", 0) if live["Id"] == old["Id"] else 0
    require(
        live.get("RestartCount", 0) == expected_restarts,
        "Runtime restarted unexpectedly",
    )
    for key in ("Cmd", "Entrypoint", "Healthcheck"):
        require(
            old["Config"].get(key) == live["Config"].get(key),
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
        "NetworkMode",
        "Privileged",
        "CapAdd",
        "CapDrop",
        "SecurityOpt",
        "ReadonlyRootfs",
        "PortBindings",
    )
    require(
        all(old["HostConfig"].get(k) == live["HostConfig"].get(k) for k in keys),
        "Runtime limits/security drift",
    )
    require(
        set(old["NetworkSettings"]["Networks"]) == set(live["NetworkSettings"]["Networks"]),
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
        and Path(mounts[0]["Source"]).resolve() == Path(data).resolve(),
        "Wrong runtime data mount",
    )
    require(
        mounts[0]["Type"] == ("bind" if image == CANDIDATE else "volume"),
        "Wrong runtime volume type",
    )
    previous = dict(x.split("=", 1) for x in old["Config"]["Env"])
    current = dict(x.split("=", 1) for x in live["Config"]["Env"])
    require(
        current.get("BRIDGE_TOKEN") == previous.get("BRIDGE_TOKEN"),
        "Service token changed",
    )
    expected = {key: str(value) for key, value in (managed_environment or {}).items()}
    if image == CANDIDATE:
        expected.update(ENV_ADDITIONS)
    require(
        all(current.get(key) == value for key, value in expected.items()),
        "Managed environment drift",
    )
    if image == CANDIDATE:
        require(
            all(current.get(k) == v for k, v in ENV_ADDITIONS.items()),
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
    assert_artifacts(folder, state)
    old = json.loads((folder / "original-inspect.json").read_text())
    nginx = json.loads((folder / "nginx-inspect.json").read_text())
    host.image(ORIGINAL)
    state["phase"] = "rollback_intent"
    save(folder, state)
    live = host.optional_service()
    require(
        live is None or live["Image"] in (ORIGINAL, CANDIDATE),
        "Unexpected rollback runtime; refuse to stop it",
    )
    if live is not None and live["Image"] == CANDIDATE:
        require(
            state.get("candidate_id") == live["Id"],
            "Candidate rollback identity mismatch",
        )
        host.stop(live["Id"], allow_unclean=True)
    conflicts = restore_configs(folder, state)
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
    assert_runtime(old, live, state["original_data"], ORIGINAL, state.get("managed_environment"))
    if not conflicts:
        host.reload_nginx(nginx)
    state.update(
        phase="rolled_back" if not conflicts else "original_runtime_recovered_config_conflict",
        deployed=False,
        rollback_id=live["Id"],
        configuration_conflicts=conflicts,
    )
    save(folder, state)
    require(host.others() == state["others"], "Unrelated container changed during rollback")
    require(
        not conflicts,
        "Original runtime recovered; configuration conflict requires reconciliation",
    )


def deploy(folder, state, host, evidence):
    gate(evidence)
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
    # Resolve production upstream DNS while the original is still available.
    host.validate_nginx(folder / "nginx.after", nginx)
    assert_files(folder, state, "before")
    assert_inputs(state)
    try:
        # Persist BEFORE submitting the stop, including in timeout/error paths.
        state.update(phase="quiescing_original", stop_intent=True)
        save(folder, state)
        host.stop(state["original_id"])
        fresh_snapshot(folder, state, host)
        assert_files(folder, state, "before")
        assert_inputs(state)
        state.update(phase="cutover_intent", cutover_intent=True)
        save(folder, state)
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
        state["candidate_id"] = live["Id"]
        save(folder, state)
        live = host.healthy(CANDIDATE, live["Id"])
        assert_runtime(
            old,
            live,
            state["candidate_data"],
            CANDIDATE,
            state.get("managed_environment"),
        )
        nonprovider_probe(host, live)
        host.reload_nginx(nginx)
        for _ in range(3):
            time.sleep(10)
            live = host.healthy(CANDIDATE, state["candidate_id"])
            assert_runtime(
                old,
                live,
                state["candidate_data"],
                CANDIDATE,
                state.get("managed_environment"),
            )
            nonprovider_probe(host, live)
            require(
                host.others() == state["others"],
                "Unrelated runtime changed during soak",
            )
            assert_files(folder, state, "after")
            assert_inputs(state)
        capacity(folder)
        state.update(
            phase="deployed_local_checks_passed",
            deployed=True,
            soak_seconds=30,
            provider_acceptance=False,
            public_browser_acceptance=False,
        )
        save(folder, state)
    except BaseException as error:
        state.update(phase="deployment_failed", error_class=type(error).__name__)
        save(folder, state)
        if state["cutover_intent"]:
            # Reconcile an ambiguous compose command without trusting a name alone.
            live = host.optional_service()
            if live is not None and live["Image"] == CANDIDATE and "candidate_id" not in state:
                require(
                    any(
                        m["Destination"] == "/data" and m["Source"] == state["candidate_data"]
                        for m in live["Mounts"]
                    ),
                    "Unrecognized candidate after ambiguous compose",
                )
                state["candidate_id"] = live["Id"]
                save(folder, state)
            rollback(folder, state, host)
        else:
            current = host.optional_service()
            if current is None:
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
                recovered = host.healthy(ORIGINAL)
                assert_runtime(
                    old,
                    recovered,
                    state["original_data"],
                    ORIGINAL,
                    state.get("managed_environment"),
                )
                state["rollback_id"] = recovered["Id"]
            else:
                host.start_original(state["original_id"])
            state.update(phase="pre_cutover_failed_original_restarted", deployed=False)
            save(folder, state)
        raise
    return state


def validate_release_path(folder, mode):
    # New preparations/deploy snapshots stay on the requested NAS. Existing
    # local generations remain available for rollback without moving any data.
    roots = (BACKUPS, LEGACY_BACKUPS) if mode == "rollback" else (BACKUPS,)
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
            state = json.loads((folder / "state.private.json").read_text())
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
                require(state.get("stop_intent"), "No stop or cutover was requested")
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
