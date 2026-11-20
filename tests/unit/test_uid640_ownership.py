"""Exercise the UID transition on disposable files, never on host volumes."""

import json
import os
from pathlib import Path
from unittest import mock

import pytest

from tools import uid640_ownership as ownership
from tools.uid640_config import Config

original_check_volumes = ownership.check_volumes

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires isolated root fixture")


@pytest.fixture(autouse=True)
def config(monkeypatch, tmp_path):
    cfg = Config(
        "fixture-host",
        "llm-proxy2",
        "llm-proxy2",
        "docker",
        ("docker_llm-proxy2-data", "docker_llm-proxy2-logs"),
        (
            Path("/var/lib/docker/volumes/docker_llm-proxy2-data/_data"),
            Path("/var/lib/docker/volumes/docker_llm-proxy2-logs/_data"),
        ),
        ("/app/data", "/app/logs"),
        (1001, 1001),
        (640, 640),
        Path("/root"),
        tmp_path,
        "llmproxy.db",
    )
    monkeypatch.setattr(ownership, "CFG", cfg)
    monkeypatch.setattr(ownership, "OLD", cfg.old)
    monkeypatch.setattr(ownership, "NEW", cfg.new)
    monkeypatch.setattr(ownership, "ROOTS", cfg.roots)
    monkeypatch.setattr(ownership, "check_volumes", lambda: None)
    monkeypatch.setattr(ownership, "trusted_docker", lambda: None)


@pytest.fixture
def volume_roots(tmp_path):
    roots = (tmp_path / "data", tmp_path / "logs")
    for root in roots:
        root.mkdir()
        (root / "fixture").write_text("synthetic")
        os.chown(root, *ownership.OLD)
        os.chown(root / "fixture", *ownership.OLD)
    return roots


def owners(roots):
    return {(item["uid"], item["gid"]) for item in ownership.inventory(roots)}


def test_apply_and_rollback_preserve_content_and_modes(volume_roots, tmp_path):
    initial = ownership.inventory(volume_roots)
    manifest = ownership.apply(volume_roots, tmp_path)
    assert owners(volume_roots) == {ownership.NEW}
    assert all((root / "fixture").read_text() == "synthetic" for root in volume_roots)
    ownership.rollback(volume_roots, manifest)
    assert ownership.inventory(volume_roots) == initial


def test_rejects_symlink_before_changing_ownership(volume_roots, tmp_path):
    (volume_roots[0] / "link").symlink_to("fixture")
    with pytest.raises(RuntimeError, match="link or special"):
        ownership.apply(volume_roots, tmp_path)
    assert os.stat(volume_roots[0]).st_uid == ownership.OLD[0]


def test_rejects_hardlink_before_changing_ownership(volume_roots, tmp_path):
    os.link(volume_roots[0] / "fixture", volume_roots[0] / "second")
    with pytest.raises(RuntimeError, match="hardlink"):
        ownership.apply(volume_roots, tmp_path)
    assert os.stat(volume_roots[0]).st_uid == ownership.OLD[0]


def test_rejects_mixed_owner_before_changing_ownership(volume_roots, tmp_path):
    os.chown(volume_roots[0] / "fixture", 1234, 1234)
    with pytest.raises(RuntimeError, match="unexpected initial owner"):
        ownership.apply(volume_roots, tmp_path)
    assert os.stat(volume_roots[1]).st_uid == ownership.OLD[0]


def test_rollback_refuses_changed_file_set(volume_roots, tmp_path):
    manifest = ownership.apply(volume_roots, tmp_path)
    added = volume_roots[0] / "new-file"
    added.write_text("synthetic")
    os.chown(added, *ownership.NEW)
    with pytest.raises(RuntimeError, match="file set changed"):
        ownership.rollback(volume_roots, manifest)
    assert owners(volume_roots) == {ownership.NEW}


