"""Temporary-file and fake-Docker tests. Never contact Docker or a server."""

import copy
import io
import json
import sqlite3
import subprocess
import sys
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import rehearse_gateway as r


@pytest.fixture(autouse=True)
def no_real_commands(monkeypatch):
    def reject(*_args, **_kwargs):
        pytest.fail("Unit test attempted a real subprocess")

    monkeypatch.setattr(subprocess, "run", reject)


def fixture_db(path):
    with sqlite3.connect(path) as db:
        for table, fields in r.CREDENTIAL_COLUMNS.items():
            db.execute(f'CREATE TABLE "{table}" (' + ",".join(f'"{c}" TEXT' for c in fields) + ")")
            db.execute(
                f'INSERT INTO "{table}" VALUES (' + ",".join("?" for _ in fields) + ")",
                ["synthetic-" + c for c in fields],
            )


def test_copy_verified_independent_and_baseline_unchanged(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    fixture_db(source / "gateway.db")
    expected = r.manifest(source)
    target = tmp_path / "copy"
    r.independent_copy(source, target, expected)
    (target / "gateway.db").write_bytes(b"candidate-owned changes")
    assert r.manifest(source) == expected
    with pytest.raises(r.Refuse, match="already exists"):
        r.independent_copy(source, target, expected)


def test_copy_refuses_source_child(tmp_path):
    (tmp_path / "data").write_bytes(b"fixture")
    with pytest.raises(r.Refuse, match="separate"):
        r.independent_copy(tmp_path, tmp_path / "copy", r.manifest(tmp_path))


@pytest.mark.parametrize("name", ["database.db-wal", "database.db-shm"])
def test_snapshot_rejects_sqlite_sidecars(tmp_path, name):
    (tmp_path / name).write_bytes(b"fixture")
    with pytest.raises(r.Refuse, match="sidecars"):
        r.manifest(tmp_path)


def test_snapshot_rejects_subdirectories(tmp_path):
    (tmp_path / "nested").mkdir()
    with pytest.raises(r.Refuse, match="regular"):
        r.manifest(tmp_path)


def test_copy_refuses_wrong_digest(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "x").write_bytes(b"original")
    expected = r.manifest(source)
    expected["x"]["sha256"] = "0" * 64
    with pytest.raises(r.Refuse, match="verification"):
        r.independent_copy(source, tmp_path / "copy", expected)


def test_db_integrity_and_private_credentials(tmp_path):
    path = tmp_path / "gateway.db"
    fixture_db(path)
    before = r.database_report(path, credentials=True, immutable=True)
    assert before["integrity"] is True
    assert "synthetic" not in json.dumps(before)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE users SET password_hash='changed-only-in-test'")
    after = r.database_report(path, credentials=True)
    assert before["credential_fingerprints"] != after["credential_fingerprints"]


def test_missing_credential_schema_refused(tmp_path):
    path = tmp_path / "gateway.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE users (id TEXT)")
    with pytest.raises(r.Refuse, match="schema"):
        r.database_report(path, credentials=True)


@pytest.mark.parametrize(
    "items", [["A=x", "A=y"], ["A=x\nB=y"], ["BAD KEY=x"], ["missing-equals"], ["A=x\0y"]]
)
def test_environment_fail_closed(items):
    with pytest.raises(r.Refuse):
        r.parse_environment(items)


def test_environment_keeps_literal_dollars_equals():
    assert r.parse_environment(["SECRET_KEY=synthetic$a=b", "EMPTY="]) == {
        "SECRET_KEY": "synthetic$a=b",
        "EMPTY": "",
    }


def container(root, owner="fixture", stage="candidate", running=False):
    return {
        "Id": "a" * 64,
        "Image": r.CANDIDATE,
        "Name": "/" + owner + "-" + stage,
        "Config": {"Labels": {r.OWNER_LABEL: owner}, "User": "1001:1001"},
        "HostConfig": {
            "NetworkMode": "none",
            "PortBindings": {},
            "Privileged": False,
            "ReadonlyRootfs": True,
            "RestartPolicy": {"Name": "no"},
            "PidMode": "",
            "IpcMode": "private",
            "Tmpfs": {"/tmp": "fixture"},
            "Memory": 2 * r.GIB,
            "MemorySwap": 2 * r.GIB,
            "NanoCpus": 2_000_000_000,
            "PidsLimit": 256,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
        },
        "NetworkSettings": {"Ports": {}},
        "Mounts": [
            {
                "Type": "bind",
                "Destination": "/app/" + name,
                "Source": str(root / stage / name),
                "RW": True,
            }
            for name in ("data", "logs")
        ],
        "State": {"Running": running, "ExitCode": 0, "OOMKilled": False},
    }


