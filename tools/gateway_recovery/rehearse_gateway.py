"""Offline gateway recovery rehearsal; never mounts or mutates production data.

Linux/root only. Execute directly under the reviewed shared capacity_guard.py.
No container is started until its immutable image, mounts and isolation are checked.
Raw Docker results, environment, hashes of credential records and logs are private.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import time
from pathlib import Path

ORIGINAL = "sha256:21a85131e8b68dbdcd13c5bc0d7a5ec56fd971565ed3d481eadb1b75e33cd900"
CANDIDATE = "sha256:13771511ed12cf18e122978ccbe48af43010dbf91aa81294dc8eb0d08d00acc3"
SNAPSHOT = Path(
    "/var/backups/codex-remediation-20261109/gateway-security-preparation-20261110T0445"
)
OUTPUT_PARENT = Path("/var/backups/codex-remediation-20261110")
GUARD_SHA256 = "628bc3a62636e2b2a6d6795aa861d4f78975b683ffed8b5d7b8bded4c6e0c672"
OWNER_LABEL = "org.tmr.codex.gateway-recovery"
GIB = 1024**3
PEAK = 3 * GIB
MAX_SOURCE = GIB // 2
MAX_WORKING_DATA = GIB
FULL_ID = re.compile(r"[0-9a-f]{64}")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
CREDENTIAL_COLUMNS = {
    "users": ("id", "username", "password_hash"),
    "api_keys": ("id", "key_hash", "encrypted_key"),
    "providers": ("id", "api_key", "oauth_refresh_token"),
    "provider_oauth_accounts": ("id", "access_token", "refresh_token"),
}
PROBE = r"""
import json,urllib.error,urllib.request
from app.__version__ import __version__
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:3000/health',timeout=3) as r:
    assert r.status==200
    health=json.load(r)
assert isinstance(health,dict)
assert health.get('status') in ('healthy','degraded')
assert health.get('version')==__version__
assert {'dbPool','workers','circuitBreakers'} <= health.keys()
try:
    opener.open('http://127.0.0.1:3000/api/auth/me',timeout=3)
except urllib.error.HTTPError as e:
    assert e.code==401
else:
    raise AssertionError('Anonymous user endpoint did not deny access')
print(json.dumps({'version':__version__,'health':True,
                 'endpoint_status':health['status'],'anonymous_auth_denied':True}))