def test_rollback_refuses_replaced_inode(volume_roots, tmp_path):
    manifest = ownership.apply(volume_roots, tmp_path)
    fixture = volume_roots[0] / "fixture"
    retained = volume_roots[0] / "retained"
    fixture.rename(retained)
    fixture.write_text("replacement")
    os.chown(fixture, *ownership.NEW)
    retained.unlink()
    with pytest.raises(RuntimeError, match="file identity or mode changed"):
        ownership.rollback(volume_roots, manifest)
    assert owners(volume_roots) == {ownership.NEW}


def test_failed_update_rolls_back_partial_changes(volume_roots, tmp_path):
    actual_fchown = os.fchown
    calls = 0

    def fail_once(fd, uid, gid):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic failure")
        actual_fchown(fd, uid, gid)

    with (
        mock.patch.object(ownership.os, "fchown", side_effect=fail_once),
        pytest.raises(OSError, match="synthetic failure"),
    ):
        ownership.apply(volume_roots, tmp_path)
    assert owners(volume_roots) == {ownership.OLD}


def test_refuses_rollback_if_guard_reports_gateway_running(volume_roots, tmp_path):
    calls = 0

    def guard():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("gateway started")

    with pytest.raises(RuntimeError, match="gateway started"):
        ownership.apply(volume_roots, tmp_path, guard)
    assert owners(volume_roots) == {ownership.NEW}
    manifests = list(tmp_path.glob("llmproxy-uid640-ownership-*/manifest.json"))
    assert len(manifests) == 1
    ownership.rollback(volume_roots, manifests[0])
    assert owners(volume_roots) == {ownership.OLD}


@pytest.mark.parametrize(
    "other_mount",
    [
        {
            "Type": "volume",
            "Name": "docker_llm-proxy2-data",
            "Source": "/var/lib/docker/volumes/docker_llm-proxy2-data/_data",
        },
        {
            "Type": "bind",
            "Source": "/var/lib/docker/volumes/docker_llm-proxy2-data/_data/subdirectory",
        },
    ],
)
def test_stopped_gateway_refuses_another_container_using_volume(other_mount):
    gateway_id = "a" * 64
    image_id = "sha256:" + "b" * 64
    gateway = {
        "Id": gateway_id,
        "Image": image_id,
        "State": {"Running": False, "ExitCode": 0},
        "Config": {
            "User": "appuser",
            "Labels": {
                "com.docker.compose.service": "llm-proxy2",
                "com.docker.compose.project": "docker",
            },
        },
        "HostConfig": {"UsernsMode": "", "RestartPolicy": {"Name": "no"}},
        "Mounts": [
            {
                "Type": "volume",
                "RW": True,
                "Name": name,
                "Source": str(root),
                "Destination": dst,
            }
            for name, root, dst in zip(
                ("docker_llm-proxy2-data", "docker_llm-proxy2-logs"),
                ownership.ROOTS,
                ("/app/data", "/app/logs"),
                strict=True,
            )
        ],
    }
    other_id = "c" * 64
    other = {
        "Id": other_id,
        "Name": "/other",
        "Config": {"Labels": {}},
        "Mounts": [other_mount],
    }

    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        if args[1:3] == ["inspect", "llm-proxy2"]:
            return json.dumps([gateway])
        if args[1:3] == ["ps", "-aq"]:
            return gateway_id + "\n" + other_id + "\n"
        if args[1] == "info":
            return "[]"
        return json.dumps([other])

    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(ownership.subprocess, "check_output", side_effect=docker),
        pytest.raises(RuntimeError, match="another container"),
    ):
        ownership.stopped_gateway(gateway_id, image_id)


