"""Opt-in real PRE-CUTOVER Nginx validator acceptance; retain every artifact.

Run the reviewed, committed copy directly under the shared capacity guard on
tmrwww01. This does not exercise candidate routing, browser auth, deployment or
rollback, and does not set any production release gate. Only two named private
parents may be created root:root 0700; independent backup coverage is unverified.
Only the helper's exactly validated new fixture may be created/started/stopped.
There is deliberately no cleanup or recovery-by-name-prefix implementation.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import traceback
from pathlib import Path

import network_nginx as n
import release as r

BASE = Path("/mnt/s/backups/codex-private-security-20261109/nginx-validation")
GUARD = Path("/usr/local/lib/codex-remediation/retention/capacity_guard.py")
FRAGMENT = Path("grok_bridge/deployment/nginx.grok-bridge.conf")
DEPLOYMENT = FRAGMENT.parent
HEX = re.compile(r"[0-9a-f]{64}\Z")
MODULES = (
    "release",
    "network_nginx",
    "network_nginx_inputs",
    "network_docker",
    "network_policy",
    "network_compose",
    "network_transaction",
)
MAX_INPUT = 16 * 1024 * 1024
PEAK = (r.GIB + 9) // 10


class Refused(RuntimeError):
    """Public errors carry no command output or private configuration."""


def require(ok, message):
    if not ok:
        raise Refused(message)


def checksum(value):
    return hashlib.sha256(value).hexdigest()


def fingerprint(value):
    return checksum(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def no_symlinks(path):
    path = Path(path)
    require(path.is_absolute() and ".." not in path.parts, "Use an absolute canonical path")
    for item in (*reversed(path.parents), path):
        require(not item.is_symlink(), "Symlink path requires review")
    require(path.resolve() == path, "Path resolution changed")
    return path


def read_regular(path):
    path = no_symlinks(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode), "Input must be a regular file")
        require(info.st_size <= MAX_INPUT, "Input exceeds reviewed bound")
        data = stream.read(MAX_INPUT + 1)
        require(len(data) <= MAX_INPUT, "Input grew beyond reviewed bound")
    return data


def private_json(path, value):
    r.private_write(path, (json.dumps(value, sort_keys=True, indent=2) + "\n").encode())


def require_platform():
    require(sys.platform == "linux" and os.geteuid() == 0, "Linux root is required")
    parent = Path(f"/proc/{os.getppid()}/cmdline").read_bytes().split(b"\0")
    require(
        os.fsencode(GUARD) in parent,
        "Use the exact shared capacity guard as direct parent",
    )


def validate_attempt(path):
    path = no_symlinks(path)
    require(path.parent == BASE, "Attempt must be a direct child of the private Nginx root")
    require(
        re.fullmatch(r"nginx-[a-z0-9][a-z0-9-]{0,47}", path.name),
        "Invalid attempt name",
    )
    require(not path.exists(), "Attempt must be fresh; retain previous attempts")
    for parent_path in (BASE.parent, BASE):
        if parent_path.exists():
            private_directory(parent_path)
    nas = Path("/mnt/s")
    existing = BASE.parent.parent.stat()
    require(
        os.path.ismount(nas)
        and nas.stat().st_dev != Path("/").stat().st_dev
        and existing.st_dev == nas.stat().st_dev,
        "Private evidence must reside on the separately mounted NAS",
    )
    r.capacity(BASE.parent.parent, peak=PEAK)
    r.capacity(r.ROOT, peak=PEAK)


def private_directory(path):
    info = no_symlinks(path).stat()
    require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == 0
        and info.st_gid == 0
        and stat.S_IMODE(info.st_mode) == 0o700,
        "Private directory must be root:root 0700",
    )


def create_attempt(path):
    # Explicit named parents only. Never chmod/chown or recursively mkdir the
    # shared /mnt/s/backups tree. A concurrent creator forces revalidation.
    for parent in (BASE.parent, BASE):
        if not parent.exists():
            parent.mkdir(mode=0o700)
        private_directory(parent)
        require(
            parent.stat().st_dev == Path("/mnt/s").stat().st_dev,
            "Private parent moved off NAS",
        )
    path.mkdir(mode=0o700)
    private_directory(path)


def git_read(source, *args):
    result = subprocess.run(
        ["git", "-c", "safe.directory=" + str(source), "-C", str(source), *args],
        capture_output=True,
        check=False,
        timeout=30,
    )
    require(result.returncode == 0, "Cannot verify maintained source")
    return result.stdout


def source_identity(source, expected_commit, fragment):
    """Verify exact reviewed HEAD, clean checkout, module origin and committed bytes."""
    source, fragment = no_symlinks(source), no_symlinks(fragment)
    require(re.fullmatch(r"[0-9a-f]{40}", expected_commit), "Exact reviewed commit required")
    require(fragment == source / FRAGMENT, "Use the maintained fragment from this checkout")
    require(
        git_read(source, "rev-parse", "HEAD").decode().strip() == expected_commit,
        "Maintained source commit differs",
    )
    require(
        git_read(source, "rev-parse", "--show-toplevel").decode().strip() == str(source),
        "Source must be the repository root",
    )
    require(
        not git_read(source, "status", "--porcelain=v1", "--untracked-files=all"),
        "Maintained checkout must be clean",
    )
    paths = [FRAGMENT, DEPLOYMENT / "rehearse_nginx.py"]
    require(Path(__file__).resolve() == source / paths[-1], "Execute the maintained runner")
    for name in MODULES:
        relative = DEPLOYMENT / (name + ".py")
        require(
            Path(sys.modules[name].__file__).resolve() == source / relative,
            "Imported operator module is outside reviewed source",
        )
        paths.append(relative)
    hashes = {}
    for relative in paths:
        actual = read_regular(source / relative)
        require(
            actual == git_read(source, "show", expected_commit + ":" + relative.as_posix()),
            "Maintained source bytes differ from commit",
        )
        hashes[relative.as_posix()] = checksum(actual)
    require(
        type(r.NETWORK_RELEASE_SCHEMA) is int and r.NETWORK_RELEASE_SCHEMA == 1,
        "Operator network schema requires review",
    )
    return {"commit": expected_commit, "sha256": hashes}


def container_fingerprint(value):
    state = value["State"]
    # Health probe logs/timestamps naturally advance; lifecycle and health status
    # may not change. Full raw State is also retained in the private inventory.
    lifecycle = {
        key: state.get(key)
        for key in (
            "Status",
            "Running",
            "Paused",
            "Restarting",
            "OOMKilled",
            "Dead",
            "Pid",
            "ExitCode",
            "Error",
            "StartedAt",
            "FinishedAt",
        )
    }
    lifecycle["HealthStatus"] = state.get("Health", {}).get("Status")
    return {
        "name": value["Name"],
        "config_sha256": fingerprint(
            {
                key: value.get(key)
                for key in (
                    "Id",
                    "Name",
                    "Created",
                    "Image",
                    "Config",
                    "HostConfig",
                    "Mounts",
                    "Path",
                    "Args",
                )
            }
        ),
        "network_sha256": fingerprint(value["NetworkSettings"]),
        "lifecycle": lifecycle,
        "restart_count": value.get("RestartCount"),
    }


def list_ids(host, kind):
    result = host.run(
        [
            "docker",
            kind,
            "ls",
            "--no-trunc",
            *(["--all"] if kind == "container" else []),
            "--format",
            "{{.ID}}",
        ]
    ).split()
    require(
        len(result) <= 4096 and len(result) == len(set(result)),
        "Ambiguous Docker inventory",
    )
    require(all(HEX.fullmatch(item) for item in result), "Inventory requires immutable IDs")
    return sorted(result)


def inventory(host):
    cids, nids = list_ids(host, "container"), list_ids(host, "network")
    result = {"containers": {}, "networks": {}}
    for kind, ids in (("containers", cids), ("networks", nids)):
        prefix = ["docker", "inspect"] if kind == "containers" else ["docker", "network", "inspect"]
        for offset in range(0, len(ids), 64):
            batch = ids[offset : offset + 64]
            rows = json.loads(host.run([*prefix, *batch]))
            require(
                type(rows) is list and len(rows) == len(batch),
                "Incomplete Docker inspection",
            )
            require(
                sorted(row["Id"] for row in rows) == batch,
                "Inspection identity mismatch",
            )
            for row in rows:
                result[kind][row["Id"]] = row
    require(
        cids == list_ids(host, "container") and nids == list_ids(host, "network"),
        "Docker inventory changed during capture",
    )
    return result


class GuardedHost(r.Host):
    """Use the real validator, with a second narrow command and identity boundary."""

    def __init__(self, folder):
        super().__init__(folder)
        self.baseline_ids = None
        self.create_calls = 0
        self.start_calls = 0

    def run(self, args, timeout=180):
        require(
            args and args[0] == "docker",
            "Only bounded local Docker operations are allowed",
        )
        tail = args[1:]
        readonly = tail[:2] in (
            ["container", "ls"],
            ["network", "ls"],
            ["network", "inspect"],
            ["image", "inspect"],
        ) or tail[:1] == ["inspect"]
        if not readonly:
            require(self.baseline_ids is not None, "Private baseline must precede mutation")
            state = n._load(self.folder)
            require(state is not None, "Exact helper ownership ledger is required")
            if tail[:1] == ["create"]:
                require(
                    self.create_calls == 0
                    and args == n._create_args(state)
                    and state.get("id") is None,
                    "Only one exact helper fixture may be created",
                )
                self.create_calls += 1
            elif tail[:1] in (["start"], ["stop"]):
                cid = state.get("id")
                require(
                    type(cid) is str and HEX.fullmatch(cid) and cid not in self.baseline_ids,
                    "Cannot mutate a preexisting or ambiguous container",
                )
                require(
                    args
                    in (
                        ["docker", "start", "-a", cid],
                        ["docker", "stop", "--time", "5", cid],
                    ),
                    "Only exact retained fixture lifecycle commands are allowed",
                )
                n._fixture(self.inspect(cid), state)
                if tail[0] == "start":
                    require(self.start_calls < 2, "Only two validator runs are allowed")
                    self.start_calls += 1
            else:
                raise Refused("Docker mutation is outside validator acceptance")
        return super().run(args, timeout=timeout)


def invariant_audit(before, after, files, owned_id=None):
    for cid, original in before["containers"].items():
        require(cid in after["containers"], "Preexisting container disappeared")
        require(
            container_fingerprint(after["containers"][cid]) == container_fingerprint(original),
            "Preexisting container configuration, endpoints or lifecycle changed",
        )
    networks = copy.deepcopy(after["networks"])
    if owned_id is not None:
        for network in networks.values():
            if owned_id in network.get("Containers", {}):
                require(
                    network.get("Name") == n.DEFAULT,
                    "Owned validator appeared on an unexpected network",
                )
                del network["Containers"][owned_id]
    require(
        fingerprint(before["networks"]) == fingerprint(networks),
        "Preexisting networks or endpoints changed",
    )
    for path, data in files.items():
        require(read_regular(path) == data, "Canonical or staged file changed")


def owned_receipt(folder, current, before_ids):
    state = n._load(folder)
    if state is None:
        return None
    matches = [
        row
        for cid, row in current["containers"].items()
        if cid not in before_ids and row.get("Name") == "/" + state["name"]
    ]
    require(len(matches) <= 1, "Owned fixture lookup is ambiguous")
    if not matches:
        require(state.get("id") is None, "Recorded owned fixture disappeared")
        return None
    row = matches[0]
    cid = n._fixture(row, state)
    return {
        "name": state["name"],
        "id": cid,
        "state": "running" if row["State"].get("Running") else "stopped",
        "exit_code": row["State"].get("ExitCode"),
        "oom_killed": bool(row["State"].get("OOMKilled")),
        "configuration_verified": True,
    }


def fixture_observation(folder, current, before_ids):
    """Informational only: never grants a lifecycle command or endpoint exemption.

    A failed full configuration check can still leave an exact recorded fixture.
    Recognize it only by its persisted full ID, expected name and owner label.
    No lookup/adoption by name, prefix, partial ID or unverified configuration.
    """
    state = n._load(folder)
    if state is None:
        return None
    cid = state.get("id")
    if type(cid) is not str or not HEX.fullmatch(cid) or cid in before_ids:
        return None
    row = current["containers"].get(cid)
    if type(row) is not dict or row.get("Id") != cid or row.get("Name") != "/" + state["name"]:
        return None
    config = row.get("Config")
    labels = config.get("Labels") if type(config) is dict else None
    if type(labels) is not dict or labels.get(n.OWNER) != state["transaction"]:
        return None
    runtime = row.get("State")
    runtime = runtime if type(runtime) is dict else {}
    running, exit_code, oom = (
        runtime.get("Running"),
        runtime.get("ExitCode"),
        runtime.get("OOMKilled"),
    )
    return {
        "name": state["name"],
        "id": cid,
        "state": {True: "running", False: "stopped"}.get(running, "unknown")
        if type(running) is bool
        else "unknown",
        "exit_code": exit_code if type(exit_code) is int else None,
        "oom_killed": oom if type(oom) is bool else None,
        "configuration_verified": False,
        "observation_only": True,
    }


def preexisting_container_audit(before, after):
    """Check every original even when a previous container is missing or changed."""
    counts = {
        "expected": len(before),
        "checked": 0,
        "missing": 0,
        "configuration_changed": 0,
        "endpoints_changed": 0,
        "lifecycle_changed": 0,
        "unreadable": 0,
    }
    for cid, original in before.items():
        counts["checked"] += 1
        if cid not in after:
            counts["missing"] += 1
            continue
        try:
            old, new = (
                container_fingerprint(original),
                container_fingerprint(after[cid]),
            )
            counts["configuration_changed"] += old["config_sha256"] != new["config_sha256"]
            counts["endpoints_changed"] += old["network_sha256"] != new["network_sha256"]
            counts["lifecycle_changed"] += (
                old["lifecycle"] != new["lifecycle"] or old["restart_count"] != new["restart_count"]
            )
        except Exception:  # noqa: BLE001 -- malformed inspection must not skip other originals.
            counts["unreadable"] += 1
    counts["status"] = (
        "failed"
        if any(
            counts[key]
            for key in (
                "missing",
                "configuration_changed",
                "endpoints_changed",
                "lifecycle_changed",
                "unreadable",
            )
        )
        else "passed"
    )
    return counts


def preexisting_network_audit(before, after, validated_owned_id=None):
    """Only a fully validated fixture may account for an added default endpoint."""
    counts = {
        "expected": len(before),
        "checked": 0,
        "missing": 0,
        "changed": 0,
        "unreadable": 0,
        "added": len(set(after) - set(before)),
    }
    for nid, original in before.items():
        counts["checked"] += 1
        if nid not in after:
            counts["missing"] += 1
            continue
        try:
            value = copy.deepcopy(after[nid])
            if validated_owned_id is not None and validated_owned_id in value.get("Containers", {}):
                require(value.get("Name") == n.DEFAULT, "Validator endpoint requires review")
                del value["Containers"][validated_owned_id]
            counts["changed"] += fingerprint(original) != fingerprint(value)
        except Exception:  # noqa: BLE001 -- continue every original network on malformed data.
            counts["unreadable"] += 1
    counts["status"] = (
        "failed"
        if any(counts[key] for key in ("missing", "changed", "unreadable", "added"))
        else "passed"
    )
    return counts


def input_audit(files):
    counts = {"expected": len(files), "checked": 0, "changed": 0, "unreadable": 0}
    for path, data in files.items():
        counts["checked"] += 1
        try:
            counts["changed"] += read_regular(path) != data
        except Exception:  # noqa: BLE001 -- an unreadable input must not hide a second edit.
            counts["unreadable"] += 1
    counts["status"] = (
        "unverified"
        if not files
        else ("failed" if counts["changed"] or counts["unreadable"] else "passed")
    )
    return counts


def record_audit_error(result, folder, label, filename, exc):
    result["errors"].append(label + ":" + type(exc).__name__)
    try:
        r.private_write(folder / filename, traceback.format_exc().encode())
    except BaseException as write_error:  # noqa: BLE001 -- evidence failure cannot suppress audits.
        result["errors"].append("EvidenceWriteAudit:" + type(write_error).__name__)


def final_audits(host, result, before, files, source, verify_source):
    """Audit categories independently; ownership failure never skips originals."""
    status = result["audit_status"]
    current, validated_owned_id = None, None
    if before is not None:
        try:
            current = inventory(host)
            status["inventory_capture"] = "passed"
        except BaseException as exc:  # noqa: BLE001
            status["inventory_capture"] = "failed"
            record_audit_error(
                result,
                host.folder,
                "InventoryAudit",
                "audit-inventory.private.log",
                exc,
            )
        if current is not None:
            try:
                private_json(host.folder / "inventory-after.private.json", current)
                status["inventory_evidence"] = "passed"
            except BaseException as exc:  # noqa: BLE001
                status["inventory_evidence"] = "failed"
                record_audit_error(
                    result,
                    host.folder,
                    "InventoryEvidenceAudit",
                    "audit-inventory-evidence.private.log",
                    exc,
                )
            try:
                receipt = owned_receipt(host.folder, current, set(before["containers"]))
                result["fixture"] = receipt
                # This ID comes only from the full _fixture check, never the
                # weaker observation below. Other ownership requirements may fail.
                validated_owned_id = receipt["id"] if receipt else None
                added = set(current["containers"]) - set(before["containers"])
                require(
                    added == ({validated_owned_id} if receipt else set()),
                    "Unexpected new container",
                )
                if receipt:
                    require(receipt["state"] == "stopped", "Owned validator remains running")
                    if result.get("runs_returned_same_id"):
                        require(
                            type(receipt["exit_code"]) is int
                            and receipt["exit_code"] == 0
                            and not receipt["oom_killed"],
                            "Retained successful validator has an unclean exit",
                        )
                status["ownership"] = "passed"
            except BaseException as exc:  # noqa: BLE001
                status["ownership"] = "failed"
                record_audit_error(
                    result,
                    host.folder,
                    "OwnershipAudit",
                    "audit-ownership.private.log",
                    exc,
                )
            if result["fixture"] is None:
                try:
                    result["fixture_observation"] = fixture_observation(
                        host.folder, current, set(before["containers"])
                    )
                except BaseException as exc:  # noqa: BLE001
                    record_audit_error(
                        result,
                        host.folder,
                        "ObservationAudit",
                        "audit-observation.private.log",
                        exc,
                    )
            for kind, function, args in (
                (
                    "containers",
                    preexisting_container_audit,
                    (before["containers"], current["containers"]),
                ),
                (
                    "networks",
                    preexisting_network_audit,
                    (before["networks"], current["networks"], validated_owned_id),
                ),
            ):
                try:
                    audit = function(*args)
                    result["preexisting_audit"][kind] = audit
                    status["preexisting_" + kind] = audit["status"]
                    require(
                        audit["status"] == "passed",
                        "Preexisting " + kind + " changed or unreadable",
                    )
                except BaseException as exc:  # noqa: BLE001
                    status["preexisting_" + kind] = "failed"
                    record_audit_error(
                        result,
                        host.folder,
                        kind.title() + "Audit",
                        "audit-" + kind + ".private.log",
                        exc,
                    )
    # These checks do not depend on final inventory capture, fixture validation,
    # each other, or successful persistence of an earlier audit traceback.
    if before is not None:
        try:
            originals = [x for x in before["containers"].values() if x.get("Name") == "/nginx"]
            require(len(originals) == 1, "Original Nginx input binding is ambiguous")
            n.verify_validation_inputs(host.folder, originals[0])
            status["include_inputs"] = "passed"
        except BaseException as exc:  # noqa: BLE001
            status["include_inputs"] = "failed"
            record_audit_error(
                result,
                host.folder,
                "IncludeInputAudit",
                "audit-include-input.private.log",
                exc,
            )
    try:
        audit = input_audit(files)
        result["input_audit"] = audit
        status["inputs"] = audit["status"]
        require(audit["status"] != "failed", "Inputs changed or unreadable")
    except BaseException as exc:  # noqa: BLE001
        status["inputs"] = "failed"
        record_audit_error(result, host.folder, "InputAudit", "audit-input.private.log", exc)
    if source is not None:
        try:
            require(verify_source() == source, "Source changed during acceptance")
            status["source"] = "passed"
        except BaseException as exc:  # noqa: BLE001
            status["source"] = "failed"
            record_audit_error(result, host.folder, "SourceAudit", "audit-source.private.log", exc)
    if before is not None and all(value == "passed" for value in status.values()):
        result["invariants"] = "passed"
    if any("Audit:" in error for error in result["errors"]):
        result["invariants"] = "failed"


def exercise(host, fragment, verify_source):
    """Caller holds both guards and owns a fresh private directory. No cleanup."""
    result = {
        "acceptance": "failed",
        "scope": "pre-cutover retained Nginx validator only",
        "errors": [],
        "fixture": None,
        "fixture_observation": None,
        "invariants": "unverified",
        "audit_status": {
            key: "unverified"
            for key in (
                "inventory_capture",
                "inventory_evidence",
                "ownership",
                "preexisting_containers",
                "preexisting_networks",
                "inputs",
                "include_inputs",
                "source",
            )
        },
        "preexisting_audit": {"containers": None, "networks": None},
        "input_audit": None,
        "reviewed_source": None,
        "preexisting_counts": None,
        "input_sha256": {},
    }
    before, files, source = None, {}, None
    try:
        source = verify_source()
        result["reviewed_source"] = copy.deepcopy(source)
        before = inventory(host)
        result["preexisting_counts"] = {key: len(value) for key, value in before.items()}
        private_json(host.folder / "inventory-before.private.json", before)
        host.baseline_ids = set(before["containers"])
        nginx, bridge = host.inspect("nginx"), host.inspect(r.SERVICE)
        for original in (nginx, bridge):
            require(
                original["Id"] in before["containers"]
                and container_fingerprint(original)
                == container_fingerprint(before["containers"][original["Id"]]),
                "Original runtime changed",
            )
        compiled = [m for m in nginx["Mounts"] if m["Destination"] == "/etc/nginx/nginx.conf"]
        require(
            len(compiled) == 1 and compiled[0]["Type"] == "bind",
            "Review Nginx config mount",
        )
        canonical = r.ROOT / "config/nginx/projects-locations.d/grok-bridge.conf"
        compose = r.ROOT / "docker-compose.yml"
        config = Path(compiled[0]["Source"])
        for path in (canonical, compose, config, fragment):
            files[path] = read_regular(path)
        staged = host.folder / "nginx.after"
        content = r.render_nginx(files[config], files[fragment])
        r.private_write(staged, content)
        files[staged] = content
        result["input_sha256"] = {
            label: checksum(files[path])
            for label, path in {
                "canonical_fragment": canonical,
                "canonical_compose": compose,
                "compiled_nginx": config,
                "maintained_fragment": fragment,
                "staged_nginx": staged,
            }.items()
        }
        private_json(
            host.folder / "inputs.private.json",
            {
                "source": source,
                "file_sha256": {str(p): checksum(v) for p, v in files.items()},
                "nginx": nginx,
                "grok": bridge,
                "fingerprints": {
                    cid: container_fingerprint(row) for cid, row in before["containers"].items()
                },
            },
        )
        invariant_audit(before, inventory(host), files)
        require(verify_source() == source, "Source changed before validation")
        first = host.validate_nginx(staged, nginx, bridge)
        middle = inventory(host)
        receipt = owned_receipt(host.folder, middle, set(before["containers"]))
        invariant_audit(before, middle, files, receipt["id"] if receipt else None)
        require(
            receipt is not None and receipt["id"] == first and receipt["state"] == "stopped",
            "First validation did not retain its exact stopped fixture",
        )
        require(
            set(middle["containers"]) - set(before["containers"]) == {first},
            "Unexpected new container during validation",
        )
        require(verify_source() == source, "Source changed between validations")
        second = host.validate_nginx(staged, nginx, bridge)
        require(first == second, "Validator did not reuse its exact fixture")
        result["runs_returned_same_id"] = True
    except BaseException as exc:  # noqa: BLE001 -- audit even on interruption; diagnostics private.
        result["errors"].append(type(exc).__name__)
        try:
            r.private_write(host.folder / "failure.private.log", traceback.format_exc().encode())
        except BaseException as write_error:  # noqa: BLE001 -- continue the independent final audit.
            result["errors"].append("EvidenceWriteAudit:" + type(write_error).__name__)
    finally:
        final_audits(host, result, before, files, source, verify_source)
        if not result["errors"] and result.get("runs_returned_same_id") and result["fixture"]:
            result["acceptance"] = "passed"
        private_json(host.folder / "result.json", result)
    return result


def run(args):
    require_platform()

    def verify():
        return source_identity(args.source, args.expected_commit, args.fragment)

    verify()
    with r.operation_lock():
        validate_attempt(args.attempt)
        old_umask = os.umask(0o077)
        try:
            create_attempt(args.attempt)
            return exercise(GuardedHost(args.attempt), args.fragment, verify)
        finally:
            os.umask(old_umask)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--fragment", required=True, type=Path)
    parser.add_argument("--attempt", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except BaseException as exc:  # noqa: BLE001 -- never emit raw traceback/configuration.
        print(json.dumps({"acceptance": "refused", "error_class": type(exc).__name__}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0 if result["acceptance"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
