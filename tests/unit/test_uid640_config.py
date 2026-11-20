import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from tools import uid640_config as config

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires isolated root fixture")


def valid_data():
    return {
        "host": "fixture-host",
        "container": "gateway",
        "service": "gateway",
        "project": "fixture",
        "volumes": [
            {
                "name": "fixture-data",
                "root": "/var/lib/docker/volumes/fixture-data/_data",
                "destination": "/app/data",
            },
            {
                "name": "fixture-logs",
                "root": "/var/lib/docker/volumes/fixture-logs/_data",
                "destination": "/app/logs",
            },
        ],
        "old": [1001, 1001],
        "new": [640, 640],
        "backup_parent": "/root",
        "manifest_parent": "/root",
        "db_name": "gateway.db",
    }


def write_config(directory, data=None, mode=0o600):
    p = Path(directory) / "settings.json"
    p.write_text(json.dumps(valid_data() if data is None else data))
    p.chmod(mode)
    return p


def test_config_accepts_exact_host_and_secure_path():
    with tempfile.TemporaryDirectory(dir="/root") as td:
        path = write_config(td)
        with mock.patch.object(config.socket, "gethostname", return_value="fixture-host"):
            result = config.load(path)
        assert result.host == "fixture-host"
        assert result.old == (1001, 1001)


@pytest.mark.parametrize("mode", [0o644, 0o400])
def test_config_rejects_wrong_mode(mode):
    with tempfile.TemporaryDirectory(dir="/root") as td:
        path = write_config(td, mode=mode)
        with pytest.raises(RuntimeError, match="0600"):
            config.load(path)


def test_config_rejects_wrong_host():
    with tempfile.TemporaryDirectory(dir="/root") as td:
        path = write_config(td)
        with (
            mock.patch.object(config.socket, "gethostname", return_value="different-host"),
            pytest.raises(RuntimeError, match="host mismatch"),
        ):
            config.load(path)


def test_config_rejects_symlink_and_writable_parent():
    with tempfile.TemporaryDirectory(dir="/root") as td:
        base = Path(td)
        actual = write_config(base)
        (base / "link").symlink_to(actual)
        with pytest.raises(OSError):
            config.load(base / "link")
        base.chmod(0o777)
        with pytest.raises(RuntimeError, match="root-controlled"):
            config.load(actual)
        base.chmod(0o700)


@pytest.mark.parametrize("change", ["overlap", "old_equals_new", "unknown_field", "path_traversal"])
def test_config_rejects_unsafe_values(change):
    data = valid_data()
    if change == "overlap":
        data["volumes"][1]["root"] = data["volumes"][0]["root"] + "/child"
    elif change == "old_equals_new":
        data["new"] = data["old"]
    elif change == "unknown_field":
        data["surprise"] = True
    else:
        data["db_name"] = "../other"
    with tempfile.TemporaryDirectory(dir="/root") as td:
        path = write_config(td, data)
        with (
            mock.patch.object(config.socket, "gethostname", return_value="fixture-host"),
            pytest.raises(RuntimeError),
        ):
            config.load(path)