def test_stopped_gateway_refuses_automatic_restart():
    gateway_id = "a" * 64
    image_id = "sha256:" + "b" * 64
    gateway = {
        "Id": gateway_id,
        "Image": image_id,
        "State": {"Running": False, "ExitCode": 0},
        "Config": {
            "User": "appuser",
            "Labels": {
                "com.docker.compose.service": "llm-proxy2",
                "com.docker.compose.project": "docker",
            },
        },
        "HostConfig": {"UsernsMode": "", "RestartPolicy": {"Name": "unless-stopped"}},
        "Mounts": [
            {
                "Type": "volume",
                "RW": True,
                "Name": name,
                "Source": str(root),
                "Destination": dst,
            }
            for name, root, dst in zip(
                ("docker_llm-proxy2-data", "docker_llm-proxy2-logs"),
                ownership.ROOTS,
                ("/app/data", "/app/logs"),
                strict=True,
            )
        ],
    }
    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(
            ownership.subprocess,
            "check_output",
            side_effect=lambda args, **kwargs: "[]" if args[3] == "info" else json.dumps([gateway]),
        ),
        pytest.raises(RuntimeError, match="stopped gateway"),
    ):
        ownership.stopped_gateway(gateway_id, image_id)


@pytest.mark.parametrize("integrity_ok", [True, False])
def test_failed_exit_rollback_requires_database_integrity(integrity_ok):
    gateway_id = "a" * 64
    image_id = "sha256:" + "b" * 64
    gateway = {
        "Id": gateway_id,
        "Image": image_id,
        "State": {"Running": False, "ExitCode": 1},
        "Config": {
            "User": "appuser",
            "Labels": {
                "com.docker.compose.service": "llm-proxy2",
                "com.docker.compose.project": "docker",
            },
        },
        "HostConfig": {"UsernsMode": "", "RestartPolicy": {"Name": "no"}},
        "Mounts": [
            {
                "Type": "volume",
                "RW": True,
                "Name": name,
                "Source": str(root),
                "Destination": dst,
            }
            for name, root, dst in zip(
                ("docker_llm-proxy2-data", "docker_llm-proxy2-logs"),
                ownership.ROOTS,
                ("/app/data", "/app/logs"),
                strict=True,
            )
        ],
    }

    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        if args[1:3] == ["inspect", "llm-proxy2"]:
            return json.dumps([gateway])
        if args[1] == "info":
            return "[]"
        return gateway_id + "\n"

    database = mock.MagicMock()
    database.__enter__.return_value.execute.return_value.fetchall.return_value = [
        ("ok" if integrity_ok else "corrupt",)
    ]
    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(ownership.subprocess, "check_output", side_effect=docker),
        mock.patch.object(ownership.Path, "lstat", return_value=mock.Mock(st_mode=0o100600)),
        mock.patch.object(ownership.sqlite3, "connect", return_value=database),
    ):
        with pytest.raises(RuntimeError, match="stopped gateway"):
            ownership.stopped_gateway(gateway_id, image_id)
        if integrity_ok:
            ownership.stopped_gateway(gateway_id, image_id, allow_failed_exit=True)
        else:
            with pytest.raises(RuntimeError, match="integrity failed"):
                ownership.stopped_gateway(gateway_id, image_id, allow_failed_exit=True)


def test_absent_gateway_requires_empty_inventory_and_integrity():
    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        return "[]" if args[1] == "info" else ""

    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(ownership.subprocess, "check_output", side_effect=docker),
        mock.patch.object(ownership, "check_database_integrity") as integrity,
    ):
        ownership.absent_gateway()
        integrity.assert_called_once()


@pytest.mark.parametrize(
    "existing",
    [
        {"Name": "/llm-proxy2", "Config": {"Labels": {}}, "Mounts": []},
        {
            "Name": "/other",
            "Config": {"Labels": {}},
            "Mounts": [
                {
                    "Type": "volume",
                    "Name": "docker_llm-proxy2-data",
                    "Source": "/var/lib/docker/volumes/docker_llm-proxy2-data/_data",
                }
            ],
        },
    ],
)
def test_absent_gateway_refuses_existing_gateway_or_volume_user(existing):
    container_id = "d" * 64
    existing["Id"] = container_id

    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        if args[1] == "info":
            return "[]"
        if args[1] == "ps":
            return container_id
        return json.dumps([existing])

    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(ownership.subprocess, "check_output", side_effect=docker),
        mock.patch.object(ownership, "check_database_integrity") as integrity,
        pytest.raises(RuntimeError),
    ):
        ownership.absent_gateway()
    integrity.assert_not_called()


