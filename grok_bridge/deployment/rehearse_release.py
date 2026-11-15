"""Exercise maintained Grok release functions against an owned, isolated Docker stack."""

import argparse
import ast
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import yaml

OWNER = "com.codex.grok-release-rehearsal"
NGINX_IMAGE = "sha256:67a10554437ff2017327a3ebb36c8a779f38a7f67f861980109c1cd81e133c4a"
CREATED_ROOTS = set()
LEGACY_TEST_ROOT = Path("/tmp/codex-remediate-20261109")
NAS_TEST_ROOT = Path("/mnt/s/backups/codex-security-20261109")


def command(args, timeout=240):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def inspect(name):
    return json.loads(command(["docker", "inspect", name]))[0]


def inventory():
    ids = command(["docker", "ps", "-aq"]).split()
    values = json.loads(command(["docker", "inspect", *ids])) if ids else []
    return {
        x["Id"]: {
            "image": x["Image"],
            "name": x["Name"],
            "state": x["State"]["Status"],
            "config_hash": hashlib.sha256(
                json.dumps(x["Config"], sort_keys=True).encode()
            ).hexdigest(),
            "host_hash": hashlib.sha256(
                json.dumps(x["HostConfig"], sort_keys=True).encode()
            ).hexdigest(),
            "mounts": x["Mounts"],
        }
        for x in values
    }


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_attempt_root(root):
    assert root == root.resolve(), "Use a resolved attempt directory"
    assert not root.exists() and not root.is_symlink(), (
        "Inspect prior rehearsal; never reuse its data"
    )
    assert root.parent == LEGACY_TEST_ROOT or (
        root.parent == NAS_TEST_ROOT
        and re.fullmatch(r"grok-final-lifecycle-[a-z0-9][a-z0-9-]{0,29}", root.name)
    ), "Use a direct child of the legacy test root or a named NAS lifecycle attempt"