def isolation_check(c, root):
    r.assert_isolated(
        c,
        "fixture",
        "fixture-candidate",
        r.CANDIDATE,
        root / "candidate/data",
        root / "candidate/logs",
    )


def test_isolation_accepts_exact_fixture(tmp_path):
    isolation_check(container(tmp_path), tmp_path)


def test_isolation_accepts_exposed_but_unpublished_port(tmp_path):
    c = container(tmp_path)
    c["NetworkSettings"]["Ports"] = {"3000/tcp": None}
    isolation_check(c, tmp_path)


def test_isolation_rejects_published_network_port(tmp_path):
    c = container(tmp_path)
    c["NetworkSettings"]["Ports"] = {"3000/tcp": [{"HostPort": "3000"}]}
    with pytest.raises(r.Refuse, match="network isolation"):
        isolation_check(c, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        lambda c: c["HostConfig"].update(NetworkMode="host"),
        lambda c: c["HostConfig"].update(PortBindings={"3000/tcp": [{"HostPort": "3000"}]}),
        lambda c: c["Config"]["Labels"].clear(),
        lambda c: c.update(Image=r.ORIGINAL),
        lambda c: c["Mounts"][0].update(Source="/var/lib/docker/volumes/production/_data"),
        lambda c: c["Mounts"].append(
            {
                "Type": "bind",
                "Source": "/var/run/docker.sock",
                "Destination": "/var/run/docker.sock",
                "RW": True,
            }
        ),
        lambda c: c["HostConfig"].update(Privileged=True),
        lambda c: c["HostConfig"].update(Memory=0),
        lambda c: c["HostConfig"].update(PidMode="host"),
        lambda c: c["Config"].update(User="root"),
    ],
)
def test_isolation_rejects_drift(tmp_path, change):
    c = container(tmp_path)
    change(c)
    with pytest.raises(r.Refuse):
        isolation_check(c, tmp_path)


def test_creation_arguments_never_mount_snapshot_or_publish(tmp_path):
    args = r.create_arguments(
        "fixture",
        "owner",
        r.CANDIDATE,
        tmp_path / "fresh/data",
        tmp_path / "fresh/logs",
        tmp_path / "private.env",
    )
    assert args[0] == "create" and args[-1] == r.CANDIDATE
    assert args[args.index("--network") + 1] == "none"
    assert args[args.index("--pull") + 1] == "never"
    assert "-p" not in args and "--publish" not in args and "--publish-all" not in args
    assert str(r.SNAPSHOT) not in " ".join(args)
    assert "synthetic-secret" not in " ".join(args)
    assert "--read-only" in args


class FakeDocker:
    def __init__(self, c=None):
        self.c = c
        self.calls = []
        self.stop_timeout = False
        self.stop_stays_running = False
        self.create_timeout = False
        self.create_container = None
        self.fail_inventory = False

    def inspect(self, identity):
        assert self.c and identity == self.c["Id"]
        return copy.deepcopy(self.c)

    def optional_named(self, name):
        self.calls.append(["optional_named", name])
        return copy.deepcopy(self.c) if self.c and self.c["Name"] == "/" + name else None

    def command(self, args, **_kwargs):
        self.calls.append(args)
        if args[0] == "ps":
            if self.fail_inventory:
                raise r.Refuse("Docker unavailable")
            return 0, self.c["Id"] if self.c else ""
        if args[0] == "create":
            self.c = copy.deepcopy(self.create_container)
            if self.create_timeout:
                raise subprocess.TimeoutExpired("docker", 60)
            return 0, self.c["Id"]
        if args[0] == "cp":
            Path(args[2]).write_text("appuser:x:1001:1001:fixture:/nonexistent:/bin/false\n")
        if args[0] == "stop":
            assert args[-1] == self.c["Id"]
            self.c["State"]["Running"] = self.stop_stays_running
            if self.stop_timeout:
                raise subprocess.TimeoutExpired("docker", 150)
        if args[0] == "rm":
            assert args[-1] == self.c["Id"]
            self.c = None
        return 0, ""


def runner_fixture(tmp_path, running=False):
    root = tmp_path / "fixture"
    root.mkdir()
    fake = FakeDocker(container(root, running=running))
    runner = r.Rehearsal(root, docker=fake)
    runner.state["containers"]["candidate"] = {
        "id": "a" * 64,
        "name": "fixture-candidate",
        "image": r.CANDIDATE,
    }
    return runner, fake


def test_stop_timeout_reconciles_and_records_intent(tmp_path):
    runner, fake = runner_fixture(tmp_path, running=True)
    fake.stop_timeout = True
    runner.stop("candidate")
    saved = json.loads((runner.root / "receipt.private.json").read_text())
    assert saved["containers"]["candidate"]["stop_intent"] is True
    assert fake.c["State"]["Running"] is False