def test_rollback_rejects_changed_configuration(volume_roots, tmp_path, monkeypatch):
    from dataclasses import replace

    manifest = ownership.apply(volume_roots, tmp_path)
    monkeypatch.setattr(ownership, "CFG", replace(ownership.CFG, project="different"))
    with pytest.raises(RuntimeError, match="manifest roots mismatch"):
        ownership.rollback(volume_roots, manifest)
    assert owners(volume_roots) == {ownership.NEW}


def test_absent_gateway_rejects_other_container_with_service_labels():
    other_id = "e" * 64
    other = {
        "Id": other_id,
        "Name": "/different-name",
        "Mounts": [],
        "Config": {
            "Labels": {
                "com.docker.compose.service": "llm-proxy2",
                "com.docker.compose.project": "docker",
            }
        },
    }

    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        if args[1] == "info":
            return "[]"
        if args[1] == "ps":
            return other_id
        return json.dumps([other])

    with (
        mock.patch.object(ownership.socket, "gethostname", return_value="fixture-host"),
        mock.patch.object(ownership.subprocess, "check_output", side_effect=docker),
        pytest.raises(RuntimeError, match="same-service container"),
    ):
        ownership.absent_gateway()


def test_check_volumes_rejects_unexpected_root(monkeypatch):
    # Recover the implementation saved before the autouse fixture replaces it.
    cfg = ownership.CFG
    volumes = [
        {"Name": cfg.volume_names[0], "Driver": "local", "Mountpoint": "/other"},
        {
            "Name": cfg.volume_names[1],
            "Driver": "local",
            "Mountpoint": str(cfg.roots[1]),
        },
    ]
    with (
        mock.patch.object(ownership.subprocess, "check_output", return_value=json.dumps(volumes)),
        pytest.raises(RuntimeError, match="volume identity"),
    ):
        original_check_volumes()


def test_rollback_rejects_manifest_outside_configured_parent(volume_roots, tmp_path):
    manifest = ownership.apply(volume_roots, tmp_path)
    outside = tmp_path.parent / "outside" / manifest.parent.name / manifest.name
    with pytest.raises(RuntimeError, match="outside configured parent"):
        ownership.rollback(volume_roots, outside)
    assert owners(volume_roots) == {ownership.NEW}


def test_rollback_rejects_symlinked_manifest(volume_roots, tmp_path):
    manifest = ownership.apply(volume_roots, tmp_path)
    original = manifest.with_name("original.json")
    manifest.rename(original)
    manifest.symlink_to(original)
    with pytest.raises(OSError):
        ownership.rollback(volume_roots, manifest)
    assert owners(volume_roots) == {ownership.NEW}


def test_rollback_rejects_symlinked_manifest_directory(volume_roots, tmp_path):
    manifest = ownership.apply(volume_roots, tmp_path)
    directory = manifest.parent
    original = directory.with_name("renamed")
    directory.rename(original)
    directory.symlink_to(original, target_is_directory=True)
    with pytest.raises(OSError):
        ownership.rollback(volume_roots, manifest)
    assert owners(volume_roots) == {ownership.NEW}


def test_docker_command_pins_binary_socket_and_scrubs_environment():
    with mock.patch.object(ownership.subprocess, "check_output", return_value="[]") as call:
        ownership.docker_output(["docker", "info"])
    args, kwargs = call.call_args
    assert args[0] == [
        "/usr/bin/docker",
        "--host",
        "unix:///var/run/docker.sock",
        "info",
    ]
    assert kwargs["env"] == {"PATH": "/usr/bin:/bin", "HOME": "/root"}


def inspect_other(other):
    other_id = "f" * 64
    other["Id"] = other_id

    def docker(args, text, timeout, env):
        args = [args[0]] + args[3:]
        if args[1] == "ps":
            return other_id
        return json.dumps([other])

    return mock.patch.object(ownership.subprocess, "check_output", side_effect=docker)


