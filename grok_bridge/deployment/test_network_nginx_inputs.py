"""Synthetic temporary-tree checks; no Docker, SSH or production paths."""

import copy
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import network_nginx_inputs as n
import pytest


@pytest.fixture
def trees(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir(mode=0o700)
    sources = tmp_path / "originals"
    sources.mkdir()
    conf, projects = sources / "conf.d", sources / "projects-locations.d"
    conf.mkdir()
    projects.mkdir()
    (conf / "nested").mkdir()
    (conf / "root.conf").write_bytes(b"# PRIVATE-ONE\nlocation /one { return 200; }\n")
    (conf / "nested/a.conf").write_bytes(b"# nested\n")
    (projects / "grok.conf").write_bytes(b"# PRIVATE-TWO\n")
    for path in sources.rglob("*.conf"):
        path.chmod(0o600)
    mounts = [
        {"Destination": target, "Source": str(source), "Type": "bind", "RW": False}
        for target, source in zip(n.TARGETS, (conf, projects), strict=True)
    ]
    # CI runs as an unprivileged owner of these synthetic inputs. Exercise all
    # permission checks and Linux NOATIME reads, substituting only root identity.
    monkeypatch.setattr(n, "_private_owner", lambda info: None)
    monkeypatch.setattr(n.os, "geteuid", lambda: 0, raising=False)
    return attempt, mounts, conf, projects


def test_snapshot_is_byte_identical_private_and_original_metadata_is_unchanged(trees):
    folder, mounts, conf, projects = trees
    before = {
        path: n._metadata(path.stat())
        for root in (conf, projects)
        for path in [root, *root.rglob("*")]
    }
    record = n.prepare(folder, mounts)
    assert json.loads(json.dumps(record)) == record
    mapping = n.overrides(folder, record)
    assert set(mapping) == set(n.TARGETS)
    for target, source in mapping.items():
        assert Path(source).is_relative_to(folder)
        original = Path(record["trees"][target]["source"])
        for relative, row in record["trees"][target]["original"].items():
            copied = Path(source) / relative
            if row["kind"] == "file":
                # Do not use normal reads on original files: preserving source
                # atime is part of the Linux contract under test.
                _, contents = n._scan(original, n._Budget(), capture=True)
                assert copied.read_bytes() == contents[relative]
            if os.name == "posix":
                expected = 0o700 if row["kind"] == "directory" else 0o600
                assert stat.S_IMODE(copied.stat().st_mode) == expected
    assert all(n._metadata(path.stat()) == info for path, info in before.items())
    # Reading copies above may advance atime; verify before such external reads
    # is covered by prepare and overrides themselves.


def test_repeat_verification_and_override_resolution_preserve_everything(trees):
    folder, mounts, _, _ = trees
    record = n.prepare(folder, mounts)
    for _ in range(3):
        n.verify(folder, record)
        assert n.overrides(folder, record)


@pytest.mark.parametrize("count", [0, 1])
def test_optional_mounts_only_copy_present_reviewed_targets(trees, count):
    folder, mounts, _, _ = trees
    record = n.prepare(
        folder,
        mounts[:count]
        + [{"Destination": "/mnt/s", "Source": "/not/read/or/copied", "Type": "bind"}],
    )
    assert set(record["trees"]) == set(list(n.TARGETS)[:count])
    assert len(n.overrides(folder, record)) == count


def test_duplicate_target_is_refused_before_private_copy(trees):
    folder, mounts, _, _ = trees
    with pytest.raises(n.NginxInputsError, match="Duplicate"):
        n.prepare(folder, mounts + [copy.deepcopy(mounts[0])])
    assert not (folder / n.COPY_ROOT).exists()


@pytest.mark.parametrize("source", ["relative", "/", "folder", "parent"])
def test_original_private_overlap_and_noncanonical_source_refused(trees, source):
    folder, mounts, _, _ = trees
    mounts[0]["Source"] = str(
        folder if source == "folder" else folder.parent if source == "parent" else Path(source)
    )
    with pytest.raises(n.NginxInputsError):
        n.prepare(folder, mounts)
    assert not (folder / n.COPY_ROOT).exists()


@pytest.mark.parametrize(
    "change", ["edit", "add-file", "delete-file", "add-dir", "delete-dir", "mode"]
)
def test_original_changes_refuse_verification_and_overrides(trees, change):
    folder, mounts, conf, _ = trees
    record = n.prepare(folder, mounts)
    if change == "edit":
        (conf / "root.conf").write_bytes(b"changed")
    elif change == "add-file":
        (conf / "new.conf").write_bytes(b"new")
    elif change == "delete-file":
        (conf / "root.conf").unlink()
    elif change == "add-dir":
        (conf / "new-dir").mkdir()
    elif change == "delete-dir":
        (conf / "nested/a.conf").unlink()
        (conf / "nested").rmdir()
    else:
        if os.name != "posix":
            pytest.skip("POSIX mode mutation")
        (conf / "root.conf").chmod(0o640)
    for call in (n.verify, n.overrides):
        with pytest.raises(n.NginxInputsError):
            call(folder, record)


@pytest.mark.parametrize("change", ["edit", "add", "delete", "root-entry", "manifest"])
def test_copy_and_manifest_changes_refuse_reuse(trees, change):
    folder, mounts, _, _ = trees
    record = n.prepare(folder, mounts)
    root = folder / n.COPY_ROOT
    target = root / "conf.d/root.conf"
    if change == "edit":
        target.write_bytes(b"changed")
    elif change == "add":
        (target.parent / "unexpected.conf").write_bytes(b"extra")
    elif change == "delete":
        target.unlink()
    elif change == "root-entry":
        (root / "unexpected").mkdir()
    else:
        (root / n.MANIFEST).write_bytes(b"{}\n")
    with pytest.raises(n.NginxInputsError):
        n.overrides(folder, record)


def test_source_edit_during_copy_is_detected_and_attempt_retained(trees, monkeypatch):
    folder, mounts, conf, _ = trees
    original = n._write_file

    def alter(path, data):
        original(path, data)
        if path.name == "root.conf":
            (conf / "root.conf").write_bytes(b"concurrent source edit")

    monkeypatch.setattr(n, "_write_file", alter)
    with pytest.raises(n.NginxInputsError):
        n.prepare(folder, mounts)
    assert (folder / n.COPY_ROOT / "conf.d/root.conf").exists()


def test_interrupted_copy_is_retained_and_cannot_be_reused(trees, monkeypatch):
    folder, mounts, _, _ = trees
    original = n._write_file
    calls = 0

    def interrupt(path, data):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("PRIVATE-INPUT-CONTEXT")
        original(path, data)

    monkeypatch.setattr(n, "_write_file", interrupt)
    with pytest.raises(n.NginxInputsError) as error:
        n.prepare(folder, mounts)
    assert "PRIVATE-INPUT-CONTEXT" not in str(error.value)
    root = folder / n.COPY_ROOT
    assert root.exists() and list(root.rglob("*.conf"))
    assert not (root / n.MANIFEST).exists()
    monkeypatch.setattr(n, "_write_file", original)
    with pytest.raises(n.NginxInputsError, match="interrupted"):
        n.prepare(folder, mounts)
    assert root.exists()


def test_complete_snapshot_also_refuses_prepare_reuse(trees):
    folder, mounts, _, _ = trees
    n.prepare(folder, mounts)
    with pytest.raises(n.NginxInputsError, match="Existing"):
        n.prepare(folder, mounts)


@pytest.mark.parametrize("bound,value", [("MAX_ENTRIES", 3), ("MAX_BYTES", 16), ("MAX_DEPTH", 1)])
def test_bounds_stop_and_retain_partial_attempt(trees, monkeypatch, bound, value):
    folder, mounts, _, _ = trees
    monkeypatch.setattr(n, bound, value)
    with pytest.raises(n.NginxInputsError, match="bound"):
        n.prepare(folder, mounts)
    assert (folder / n.COPY_ROOT).exists()


def test_total_byte_bound_is_shared_across_both_targets(trees, monkeypatch):
    folder, mounts, conf, projects = trees
    # Each tree fits on its own; together they exceed the shared bound.
    first = sum(path.stat().st_size for path in conf.rglob("*") if path.is_file())
    second = sum(path.stat().st_size for path in projects.rglob("*") if path.is_file())
    monkeypatch.setattr(n, "MAX_BYTES", max(first, second))
    with pytest.raises(n.NginxInputsError, match="bytes"):
        n.prepare(folder, mounts)


def symlink_or_skip(path, target, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation unavailable to test account")


@pytest.mark.parametrize(
    "place", ["file", "directory", "source-ancestor", "copy-root", "copy-file"]
)
def test_symlinks_are_never_followed(trees, place):
    folder, mounts, conf, _ = trees
    if place in {"copy-root", "copy-file"}:
        record = n.prepare(folder, mounts)
        root = folder / n.COPY_ROOT
        if place == "copy-file":
            file = root / "conf.d/root.conf"
            file.unlink()
            symlink_or_skip(file, conf / "root.conf")
        else:
            moved = folder / "retained-copy"
            root.rename(moved)
            symlink_or_skip(root, moved, True)
        with pytest.raises(n.NginxInputsError):
            n.verify(folder, record)
        return
    if place == "file":
        symlink_or_skip(conf / "link.conf", conf / "root.conf")
    elif place == "directory":
        symlink_or_skip(conf / "linked", conf / "nested", True)
    else:
        alias = conf.parent.parent / "alias"
        symlink_or_skip(alias, conf.parent, True)
        mounts[0]["Source"] = str(alias / "conf.d")
    with pytest.raises(n.NginxInputsError):
        n.prepare(folder, mounts)


def test_special_file_is_refused(trees):
    folder, mounts, conf, _ = trees
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO creation requires POSIX")
    os.mkfifo(conf / "fifo")
    with pytest.raises(n.NginxInputsError, match="regular"):
        n.prepare(folder, mounts)


@pytest.mark.parametrize("schema", [True, 1.0, "1", 2])
def test_invalid_record_schema_refused(trees, schema):
    folder, mounts, _, _ = trees
    record = n.prepare(folder, mounts)
    record["schema"] = schema
    with pytest.raises(n.NginxInputsError, match="schema"):
        n.verify(folder, record)


@pytest.mark.parametrize(
    "relative", ["../outside", "/outside", "a/../outside", "a\\outside", "./a"]
)
def test_record_path_traversal_refused_before_any_copy_read(trees, relative):
    folder, mounts, _, _ = trees
    record = n.prepare(folder, mounts)
    tree = record["trees"]["/etc/nginx/conf.d"]
    tree["original"][relative] = tree["original"].pop("root.conf")
    with pytest.raises(n.NginxInputsError):
        n.verify(folder, record)


def test_unknown_target_cannot_be_injected(trees):
    folder, mounts, _, _ = trees
    record = n.prepare(folder, mounts)
    record["trees"]["/mnt/s"] = record["trees"].pop("/etc/nginx/conf.d")
    with pytest.raises(n.NginxInputsError, match="target"):
        n.overrides(folder, record)


def test_linux_root_ownership_check_rejects_nonroot(monkeypatch):
    monkeypatch.setattr(n, "_linux", lambda: True)
    with pytest.raises(n.NginxInputsError, match="root ownership"):
        n._private_owner(SimpleNamespace(st_uid=1000, st_gid=1000))


def test_linux_open_flags_require_nofollow_and_noatime(monkeypatch):
    monkeypatch.setattr(n, "_linux", lambda: True)
    monkeypatch.setattr(n.os, "O_NOATIME", 0x40000, raising=False)
    monkeypatch.setattr(n.os, "O_NOFOLLOW", 0x20000, raising=False)
    flags = n._flags()
    assert flags & n.os.O_NOATIME and flags & n.os.O_NOFOLLOW