"""


class Refuse(RuntimeError):
    """Messages must never contain credentials or application records."""


def require(ok, message):
    if not ok:
        raise Refuse(message)


def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    # Private parent, fixed child names, atomic receipt replacement.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    os.replace(tmp, path)


def plain_directory(path, private=False):
    require(path.is_absolute(), "Expected absolute directory")
    require(path.resolve() == path, "Symlink or relative path rejected")
    info = path.stat()
    require(stat.S_ISDIR(info.st_mode), "Expected directory")
    if private:
        require(info.st_uid == 0 and info.st_mode & 0o077 == 0, "Directory must be root-only")


def regular(path):
    info = path.lstat()
    require(
        stat.S_ISREG(info.st_mode) and info.st_nlink == 1,
        "Only independent regular files supported",
    )
    return info


def manifest(directory):
    """Snapshot format is intentionally flat; reject links, sockets, subdirectories."""
    result = {}
    for path in sorted(directory.iterdir()):
        info = regular(path)
        require(not path.name.endswith(("-wal", "-shm")), "Snapshot contains live SQLite sidecars")
        result[path.name] = {"bytes": info.st_size, "sha256": digest(path)}
    require(result, "Empty snapshot")
    return result


def parse_environment(items):
    env = {}
    for item in items:
        require(isinstance(item, str) and "=" in item, "Invalid environment entry")
        key, value = item.split("=", 1)
        require(ENV_KEY.fullmatch(key) and key not in env, "Invalid or duplicate environment key")
        require(
            not any(c in value for c in "\r\n\0"), "Multiline environment needs separate review"
        )
        env[key] = value
    return env


def validate_snapshot(source):
    plain_directory(source, private=True)
    plain_directory(source / "data")
    for name in ("receipt.json", "container.private.json"):
        regular(source / name)
    receipt = json.loads((source / "receipt.json").read_text())
    container = json.loads((source / "container.private.json").read_text())
    require(
        receipt.get("phase") == "consistent_backup_and_independent_file_restore_verified",
        "Snapshot is not complete",
    )
    require(receipt.get("host") == "tmrwww01", "Snapshot belongs to another host")
    require(
        receipt.get("original_image") == ORIGINAL and container.get("Image") == ORIGINAL,
        "Snapshot image mismatch",
    )
    env = parse_environment(container["Config"]["Env"])
    prefix = "sqlite+aiosqlite:////app/data/"
    url = env.get("DATABASE_URL", "")
    require(url.startswith(prefix), "Only copied SQLite data is supported")
    active = url[len(prefix) :]
    require(
        active and Path(active).name == active and active.endswith(".db"),
        "Database path is not a plain .db file",
    )
    files = manifest(source / "data")
    require(
        sum(v["bytes"] for v in files.values()) <= MAX_SOURCE,
        "Snapshot exceeds reviewed growth budget",
    )
    databases = receipt.get("sqlite_files", {})
    require(active in databases, "Active database missing from verified receipt")
    require(
        {n for n in files if n.endswith(".db")} == set(databases), "Database inventory mismatch"
    )
    for name, entry in databases.items():
        require(
            entry.get("integrity") == "ok" and entry.get("independent_restore_verified") is True,
            "Unverified database in snapshot",
        )
        require(
            files.get(name) == {"bytes": entry["bytes"], "sha256": entry["sha256"]},
            "Snapshot database hash mismatch",
        )
    # These hashes and env are never exported in the sanitized result.
    metadata = {n: digest(source / n) for n in ("receipt.json", "container.private.json")}
    return env, active, files, metadata


def database_report(path, credentials=False, immutable=False):
    regular(path)
    uri = path.resolve().as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    with sqlite3.connect(uri, uri=True, timeout=20) as db:
        require(
            db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity failure"
        )
        rows = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        report = {
            "integrity": True,
            "tables": len(rows),
            "schema_sha256": hashlib.sha256(json.dumps(rows).encode()).hexdigest(),
        }
        if credentials:
            hashes = {}
            for table, columns in CREDENTIAL_COLUMNS.items():
                actual = {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}
                require(set(columns) <= actual, "Expected credential schema missing")
                fields = ",".join('"' + c + '"' for c in columns)
                records = db.execute(f'SELECT {fields} FROM "{table}" ORDER BY id').fetchall()
                hashes[table] = {
                    "rows": len(records),
                    "sha256": hashlib.sha256(
                        json.dumps(records, sort_keys=True).encode()
                    ).hexdigest(),
                }
            report["credential_fingerprints"] = hashes
    return report


def independent_copy(source, destination, expected):
    require(
        not destination.exists() and not destination.is_symlink(),
        "Restore destination already exists",
    )
    require(
        not destination.resolve().is_relative_to(source.resolve()),
        "Restore must be separate from snapshot",
    )
    destination.mkdir(mode=0o700)
    for name in expected:
        regular(source / name)
        shutil.copyfile(source / name, destination / name)
        (destination / name).chmod(0o600)
    require(
        manifest(destination) == expected and manifest(source) == expected,
        "Independent copy verification failed",
    )
    for name in expected:
        require(
            not os.path.samefile(source / name, destination / name), "Copy aliases source inode"
        )


def capacity(path, peak=0):
    usage = shutil.disk_usage(path)
    reserve = max(64 * GIB, math.ceil(usage.total * 0.15) + 2 * GIB)
    require(usage.free >= reserve + peak, "Insufficient shared maintenance reserve")


def guarded_start(cleanup=False):
    import fcntl

    require(os.geteuid() == 0, "Root required")
    require(socket.gethostname().split(".")[0] == "tmrwww01", "Primary-only rehearsal")
    parent = [
        os.fsdecode(p) for p in Path(f"/proc/{os.getppid()}/cmdline").read_bytes().split(b"\0") if p
    ]
    paths = [Path(p) for p in parent if Path(p).name == "capacity_guard.py"]
    require(
        len(paths) == 1 and paths[0].is_absolute(),
        "Run directly under absolute shared capacity guard",
    )
    require(digest(paths[0]) == GUARD_SHA256, "Capacity guard changed; review it first")
    require(
        "--peak-gib" in parent and float(parent[parent.index("--peak-gib") + 1]) >= 3,
        "Guard needs at least 3 GiB estimated peak",
    )
    shared = os.open("/run/codex-maintenance-capacity.lock", os.O_RDWR | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(shared, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise Refuse("Shared capacity lock is not held")
    finally:
        os.close(shared)
    owned = os.open(
        "/run/codex-gateway-recovery.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    fcntl.flock(owned, fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not cleanup:
        capacity(OUTPUT_PARENT, PEAK)
    return owned


def assert_isolated(container, owner, name, image, data, logs):
    require(FULL_ID.fullmatch(container["Id"]), "Docker returned a non-full ID")
    require(
        container["Name"] == "/" + name and container["Image"] == image,
        "Container identity mismatch",
    )
    require(
        container["Config"].get("Labels", {}).get(OWNER_LABEL) == owner,
        "Container ownership mismatch",
    )
    host = container["HostConfig"]
    require(
        host["NetworkMode"] == "none"
        and not host.get("PortBindings")
        and not any((container["NetworkSettings"].get("Ports") or {}).values()),
        "Container network isolation mismatch",
    )
    require(
        not host.get("Privileged") and host.get("ReadonlyRootfs") is True,
        "Container privilege/filesystem mismatch",
    )
    require(host.get("RestartPolicy", {}).get("Name") == "no", "Unexpected restart policy")
    require(container["Config"]["User"] == "1001:1001", "Unexpected container user")
    require(
        not host.get("PidMode") and host.get("IpcMode") == "private", "Shared namespace rejected"
    )
    require(
        host.get("Memory") == 2 * GIB
        and host.get("MemorySwap") == 2 * GIB
        and host.get("NanoCpus") == 2_000_000_000,
        "Unexpected resource limits",
    )
    require(
        host.get("PidsLimit") == 256 and "ALL" in host.get("CapDrop", []),
        "Unexpected process/capability limits",
    )
    require(
        "no-new-privileges" in host.get("SecurityOpt", []),
        "Missing privilege escalation protection",
    )
    binds = [m for m in container["Mounts"] if m["Type"] != "tmpfs"]
    expected = {"/app/data": str(data), "/app/logs": str(logs)}
    require(len(binds) == 2, "Unexpected mount count")
    for mount in binds:
        require(mount["Type"] == "bind" and mount.get("RW") is True, "Unexpected mount type/mode")
        require(expected.get(mount["Destination"]) == mount["Source"], "Unexpected bind source")
    require(set(host.get("Tmpfs", {})) == {"/tmp"}, "Unexpected tmpfs mounts")


class Docker:
    def __init__(self, private):
        self.private = private
        self.sequence = 0

    def command(self, args, timeout=45, allow_failure=False):
        self.sequence += 1
        log = self.private / f"command-{self.sequence:04d}.private.log"
        # No command string is logged: env-file path is safe, but protect future additions.
        with log.open("wb") as stream:
            result = subprocess.run(
                ["docker", *args],
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        require(
            allow_failure or result.returncode == 0, "Docker command failed; inspect private log"
        )
        return result.returncode, log.read_text(errors="replace")

    def inspect(self, identity):
        return json.loads(self.command(["inspect", identity])[1])[0]

    def inventory(self):
        # Read only identities/state, avoiding global raw inspect/env disclosure.
        ids = self.command(["ps", "-aq", "--no-trunc"])[1].split()
        result = {}
        for cid in ids:
            require(FULL_ID.fullmatch(cid), "Malformed inventory ID")
            fmt = "{{json .Id}} {{json .Image}} {{json .State.Running}} {{json .State.StartedAt}}"
            result[cid] = self.command(["inspect", "--format", fmt, cid])[1].strip()
        return result

    def optional_named(self, name):
        # Successful listing distinguishes absence from Docker daemon failure.
        ids = self.command(["ps", "-aq", "--no-trunc", "--filter", "name=^/" + name + "$"])[
            1
        ].split()
        require(len(ids) <= 1, "Ambiguous fixture name")
        return self.inspect(ids[0]) if ids else None


def create_arguments(name, owner, image, data, logs, envfile):
    return [
        "create",
        "--pull",
        "never",
        "--name",
        name,
        "--label",
        OWNER_LABEL + "=" + owner,
        "--network",
        "none",
        "--restart",
        "no",
        "--ipc",
        "private",
        "--user",
        "1001:1001",
        "--memory",
        "2g",
        "--memory-swap",
        "2g",
        "--cpus",
        "2",
        "--pids-limit",
        "256",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=268435456,mode=1777",
        "--log-driver",
        "json-file",
        "--log-opt",
        "max-size=1m",
        "--log-opt",
        "max-file=2",
        "--mount",
        f"type=bind,src={data},dst=/app/data",
        "--mount",
        f"type=bind,src={logs},dst=/app/logs",
        "--env-file",
        str(envfile),
        image,
    ]


def tree_bytes(root):
    total = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        require(
            all(not (Path(base) / d).is_symlink() for d in dirs), "Unexpected link in restored data"
        )
        for name in files:
            total += regular(Path(base) / name).st_size
    return total


class Rehearsal:
    def __init__(self, root, source=SNAPSHOT, docker=None):
        self.root, self.source = root, source
        self.owner = root.name
        self.docker = docker or Docker(root)
        self.state = {"phase": "preflight", "deployed": False, "containers": {}, "cycles": {}}
        self.started = time.monotonic()
        self.baseline_inventory = None
        self.source_files = None
        self.metadata = None

    def save(self, phase):
        self.state.update(
            phase=phase, updated_utc=datetime.datetime.now(datetime.timezone.utc).isoformat()
        )
        write_json(self.root / "receipt.private.json", self.state)

    def bounds(self):
        require(time.monotonic() - self.started < 1200, "Rehearsal time budget exceeded")
        capacity(self.root)
        for stage in ("candidate", "original-recovery"):
            path = self.root / stage
            if path.exists():
                require(tree_bytes(path) <= MAX_WORKING_DATA, "Restore grew beyond reviewed budget")

    def owned(self, stage):
        record = self.state["containers"][stage]
        current = self.docker.inspect(record["id"])
        assert_isolated(
            current,
            self.owner,
            record["name"],
            record["image"],
            self.root / stage / "data",
            self.root / stage / "logs",
        )
        require(current["Id"] == record["id"], "Container ID changed")
        return current

    def make_container(self, stage, image):
        name = self.owner + "-" + stage
        require(self.docker.optional_named(name) is None, "Fixture name already exists")
        self.state["containers"][stage] = {"name": name, "image": image, "create_intent": True}
        self.save("create_intent_" + stage)
        folder = self.root / stage
        try:
            _, text = self.docker.command(
                create_arguments(
                    name,
                    self.owner,
                    image,
                    folder / "data",
                    folder / "logs",
                    self.root / "environment.private.env",
                ),
                timeout=60,
            )
            require(FULL_ID.fullmatch(text.strip()), "Create did not return full container ID")
            self.state["containers"][stage]["id"] = text.strip()
            self.save("created_" + stage)
            self.owned(stage)
            passwd = folder / "passwd.image.private"
            self.docker.command(["cp", text.strip() + ":/etc/passwd", str(passwd)])
            regular(passwd)
            passwd.chmod(0o600)
            entries = [
                line.split(":")
                for line in passwd.read_text().splitlines()
                if line.startswith("appuser:")
            ]
            require(
                len(entries) == 1 and entries[0][2:4] == ["1001", "1001"],
                "Image appuser UID/GID not verified",
            )
            self.state["containers"][stage]["image_uid_gid_verified"] = True
        except BaseException:
            # CLI timeout can occur after Docker accepted create. Adopt only our exact identity.
            self.adopt_intent(stage)
            raise

    def adopt_intent(self, stage):
        record = self.state["containers"][stage]
        if "id" in record:
            return
        current = self.docker.optional_named(record["name"])
        if current:
            assert_isolated(
                current,
                self.owner,
                record["name"],
                record["image"],
                self.root / stage / "data",
                self.root / stage / "logs",
            )
            record["id"] = current["Id"]
            self.save("reconciled_create_" + stage)

    def stop(self, stage):
        record = self.state["containers"][stage]
        current = self.owned(stage)
        if current["State"]["Running"]:
            record["stop_intent"] = True
            self.save("stop_intent_" + stage)
            try:
                self.docker.command(["stop", "-t", "120", record["id"]], timeout=150)
            except (subprocess.TimeoutExpired, Refuse):
                # Do not mistake an ambiguous CLI outcome for a stopped container.
                current = self.owned(stage)
                if current["State"]["Running"]:
                    raise Refuse("Owned fixture did not stop; cleanup/recovery required") from None
            current = self.owned(stage)
        require(not current["State"]["Running"], "Fixture remains running")
        require(
            current["State"]["ExitCode"] == 0 and not current["State"].get("OOMKilled"),
            "Fixture did not shut down cleanly",
        )

    def ready(self, stage):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            self.bounds()
            current = self.owned(stage)
            require(
                current["State"]["Running"] and not current["State"].get("OOMKilled"),
                "Fixture exited during startup",
            )
            code, text = self.docker.command(
                ["exec", current["Id"], "python", "-c", PROBE], timeout=15, allow_failure=True
            )
            if code == 0:
                result = json.loads(text)
                require(
                    result.get("health") is True and result.get("anonymous_auth_denied") is True,
                    "Unexpected probe output",
                )
                require(
                    result.get("endpoint_status") in ("healthy", "degraded"),
                    "Unexpected endpoint status",
                )
                if stage == "candidate":
                    require(result["version"] == "5.22.54", "Unexpected candidate version")
                require(
                    re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", result["version"]),
                    "Unexpected version format",
                )
                return result
            time.sleep(2)
        raise Refuse("Offline startup deadline exceeded; inspect private logs")

    def cycle(self, stage, active, baseline_credentials):
        require(
            self.state["containers"][stage].get("image_uid_gid_verified") is True,
            "Image UID/GID not verified before start",
        )
        current = self.owned(stage)
        require(not current["State"]["Running"], "Duplicate start refused")
        self.save("start_intent_" + stage)
        self.docker.command(["start", current["Id"]], timeout=60)
        result = self.ready(stage)
        for _ in range(5):
            time.sleep(4)
            self.bounds()
            require(self.owned(stage)["State"]["Running"], "Fixture failed during short soak")
        self.stop(stage)
        report = database_report(self.root / stage / "data" / active, credentials=True)
        require(
            report["credential_fingerprints"] == baseline_credentials,
            "Credential subset changed; investigate privately",
        )
        result["database_private"] = report
        result["clean_stop"] = True
        self.state["cycles"].setdefault(stage, []).append(result)
        self.save("cycle_verified_" + stage)

    def cleanup(self):
        errors = []
        for stage, record in self.state["containers"].items():
            try:
                self.adopt_intent(stage)
                if "id" not in record:
                    continue
                ids = self.docker.command(["ps", "-aq", "--no-trunc"])[1].split()
                require(
                    all(FULL_ID.fullmatch(cid) for cid in ids), "Malformed inventory during cleanup"
                )
                if record["id"] not in ids:
                    # Daemon-backed absence also handles a crash immediately after rm succeeded.
                    record["removed"] = True
                    continue
                self.owned(stage)  # No mutation by name, only a verified owned full ID.
                try:
                    self.stop(stage)
                finally:
                    self.docker.command(
                        ["logs", "--tail", "20000", record["id"]], allow_failure=True
                    )
                    self.owned(stage)
                    self.docker.command(["rm", "-f", record["id"]], timeout=60)
                    record["removed"] = True
            except BaseException as exc:  # noqa: BLE001 - finish owned cleanup after interruption
                errors.append(type(exc).__name__)
        self.state["cleanup_errors"] = errors
        self.save("cleanup_complete" if not errors else "cleanup_incomplete")
        require(not errors, "Owned fixture cleanup incomplete; see private receipt")

    def verify_unchanged(self):
        if self.source_files is not None:
            require(
                manifest(self.source / "data") == self.source_files, "Retained snapshot changed"
            )
            require(
                {n: digest(self.source / n) for n in self.metadata} == self.metadata,
                "Snapshot metadata changed",
            )
        if self.baseline_inventory is not None:
            require(
                self.docker.inventory() == self.baseline_inventory,
                "Unrelated container identity/state changed",
            )

    def run(self):
        env, active, self.source_files, self.metadata = validate_snapshot(self.source)
        self.state["snapshot_private_manifest"] = self.source_files
        baseline = database_report(self.source / "data" / active, credentials=True, immutable=True)
        self.state["baseline_database_private"] = baseline
        self.baseline_inventory = self.docker.inventory()
        live = self.docker.command(
            ["inspect", "--format", "{{.Image}} {{.State.Running}}", "llm-proxy2"]
        )[1].strip()
        require(
            live == ORIGINAL + " true",
            "Production original identity/state changed before rehearsal",
        )
        for image in (ORIGINAL, CANDIDATE):
            info = json.loads(self.docker.command(["image", "inspect", image])[1])[0]
            require(
                info["Id"] == image and not info["Config"].get("Volumes"),
                "Image mismatch or implicit volumes",
            )
            require(info["Config"].get("User") == "appuser", "Unexpected image default user")
            require(info["Config"].get("Entrypoint") in (None, []), "Unexpected image entrypoint")
            require(
                info["Config"].get("Cmd")
                == [
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "3000",
                    "--workers",
                    "1",
                ],
                "Unexpected startup command",
            )
        env["PORT"] = "3000"
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        envfile = self.root / "environment.private.env"
        envfile.write_text("".join(k + "=" + v + "\n" for k, v in sorted(env.items())))
        envfile.chmod(0o600)
        for stage in ("candidate", "original-recovery"):
            folder = self.root / stage
            folder.mkdir(mode=0o700)
            independent_copy(self.source / "data", folder / "data", self.source_files)
            (folder / "logs").mkdir(mode=0o700)
            # Host parent stays root-only; container UID can access its two exact bind roots.
            for path in [folder / "data", folder / "logs", *(folder / "data").iterdir()]:
                os.chown(path, 1001, 1001)
        self.save("fresh_independent_copies_verified")
        try:
            self.make_container("candidate", CANDIDATE)
            self.cycle("candidate", active, baseline["credential_fingerprints"])
            self.cycle("candidate", active, baseline["credential_fingerprints"])
            self.make_container("original-recovery", ORIGINAL)
            self.cycle("original-recovery", active, baseline["credential_fingerprints"])
            schemas = [
                result["database_private"]["schema_sha256"]
                for cycles in self.state["cycles"].values()
                for result in cycles
            ]
            require(
                len(set(schemas)) == 1,
                "Candidate restart/original recovery schemas differ; review required",
            )
            for stage in ("candidate", "original-recovery"):
                for name in self.source_files:
                    if name.endswith(".db"):
                        database_report(self.root / stage / "data" / name)
            self.state["offline_checks_passed"] = True
        finally:
            try:
                self.cleanup()
            finally:
                self.verify_unchanged()
        capacity(self.root)
        self.save("offline_recovery_passed_not_deployed")
        return {
            "phase": self.state["phase"],
            "deployed": False,
            "candidate_image": CANDIDATE,
            "original_image": ORIGINAL,
            "candidate_clean_cycles": 2,
            "original_recovery_clean_cycles": 1,
            "fresh_copies_verified": True,
            "database_integrity_passed": True,
            "credential_subsets_preserved": True,
            "snapshot_unchanged": True,
            "candidate_restart_and_original_schema_equal": True,
            "offline_endpoint_statuses": {
                stage: [cycle["endpoint_status"] for cycle in cycles]
                for stage, cycles in self.state["cycles"].items()
            },
            "unrelated_container_identity_state_unchanged": True,
            "network_mode": "none",
            "published_ports": False,
            "provider_or_cluster_delivery_possible": False,
            "background_workers_disabled": False,
            "online_behavior_verified": False,
            "downgrade_on_candidate_migrated_database_tested": False,
            "production_rollout_approved": False,
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", required=True, help="Fresh lowercase identifier, e.g. 20261111t010000z"
    )
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Recover owned fixtures after interruption; never starts containers",
    )
    args = parser.parse_args()
    require(re.fullmatch(r"[a-z0-9]{8,32}", args.run_id), "Invalid run ID")
    os.umask(0o077)
    lock = guarded_start(cleanup=args.cleanup_only)
    root = OUTPUT_PARENT / ("gateway-offline-rehearsal-" + args.run_id)
    plain_directory(OUTPUT_PARENT)
    if args.cleanup_only:
        plain_directory(root, private=True)
    else:
        require(not root.exists() and not root.is_symlink(), "Run directory already exists")
        root.mkdir(mode=0o700)
    runner = Rehearsal(root)
    # A new process must not overwrite logs retained by an earlier interrupted run.
    runner.docker.sequence = max(
        (int(p.name.split("-")[1].split(".")[0]) for p in root.glob("command-*.private.log")),
        default=0,
    )
    if args.cleanup_only:
        regular(root / "receipt.private.json")
        runner.state = json.loads((root / "receipt.private.json").read_text())
        for stage, record in runner.state["containers"].items():
            require(stage in ("candidate", "original-recovery"), "Unexpected saved fixture stage")
            require(record["name"] == runner.owner + "-" + stage, "Saved fixture name mismatch")
            require(
                record["image"] == (CANDIDATE if stage == "candidate" else ORIGINAL),
                "Saved fixture image mismatch",
            )
            if "id" in record:
                require(FULL_ID.fullmatch(record["id"]), "Malformed saved ID")

    def interrupted(_signum, _frame):
        raise InterruptedError("Rehearsal interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(1200)
    try:
        if args.cleanup_only:
            runner.cleanup()
            result = {
                "phase": "cleanup_only_complete",
                "deployed": False,
                "rehearsal_pass_claimed": False,
            }
        else:
            result = runner.run()
    except BaseException as exc:  # noqa: BLE001 - sanitize all CLI errors, including interruption
        # Never echo subprocess output, environment, DB rows, arbitrary exceptions or tracebacks.
        runner.state["error_class"] = type(exc).__name__
        runner.save("failed_not_deployed")
        result = {
            "phase": "failed_not_deployed",
            "error_class": type(exc).__name__,
            "deployed": False,
        }
        write_json(
            root
            / ("cleanup-result.sanitized.json" if args.cleanup_only else "result.sanitized.json"),
            result,
        )
        print(json.dumps(result))
        return 1
    finally:
        signal.alarm(0)
        os.close(lock)
    write_json(
        root / ("cleanup-result.sanitized.json" if args.cleanup_only else "result.sanitized.json"),
        result,
    )
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
