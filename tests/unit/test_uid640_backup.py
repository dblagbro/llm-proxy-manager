import json
import os
import sqlite3
import sys
from unittest import mock

import pytest

from tools import uid640_backup as backup
from tools.uid640_config import Config

pytestmark = pytest.mark.skipif(os.geteuid() != 0, reason="requires isolated root fixture")


@pytest.fixture
def environment(tmp_path):
    data, logs, backups = (tmp_path / name for name in ("data with spaces", "logs", "backups"))
    for p in (data, logs, backups):
        p.mkdir()
    db = data / "db.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE item (value TEXT)")
        conn.execute("INSERT INTO item VALUES (?)", ("fixture",))
    os.chown(db, 1001, 1001)
    cfg = Config(
        "fixture-host",
        "gateway",
        "gateway",
        "fixture",
        ("data", "logs"),
        (data, logs),
        ("/app/data", "/app/logs"),
        (1001, 1001),
        (640, 640),
        backups,
        backups,
        "db.sqlite",
    )
    container = {
        "Id": "container-id",
        "Image": "image-id",
        "State": {"Running": True},
        "Config": {
            "User": "appuser",
            "Labels": {
                "com.docker.compose.service": "gateway",
                "com.docker.compose.project": "fixture",
            },
        },
        "HostConfig": {"UsernsMode": ""},
        "Mounts": [
            {
                "Name": name,
                "Source": str(root),
                "Destination": dst,
                "Type": "volume",
                "RW": True,
            }
            for name, root, dst in zip(cfg.volume_names, cfg.roots, cfg.destinations, strict=True)
        ],
    }
    return cfg, container


def test_backup_is_private_and_restorable(environment, capsys):
    cfg, container = environment
    with (
        mock.patch.object(sys, "argv", ["backup", "/root/config", "container-id", "image-id"]),
        mock.patch.object(backup, "load", return_value=cfg),
        mock.patch.object(backup.ownership, "check_host"),
        mock.patch.object(backup.ownership, "docker_output", return_value=json.dumps([container])),
    ):
        backup.main()
    assert "integrity=ok" in capsys.readouterr().out
    dirs = list(cfg.backup_parent.iterdir())
    assert len(dirs) == 1 and dirs[0].stat().st_mode & 0o777 == 0o700
    dest = dirs[0] / "db.sqlite"
    assert dest.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT value FROM item").fetchone() == ("fixture",)


def test_backup_refuses_wrong_image(environment):
    cfg, container = environment
    with (
        mock.patch.object(
            sys, "argv", ["backup", "/root/config", "container-id", "different-image"]
        ),
        mock.patch.object(backup, "load", return_value=cfg),
        mock.patch.object(backup.ownership, "check_host"),
        mock.patch.object(backup.ownership, "docker_output", return_value=json.dumps([container])),
        pytest.raises(RuntimeError, match="gateway identity"),
    ):
        backup.main()
    assert list(cfg.backup_parent.iterdir()) == []


def test_backup_includes_uncheckpointed_wal(environment):
    cfg, container = environment
    source = cfg.roots[0] / cfg.db_name
    writer = sqlite3.connect(source)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO item VALUES (?)", ("wal-value",))
        writer.commit()
        assert source.with_name(source.name + "-wal").stat().st_size > 0
        with (
            mock.patch.object(sys, "argv", ["backup", "/root/config", "container-id", "image-id"]),
            mock.patch.object(backup, "load", return_value=cfg),
            mock.patch.object(backup.ownership, "check_host"),
            mock.patch.object(
                backup.ownership, "docker_output", return_value=json.dumps([container])
            ),
        ):
            backup.main()
        dest = next(cfg.backup_parent.iterdir()) / cfg.db_name
        with sqlite3.connect(dest) as conn:
            assert conn.execute("SELECT value FROM item ORDER BY value").fetchall() == [
                ("fixture",),
                ("wal-value",),
            ]
    finally:
        writer.close()


def test_backup_detects_database_replacement(environment):
    cfg, container = environment
    source = cfg.roots[0] / cfg.db_name
    actual_connect = sqlite3.connect

    def connect(db_uri, *args, **kwargs):
        result = actual_connect(db_uri, *args, **kwargs)
        if db_uri == source.as_uri() + "?mode=ro":
            source.rename(source.with_name("displaced"))
            source.write_text("replacement")
            os.chown(source, *cfg.old)
        return result

    with (
        mock.patch.object(sys, "argv", ["backup", "/root/config", "container-id", "image-id"]),
        mock.patch.object(backup, "load", return_value=cfg),
        mock.patch.object(backup.ownership, "check_host"),
        mock.patch.object(backup.ownership, "docker_output", return_value=json.dumps([container])),
        mock.patch.object(backup.sqlite3, "connect", side_effect=connect),
        pytest.raises(RuntimeError, match="source changed during backup"),
    ):
        backup.main()