def test_stop_timeout_still_running_is_not_success(tmp_path):
    runner, fake = runner_fixture(tmp_path, running=True)
    fake.stop_timeout = True
    fake.stop_stays_running = True
    with pytest.raises(r.Refuse, match="did not stop"):
        runner.stop("candidate")


def test_create_timeout_recovers_owned_id_and_cleanup(tmp_path):
    root = tmp_path / "fixture"
    root.mkdir()
    fake = FakeDocker()
    fake.create_container = container(root)
    fake.create_timeout = True
    runner = r.Rehearsal(root, docker=fake)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.make_container("candidate", r.CANDIDATE)
    assert runner.state["containers"]["candidate"]["id"] == "a" * 64
    runner.cleanup()
    assert fake.c is None
    assert ["rm", "-f", "a" * 64] in fake.calls


def test_cleanup_refuses_wrong_owner(tmp_path):
    runner, fake = runner_fixture(tmp_path)
    fake.c["Config"]["Labels"][r.OWNER_LABEL] = "somebody-else"
    with pytest.raises(r.Refuse, match="cleanup incomplete"):
        runner.cleanup()
    assert not any(call[0] in ("rm", "stop") for call in fake.calls)


def test_cleanup_recovers_absence_after_prior_rm(tmp_path):
    runner, fake = runner_fixture(tmp_path)
    fake.c = None
    runner.cleanup()
    assert runner.state["containers"]["candidate"]["removed"] is True
    assert not any(call[0] in ("rm", "stop") for call in fake.calls)


def test_cleanup_does_not_interpret_daemon_error_as_absence(tmp_path):
    runner, fake = runner_fixture(tmp_path)
    fake.fail_inventory = True
    with pytest.raises(r.Refuse, match="cleanup incomplete"):
        runner.cleanup()
    assert not runner.state["containers"]["candidate"].get("removed")


def test_cleanup_stop_failure_removes_only_owned_id_and_flags_failure(tmp_path):
    runner, fake = runner_fixture(tmp_path, running=True)
    fake.stop_timeout = True
    fake.stop_stays_running = True
    with pytest.raises(r.Refuse, match="cleanup incomplete"):
        runner.cleanup()
    assert fake.c is None
    assert ["rm", "-f", "a" * 64] in fake.calls


def test_unchanged_snapshot_check_detects_mutation(tmp_path):
    source = tmp_path / "snapshot"
    (source / "data").mkdir(parents=True)
    (source / "data/db").write_bytes(b"initial")
    runner = r.Rehearsal(tmp_path, source=source)
    runner.source_files = r.manifest(source / "data")
    runner.metadata = {}
    (source / "data/db").write_bytes(b"changed")
    with pytest.raises(r.Refuse, match="snapshot changed"):
        runner.verify_unchanged()


def test_fresh_copy_cannot_be_candidate_data_for_rollback(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "db").write_bytes(b"verified snapshot")
    expected = r.manifest(baseline)
    r.independent_copy(baseline, tmp_path / "candidate", expected)
    (tmp_path / "candidate/db").write_bytes(b"candidate migrated data")
    r.independent_copy(baseline, tmp_path / "original", expected)
    assert r.manifest(tmp_path / "original") == expected
    assert (tmp_path / "candidate/db").read_bytes() != (tmp_path / "original/db").read_bytes()


def test_probe_is_non_provider_and_proxy_free():
    assert "127.0.0.1:3000/health" in r.PROBE
    assert "127.0.0.1:3000/api/auth/me" in r.PROBE
    assert "ProxyHandler({})" in r.PROBE
    assert "Cookie" not in r.PROBE and "/v1/" not in r.PROBE


def test_cycle_will_not_start_unverified_image_user(tmp_path):
    runner, fake = runner_fixture(tmp_path)
    with pytest.raises(r.Refuse, match="UID/GID"):
        runner.cycle("candidate", "unused.db", {})
    assert not any(call[0] == "start" for call in fake.calls)


def snapshot_fixture(tmp_path, monkeypatch):
    source = tmp_path / "snapshot"
    (source / "data").mkdir(parents=True)
    fixture_db(source / "data/gateway.db")
    meta = r.manifest(source / "data")["gateway.db"]
    receipt = {
        "phase": "consistent_backup_and_independent_file_restore_verified",
        "host": "tmrwww01",
        "original_image": r.ORIGINAL,
        "sqlite_files": {
            "gateway.db": dict(meta, integrity="ok", independent_restore_verified=True)
        },
    }
    (source / "receipt.json").write_text(json.dumps(receipt))
    (source / "container.private.json").write_text(
        json.dumps(
            {
                "Image": r.ORIGINAL,
                "Config": {
                    "Env": [
                        "DATABASE_URL=sqlite+aiosqlite:////app/data/gateway.db",
                        "SECRET_KEY=synthetic-only",
                    ]
                },
            }
        )
    )
    original_plain = r.plain_directory
    monkeypatch.setattr(
        r, "plain_directory", lambda p, private=False: original_plain(p, private=False)
    )
    return source