def require_supported_operator(source):
    """Refuse the old two-service harness before any host or filesystem mutation."""
    operator = Path(source) / "grok_bridge/deployment/release.py"
    tree = ast.parse(operator.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "NETWORK_RELEASE_SCHEMA":
            raise RuntimeError(
                "Legacy two-service rehearsal cannot validate the network release operator; "
                "a reviewed network-aware retention-preserving rehearsal is required"
            )


def run(root, source):
    require_supported_operator(source)
    assert os.geteuid() == 0
    assert b"capacity_guard.py" in Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
    validate_attempt_root(root)
    if root.parent == NAS_TEST_ROOT:
        nas = Path("/mnt/s")
        assert os.path.ismount(nas) and nas.stat().st_dev != Path("/").stat().st_dev, (
            "NAS is not mounted separately; refusing new root-filesystem test data"
        )
    os.umask(0o077)
    root.mkdir(mode=0o700)
    assert root.stat().st_uid == 0 and root.stat().st_mode & 0o777 == 0o700, (
        "Attempt directory must be root-only before private inputs are saved"
    )
    CREATED_ROOTS.add(root)
    operator = source / "grok_bridge/deployment/release.py"
    git = ["git", "-c", "safe.directory=" + str(source), "-C", str(source)]
    baseline = inventory()
    operator_sha = sha(operator)
    source_commit = command([*git, "rev-parse", "HEAD"]).strip()
    spec = importlib.util.spec_from_file_location("maintained_grok_release", operator)
    r = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(r)
    r.ROOT = root
    result = {
        "phase": "starting",
        "operator_sha256": operator_sha,
        "harness_sha256": sha(__file__),
        "source_commit": source_commit,
        "production_deployed": False,
        "production_data_used": False,
        "provider_network_used": False,
        "original_image": r.ORIGINAL,
        "candidate_image": r.CANDIDATE,
        "nginx_image": NGINX_IMAGE,
        "scenarios": [],
    }

    def save(phase):
        result.update(
            phase=phase,
            updated_utc=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        )
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")

    with r.operation_lock():
        for scenario in ("roundtrip", "missing-after-create-failure"):
            case = root / scenario
            case.mkdir(mode=0o700)
            name = root.name + ("-a" if scenario == "roundtrip" else "-b")
            service, nginx, network, volume = (
                name + "-bridge",
                name + "-nginx",
                name + "-net",
                name + "-data",
            )
            r.ROOT, r.SERVICE = case, service
            tx = case / "transaction"
            tx.mkdir(mode=0o700)
            fragment_dir = case / "config/nginx/projects-locations.d"
            fragment_dir.mkdir(parents=True)
            nginx_config = case / "config/nginx/nginx.conf"
            logdir = case / "nginx-logs"
            logdir.mkdir()
            canonical_fragment = fragment_dir / "grok-bridge.conf"
            canonical_fragment.write_text(
                "location = /grok-bridge { return 200 'original fixture'; }\n"
            )
            nginx_config.write_text(
                "worker_processes 1;\npid /tmp/nginx.pid;\nevents {}\nhttp {\n"
                " access_log off;\n resolver 127.0.0.11 ipv6=off;\n server {\n  listen 8080;\n  "
                + r.MARKER
                + "\n  "
                + canonical_fragment.read_text()
                + "  # ── from projects-locations.d/fixture-health.conf ──\n"
                "  location = /fixture-health { return 200 'ok'; }\n }\n}\n"
            )
            proposal = case / "proposed-grok.conf"
            proposal.write_text(
                (source / "grok_bridge/deployment/nginx.grok-bridge.conf")
                .read_text()
                .replace("llm-proxy2-grok-bridge", service)
            )
            project = name.replace("-", "")
            health = "import urllib.request; assert urllib.request.urlopen('http://127.0.0.1:8443/healthz',timeout=3).status==200"
            config = {
                "services": {
                    service: {
                        "container_name": service,
                        "image": r.ORIGINAL,
                        "labels": {OWNER: name},
                        "restart": "no",
                        "mem_limit": "2g",
                        "cpus": 2,
                        "shm_size": "512m",
                        "environment": {
                            "BRIDGE_TOKEN": "synthetic-fixture-token",
                            "BRIDGE_PUBLIC_PATH": "/grok-bridge",
                        },
                        "volumes": ["profile:/data"],
                        "networks": ["isolated"],
                        "healthcheck": {
                            "test": ["CMD", "python", "-c", health],
                            "interval": "5s",
                            "timeout": "5s",
                            "retries": 36,
                            "start_period": "30s",
                        },
                    },
                    nginx: {
                        "container_name": nginx,
                        "image": NGINX_IMAGE,
                        "labels": {OWNER: name},
                        "restart": "no",
                        "mem_limit": "128m",
                        "networks": ["isolated"],
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(nginx_config),
                                "target": "/etc/nginx/nginx.conf",
                                "read_only": True,
                            },
                            {
                                "type": "bind",
                                "source": str(logdir),
                                "target": "/var/log/nginx",
                            },
                        ],
                    },
                },
                "networks": {"isolated": {"external": True, "name": network}},
                "volumes": {"profile": {"external": True, "name": volume}},
            }
            compose = case / "docker-compose.yml"
            compose.write_text(yaml.safe_dump(config, sort_keys=False))
            networks_created = []
            volumes_created = []
            record = {"scenario": scenario, "passed": False}
            result["scenarios"].append(record)

            class FixtureHost(r.Host):
                fault_injected = False

                def __init__(self, folder, settings):
                    super().__init__(folder)
                    self.settings = settings

                def inspect(self, target):
                    return super().inspect(self.settings["nginx"] if target == "nginx" else target)

                def run(self, args, timeout=180):
                    if args[:2] == ["docker", "create"]:
                        assert (
                            "-p" not in args
                            and "--publish" not in args
                            and "--publish-all" not in args
                        )
                        assert args[args.index("--network") + 1] == self.settings["network"]
                        args = (
                            args[:2] + ["--label", OWNER + "=" + self.settings["name"]] + args[2:]
                        )
                    if args[:2] in (
                        ["docker", "stop"],
                        ["docker", "start"],
                        ["docker", "rm"],
                    ):
                        target = inspect(args[-1])
                        assert target["Config"]["Labels"].get(OWNER) == self.settings["name"], (
                            "Mutation target is not owned"
                        )
                    if args[:2] == ["docker", "exec"]:
                        target = inspect(args[2])
                        assert target["Config"]["Labels"].get(OWNER) == self.settings["name"], (
                            "Exec target is not owned"
                        )
                    return super().run(args, timeout=timeout)

                def compose(self, path, selected_project, *operation):
                    assert Path(path).resolve().is_relative_to(self.settings["case"].resolve())
                    assert selected_project == self.settings["project"]
                    if operation and operation[0] == "up":
                        assert (
                            operation[-1] == self.settings["service"] and "--no-deps" in operation
                        )
                        if (
                            self.settings["scenario"] == "missing-after-create-failure"
                            and path == self.settings["compose"]
                            and not self.fault_injected
                        ):
                            old = self.inspect(self.settings["service"])
                            assert (
                                old["Id"] == self.settings["original_id"]
                                and not old["State"]["Running"]
                            )
                            assert old["Config"]["Labels"].get(OWNER) == self.settings["name"]
                            self.run(["docker", "rm", old["Id"]])
                            self.fault_injected = True
                            raise r.Refused("Injected failure after owned original removal")
                    return super().compose(path, selected_project, *operation)

            host = FixtureHost(
                tx,
                {
                    "case": case,
                    "nginx": nginx,
                    "network": network,
                    "name": name,
                    "project": project,
                    "service": service,
                    "scenario": scenario,
                    "compose": compose,
                },
            )
            # Refuse leftovers before entering cleanup: never adopt prior resources.
            assert not command(
                ["docker", "ps", "-aq", "--filter", "label=" + OWNER + "=" + name]
            ).strip(), "Owner label already exists"
            for target in (service, nginx):
                assert not command(
                    ["docker", "ps", "-aq", "--filter", "name=^/" + target + "$"]
                ).strip(), "Fixture container name already exists"
            try:
                save("creating_" + scenario)
                # No existing resource is adopted; create returns an immutable owned ID.
                assert not command(
                    [
                        "docker",
                        "network",
                        "ls",
                        "-q",
                        "--filter",
                        "name=^" + network + "$",
                    ]
                ).strip()
                assert not command(
                    [
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        "name=^" + volume + "$",
                    ]
                ).strip()
                nid = command(
                    [
                        "docker",
                        "network",
                        "create",
                        "--internal",
                        "--label",
                        OWNER + "=" + name,
                        network,
                    ]
                ).strip()
                networks_created.append(nid)
                command(
                    [
                        "docker",
                        "volume",
                        "create",
                        "--label",
                        OWNER + "=" + name,
                        volume,
                    ]
                )
                volumes_created.append(volume)
                vol = json.loads(command(["docker", "volume", "inspect", volume]))[0]
                assert vol["Labels"].get(OWNER) == name
                data = Path(vol["Mountpoint"])
                (data / "fixture-generation.txt").write_text("original-generation\n")
                command(
                    [
                        "docker",
                        "compose",
                        "--project-directory",
                        str(case),
                        "-p",
                        project,
                        "-f",
                        str(compose),
                        "up",
                        "-d",
                        "--no-deps",
                        "--no-build",
                        "--pull",
                        "never",
                        service,
                        nginx,
                    ]
                )
                old = host.healthy(r.ORIGINAL)
                ng = host.inspect("nginx")
                for container in (old, ng):
                    assert set(container["NetworkSettings"]["Networks"]) == {network}
                    assert not container["HostConfig"]["PortBindings"]
                    assert container["Config"]["Labels"].get(OWNER) == name
                assert (
                    json.loads(command(["docker", "network", "inspect", nid]))[0]["Internal"]
                    is True
                )
                before_bytes = {
                    str(p): p.read_bytes() for p in (compose, canonical_fragment, nginx_config)
                }
                save("preparing_" + scenario)
                state = r.prepare(tx, proposal, host)
                host.settings["original_id"] = state["original_id"]
                assert not Path(state["candidate_data"]).exists()
                # Synthetic gates are in-memory fixture inputs, never a production approval file.
                evidence = {
                    "candidate_image": r.CANDIDATE,
                    "source_merged": True,
                    "required_checks_passed": True,
                    "grok_release_gate_satisfied": True,
                    "browser_acceptance_passed": True,
                    "private_recovery_passed": True,
                    "source_commit": source_commit,
                    "gate_evidence_reference": "SYNTHETIC ISOLATED REHEARSAL ONLY",
                }
                save("deploying_" + scenario)
                if scenario == "roundtrip":
                    r.deploy(tx, state, host, evidence)
                    assert state["phase"] == "deployed_local_checks_passed"
                    candidate_data = Path(state["candidate_data"])
                    assert candidate_data != data and candidate_data.is_dir()
                    assert (
                        candidate_data / "fixture-generation.txt"
                    ).read_text() == "original-generation\n"
                    (candidate_data / "candidate-only.txt").write_text("candidate-only-write\n")
                    assert not (data / "candidate-only.txt").exists()
                    proxy_probe = (
                        "import urllib.request,urllib.error; r=urllib.request.Request('http://"
                        + nginx
                        + ":8080/grok-bridge/api/chat', data=b'{}',"
                        "headers={'Content-Type':'application/json',"
                        "'X-Bridge-Token':'synthetic-fixture-token'});\ntry:\n"
                        " urllib.request.urlopen(r,timeout=10); "
                        "raise AssertionError('expected400')\n"
                        "except urllib.error.HTTPError as e:\n"
                        " assert e.code==400 and b'conversation_id is required' in e.read(); "
                        "print('proxy-probe-passed')"
                    )
                    assert (
                        host.run(
                            [
                                "docker",
                                "exec",
                                state["candidate_id"],
                                "python",
                                "-c",
                                proxy_probe,
                            ]
                        ).strip()
                        == "proxy-probe-passed"
                    )
                    save("rolling_back_" + scenario)
                    r.rollback(tx, state, host)
                    assert (
                        candidate_data / "candidate-only.txt"
                    ).read_text() == "candidate-only-write\n"
                else:
                    try:
                        r.deploy(tx, state, host, evidence)
                    except r.Refused as exc:
                        assert "Injected failure after owned original removal" in str(exc)
                    else:
                        raise AssertionError("Fault injection did not run")
                    assert host.fault_injected
                assert state["phase"] == "rolled_back" and not state["deployed"]
                restored = host.healthy(r.ORIGINAL)
                assert restored["Mounts"][0]["Source"] == str(data)
                assert (data / "fixture-generation.txt").read_text() == "original-generation\n"
                assert not (data / "candidate-only.txt").exists()
                assert host.inspect("nginx")["Id"] == ng["Id"]
                assert all(Path(p).read_bytes() == value for p, value in before_bytes.items())
                assert state["snapshot"]["restore_verified"]
                record.update(
                    passed=True,
                    final_phase=state["phase"],
                    snapshot_verified=True,
                    original_volume_preserved=True,
                    candidate_data_retained=True,
                    nginx_container_unchanged=True,
                    config_bytes_restored=True,
                    missing_container_fault_injected=host.fault_injected,
                    proxy_without_provider_probe=scenario == "roundtrip",
                )
            finally:
                ids = command(
                    ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=" + OWNER + "=" + name]
                ).split()
                for cid in ids:
                    item = inspect(cid)
                    assert item["Id"] == cid and item["Config"]["Labels"].get(OWNER) == name
                    command(["docker", "rm", "-f", cid])
                for nid in networks_created:
                    net = json.loads(command(["docker", "network", "inspect", nid]))[0]
                    assert (
                        net["Id"] == nid
                        and net["Labels"].get(OWNER) == name
                        and not net["Containers"]
                    )
                    command(["docker", "network", "rm", nid])
                record["retained_synthetic_volumes"] = volumes_created
                record["owned_containers_and_networks_removed"] = True
                save("cleaned_" + scenario)
        assert inventory() == baseline, "Unrelated production container/configuration changed"
        assert sha(operator) == operator_sha
        assert command([*git, "rev-parse", "HEAD"]).strip() == source_commit
        result.update(
            production_runtime_unchanged=True,
            operator_source_unchanged=True,
            synthetic_gates_not_production_authorization=True,
        )
        save("isolated_full_operator_rehearsal_passed_not_deployed")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.out.resolve(), args.source_root.resolve())))
    except BaseException as exc:
        if args.out.resolve() in CREATED_ROOTS:
            path = args.out / "failure.json"
            path.write_text(
                json.dumps(
                    {
                        "error_class": type(exc).__name__,
                        "error": str(exc),
                        "production_deployed": False,
                    }
                )
                + "\n"
            )
        raise