def same_service_container(status="exited"):
    return {
        "Name": "/unrelated-fixture",
        "Mounts": [],
        "Config": {
            "Labels": {
                "com.docker.compose.service": "llm-proxy2",
                "com.docker.compose.project": "docker",
            }
        },
        "State": {
            "Running": False,
            "Paused": False,
            "Restarting": False,
            "Status": status,
        },
    }


@pytest.mark.parametrize("status", ["created", "exited", "dead"])
def test_allows_stopped_same_label_container_with_independent_mounts(status):
    other = same_service_container(status)
    other["Mounts"] = [
        {
            "Type": "volume",
            "Name": "independent-volume",
            "Source": "/var/lib/docker/volumes/independent-volume/_data",
        }
    ]
    with inspect_other(other):
        ownership.check_volume_users(None)


@pytest.mark.parametrize(
    "state",
    [
        {"Running": True, "Paused": False, "Restarting": False, "Status": "running"},
        {"Running": True, "Paused": True, "Restarting": False, "Status": "paused"},
        {"Running": False, "Paused": False, "Restarting": True, "Status": "restarting"},
        {"Running": False, "Paused": False, "Restarting": False, "Status": "unknown"},
        {"Running": False, "Paused": False, "Restarting": False},
        {"Running": None, "Paused": False, "Restarting": False, "Status": "exited"},
        None,
    ],
)
def test_rejects_active_or_unknown_same_label_container_state(state):
    other = same_service_container()
    other["State"] = state
    with (
        inspect_other(other),
        pytest.raises(RuntimeError, match="same-service container"),
    ):
        ownership.check_volume_users(None)


def test_rejects_exact_gateway_name_even_if_stopped_and_no_mounts():
    other = same_service_container()
    other["Name"] = "/llm-proxy2"
    with inspect_other(other), pytest.raises(RuntimeError, match="gateway exists"):
        ownership.check_volume_users(None)


@pytest.mark.parametrize(
    "mount",
    [
        {
            "Type": "volume",
            "Name": "docker_llm-proxy2-data",
            "Source": "/var/lib/docker/volumes/docker_llm-proxy2-data/_data",
        },
        {
            "Type": "bind",
            "Source": "/var/lib/docker/volumes/docker_llm-proxy2-data/_data/subdir",
        },
        {"Type": "bind", "Source": "/var/lib/docker/volumes"},
    ],
)
def test_rejects_stopped_same_label_with_shared_or_encompassing_mount(mount):
    other = same_service_container()
    other["Mounts"] = [mount]
    with inspect_other(other), pytest.raises(RuntimeError, match="another container"):
        ownership.check_volume_users(None)


def test_rejects_unknown_mount_inventory():
    other = same_service_container()
    other["Mounts"] = None
    with (
        inspect_other(other),
        pytest.raises(TypeError, match="unknown container mounts"),
    ):
        ownership.check_volume_users(None)


@pytest.mark.parametrize(
    "mount",
    [
        {},
        {"Type": "mystery", "Source": "/tmp/synthetic"},
        {"Type": "bind"},
        {"Type": "bind", "Source": "relative/data"},
        {"Type": "volume", "Name": "data"},
        {"Type": "volume", "Name": "data", "Source": "relative/data"},
        {"Type": "volume", "Source": "/var/lib/docker/volumes/unknown/_data"},
        {"Type": "tmpfs", "Source": "/tmp/unexpected"},
    ],
)
def test_rejects_missing_or_unsupported_mount_identity(mount):
    other = same_service_container()
    other["Mounts"] = [mount]
    with inspect_other(other), pytest.raises((RuntimeError, TypeError)):
        ownership.check_volume_users(None)


def test_accepts_explicit_sourceless_tmpfs_on_stopped_unrelated_container():
    other = same_service_container()
    other["Mounts"] = [{"Type": "tmpfs", "Source": ""}]
    with inspect_other(other):
        ownership.check_volume_users(None)