def test_complete_snapshot_manifest_and_env(tmp_path, monkeypatch):
    source = snapshot_fixture(tmp_path, monkeypatch)
    env, active, files, metadata = r.validate_snapshot(source)
    assert active == "gateway.db" and env["SECRET_KEY"] == "synthetic-only"
    assert files == r.manifest(source / "data")
    assert set(metadata) == {"receipt.json", "container.private.json"}


def test_tampered_snapshot_rejected_before_container_creation(tmp_path, monkeypatch):
    source = snapshot_fixture(tmp_path, monkeypatch)
    with (source / "data/gateway.db").open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(r.Refuse, match="hash mismatch"):
        r.validate_snapshot(source)


def test_partial_receipt_rejected(tmp_path, monkeypatch):
    source = snapshot_fixture(tmp_path, monkeypatch)
    p = source / "receipt.json"
    receipt = json.loads(p.read_text())
    receipt["phase"] = "snapshotting"
    p.write_text(json.dumps(receipt))
    with pytest.raises(r.Refuse, match="not complete"):
        r.validate_snapshot(source)


def test_successful_create_verifies_image_passwd_before_any_start(tmp_path):
    root = tmp_path / "fixture"
    (root / "candidate").mkdir(parents=True)
    fake = FakeDocker()
    fake.create_container = container(root)
    runner = r.Rehearsal(root, docker=fake)
    runner.make_container("candidate", r.CANDIDATE)
    assert runner.state["containers"]["candidate"]["image_uid_gid_verified"] is True
    assert not any(call[0] == "start" for call in fake.calls)


def execute_probe(monkeypatch, payload, health_http=200, auth_http=401):
    """Execute the actual container probe with synthetic HTTP/import responses only."""
    version = types.ModuleType("app.__version__")
    version.__version__ = "5.22.54"
    monkeypatch.setitem(sys.modules, "app.__version__", version)
    calls = []

    class Response(io.BytesIO):
        def __init__(self, data, status):
            super().__init__(json.dumps(data).encode())
            self.status = status

    class Opener:
        def open(self, url, timeout):
            calls.append(url)
            assert timeout == 3
            if url == "http://127.0.0.1:3000/health":
                return Response(payload, health_http)
            assert url == "http://127.0.0.1:3000/api/auth/me"
            if auth_http != 200:
                raise urllib.error.HTTPError(url, auth_http, "synthetic", None, None)
            return Response({"unexpected": "anonymous success"}, 200)

    def build_opener(*handlers):
        assert len(handlers) == 1 and handlers[0].proxies == {}
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    exec(compile(r.PROBE, "<maintained-offline-probe>", "exec"), {})
    return calls


def health_payload(status="healthy", version="5.22.54"):
    return {
        "status": status,
        "version": version,
        "dbPool": {"private_diagnostic": "never-export-this"},
        "workers": {"private_name": "never-export-this"},
        "circuitBreakers": {"private_provider": "never-export-this"},
    }


@pytest.mark.parametrize("status", ["healthy", "degraded"])
def test_actual_probe_accepts_documented_offline_health_status(monkeypatch, capsys, status):
    calls = execute_probe(monkeypatch, health_payload(status))
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "version": "5.22.54",
        "health": True,
        "endpoint_status": status,
        "anonymous_auth_denied": True,
    }
    assert calls == ["http://127.0.0.1:3000/health", "http://127.0.0.1:3000/api/auth/me"]


@pytest.mark.parametrize("status", ["ok", "unknown", "unhealthy", None])
def test_actual_probe_rejects_shadowed_or_unknown_status(monkeypatch, status):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, health_payload(status))


def test_actual_probe_rejects_wrong_version(monkeypatch):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, health_payload(version="5.22.53"))


def test_actual_probe_rejects_wrong_route_shape(monkeypatch):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, {"status": "healthy", "version": "5.22.54"})


def test_actual_probe_rejects_non_object_body(monkeypatch):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, ["healthy"])


def test_actual_probe_rejects_non_success_http(monkeypatch):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, health_payload(), health_http=503)


@pytest.mark.parametrize("auth_http", [200, 403, 500])
def test_actual_probe_keeps_exact_anonymous_denial_requirement(monkeypatch, auth_http):
    with pytest.raises(AssertionError):
        execute_probe(monkeypatch, health_payload(), auth_http=auth_http)
