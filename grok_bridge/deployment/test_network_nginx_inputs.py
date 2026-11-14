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
        for target, source in zip(list(n.TARGETS)[:2], (conf, projects), strict=True)
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
    assert set(mapping) == {mount["Destination"] for mount in mounts}
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


def renewal_inventory():
    return {
        "": {"kind": "directory"},
        "live": {"kind": "directory"},
        "live/example": {"kind": "directory"},
        "archive": {"kind": "directory"},
        "archive/example": {"kind": "directory"},
        "archive/example/fullchain1.pem": {"kind": "file"},
        "archive/example/fullchain2.pem": {"kind": "file"},
        "archive/example/privkey1.pem": {"kind": "file"},
        "live/example/fullchain.pem": {"kind": "symlink"},
        "archive/alias": {"kind": "symlink"},
    }


def test_relative_renewal_leaf_normalizes_to_inventoried_regular_file():
    assert (
        n._leaf_target(
            "live/example/fullchain.pem",
            "../../archive/example/fullchain1.pem",
            renewal_inventory(),
        )
        == "archive/example/fullchain1.pem"
    )


@pytest.mark.parametrize(
    "text",
    [
        "/archive/example/fullchain1.pem",
        "../../../outside.pem",
        "../../archive/example",
        "fullchain.pem",
        "../../archive/missing.pem",
        "../../archive/alias/../example/fullchain1.pem",
        "../../archive/example/fullchain1.pem/../fullchain2.pem",
        "../../archive/example/fullchain1.pem/",
        "../../archive/example/fullchain1.pem/.",
        "C:\\outside.pem",
        "",
        ".",
        "../..",
        "x" * (n.MAX_LINK_BYTES + 1),
    ],
)
def test_certificate_link_rejects_escapes_chains_directories_and_nonleaf_paths(text):
    with pytest.raises(n.NginxInputsError):
        n._leaf_target("live/example/fullchain.pem", text, renewal_inventory())


def test_include_record_cannot_enable_certificate_symlink_policy():
    rows = renewal_inventory()
    rows.pop("archive/alias")
    rows["live/example/fullchain.pem"].update(
        link_text="../../archive/example/fullchain1.pem", target="archive/example/fullchain1.pem"
    )
    n._check_links(rows, True)
    with pytest.raises(n.NginxInputsError, match="forbidden"):
        n._check_links(rows, False)


@pytest.mark.parametrize("change", ["none", "atime", "identity", "text", "permissions"])
def test_link_read_binds_identity_text_and_only_ignores_symlink_atime(
    tmp_path, monkeypatch, change
):
    source = tmp_path / "template"
    source.write_bytes(b"template")
    actual = source.stat()
    fields = {
        key: getattr(actual, key)
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_atime_ns",
        )
    }
    fields.update(st_mode=stat.S_IFLNK | 0o777, st_size=32)
    before = SimpleNamespace(**fields)
    after_fields = dict(fields)
    if change == "atime":
        after_fields["st_atime_ns"] += 1000
    elif change == "identity":
        after_fields["st_ino"] += 1
    elif change == "permissions":
        after_fields["st_mode"] = stat.S_IFLNK | 0o700
    after = SimpleNamespace(**after_fields)
    monkeypatch.setattr(n, "_linux", lambda: True)
    calls = []

    def readlink(name, *, dir_fd):
        assert name == "cert.pem" and dir_fd == 123
        calls.append(name)
        return "changed.pem" if change == "text" and len(calls) == 2 else "original.pem"

    def inspect(name, *, dir_fd, follow_symlinks):
        assert name == "cert.pem" and dir_fd == 123 and follow_symlinks is False
        return after

    monkeypatch.setattr(n.os, "readlink", readlink)
    monkeypatch.setattr(n.os, "stat", inspect)
    if change in {"none", "atime"}:
        result = n._read_leaf_link(tmp_path / "cert.pem", "cert.pem", 123, before, False)
        assert result["link_text"] == "original.pem"
        assert "st_atime_ns" not in result["metadata"] and len(calls) == 2
    else:
        with pytest.raises(n.NginxInputsError):
            n._read_leaf_link(tmp_path / "cert.pem", "cert.pem", 123, before, False)


def test_regular_metadata_still_includes_atime(monkeypatch):
    monkeypatch.setattr(n, "_linux", lambda: True)
    values = {
        key: 1
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_atime_ns",
        )
    }
    info = SimpleNamespace(**values)
    assert n._metadata(info)["st_atime_ns"] == 1
    assert "st_atime_ns" not in n._link_metadata(info)


def test_private_certificate_symlink_must_be_root_owned(tmp_path, monkeypatch):
    monkeypatch.setattr(n, "_linux", lambda: True)
    values = {
        key: 1
        for key in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
            "st_atime_ns",
        )
    }
    values.update(st_mode=stat.S_IFLNK | 0o777, st_uid=1000, st_gid=1000)
    with pytest.raises(n.NginxInputsError, match="root ownership"):
        n._read_leaf_link(tmp_path / "cert.pem", "cert.pem", 123, SimpleNamespace(**values), True)


def test_third_exact_certificate_mount_without_links_is_copied_privately(trees):
    folder, mounts, _, _ = trees
    certs = folder.parent / "plain-certs"
    certs.mkdir()
    (certs / "cert.pem").write_bytes(b"SYNTHETIC CERTIFICATE\n")
    mounts.append({"Destination": n.CERT_TARGET, "Source": str(certs), "Type": "bind"})
    record = n.prepare(folder, mounts)
    result = n.overrides(folder, record)
    assert set(result) == set(n.TARGETS)
    assert Path(result[n.CERT_TARGET]) == folder / n.COPY_ROOT / "letsencrypt"
    assert set(record["trees"][n.CERT_TARGET]["original"]) == {"", "cert.pem"}
    n.verify(folder, record)


@pytest.fixture
def certificates(trees):
    folder, mounts, _, _ = trees
    certs = folder.parent / "cert-original"
    archive = certs / "archive/example"
    live = certs / "live/example"
    archive.mkdir(parents=True)
    live.mkdir(parents=True)
    (archive / "fullchain1.pem").write_bytes(b"SYNTHETIC CERTIFICATE ONE\n")
    (archive / "fullchain2.pem").write_bytes(b"SYNTHETIC CERTIFICATE TWO\n")
    (archive / "privkey1.pem").write_bytes(b"SYNTHETIC NON-SECRET PRIVATE KEY\n")
    for file in archive.iterdir():
        file.chmod(0o600)
    symlink_or_skip(live / "fullchain.pem", "../../archive/example/fullchain1.pem")
    symlink_or_skip(live / "privkey.pem", "../../archive/example/privkey1.pem")
    mount = {"Destination": n.CERT_TARGET, "Source": str(certs), "Type": "bind", "RW": False}
    return folder, [*mounts, mount], certs


def test_certificate_snapshot_preserves_renewal_links_private_modes_and_original_metadata(
    certificates,
):
    folder, mounts, certs = certificates
    before, _ = n._scan(certs, n._Budget(), allow_links=True)
    record = n.prepare(folder, mounts)
    mapping = n.overrides(folder, record)
    copied = Path(mapping[n.CERT_TARGET])
    assert set(mapping) == set(n.TARGETS)
    assert (
        os.readlink(copied / "live/example/fullchain.pem") == "../../archive/example/fullchain1.pem"
    )
    assert os.readlink(copied / "live/example/privkey.pem") == "../../archive/example/privkey1.pem"
    for _ in range(2):
        n.verify(folder, record)
    after, _ = n._scan(certs, n._Budget(), allow_links=True)
    assert before == after
    copied_rows, contents = n._scan(
        copied, n._Budget(), private=True, capture=True, allow_links=True
    )
    assert contents["archive/example/fullchain1.pem"] == b"SYNTHETIC CERTIFICATE ONE\n"
    for relative, row in copied_rows.items():
        if row["kind"] != "symlink" and os.name == "posix":
            assert stat.S_IMODE((copied / relative).stat().st_mode) == (
                0o700 if row["kind"] == "directory" else 0o600
            )


@pytest.mark.parametrize(
    "change", ["link-text", "target-bytes", "target-missing", "copy-link", "copy-target"]
)
def test_certificate_original_or_private_link_and_target_drift_refuses(certificates, change):
    folder, mounts, certs = certificates
    record = n.prepare(folder, mounts)
    copied = folder / n.COPY_ROOT / "letsencrypt"
    if change in {"link-text", "copy-link"}:
        link = (certs if change == "link-text" else copied) / "live/example/fullchain.pem"
        link.unlink()
        link.symlink_to("../../archive/example/fullchain2.pem")
    elif change == "target-missing":
        (certs / "archive/example/fullchain1.pem").unlink()
    else:
        root = certs if change == "target-bytes" else copied
        (root / "archive/example/fullchain1.pem").write_bytes(b"changed certificate")
    with pytest.raises(n.NginxInputsError):
        n.overrides(folder, record)


@pytest.mark.parametrize(
    "text", ["../../../outside.pem", "/outside.pem", "../../archive/example", "privkey.pem"]
)
def test_actual_certificate_links_reject_escapes_absolute_directory_and_chain(certificates, text):
    folder, mounts, certs = certificates
    link = certs / "live/example/fullchain.pem"
    link.unlink()
    link.symlink_to(text, target_is_directory=text.endswith("example"))
    with pytest.raises(n.NginxInputsError):
        n.prepare(folder, mounts)


def test_certificate_support_does_not_enable_symlinks_in_include_mounts(certificates):
    folder, mounts, _ = certificates
    source = Path(mounts[0]["Source"])
    (source / "alias.conf").symlink_to("root.conf")
    with pytest.raises(n.NginxInputsError, match="forbidden"):
        n.prepare(folder, mounts)


def test_interrupted_private_link_creation_is_retained_and_refuses_reuse(certificates, monkeypatch):
    folder, mounts, _ = certificates
    original = n._write_link

    def interrupt(path, text):
        original(path, text)
        raise OSError("synthetic interruption after link creation")

    monkeypatch.setattr(n, "_write_link", interrupt)
    with pytest.raises(n.NginxInputsError):
        n.prepare(folder, mounts)
    assert (folder / n.COPY_ROOT / "letsencrypt/live/example/fullchain.pem").is_symlink()
    with pytest.raises(n.NginxInputsError, match="Existing"):
        n.prepare(folder, mounts)
