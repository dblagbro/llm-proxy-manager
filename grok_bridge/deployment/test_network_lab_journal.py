"""Private temporary journal files only; no host, Docker or execution admission."""

import copy
import hashlib
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import network_lab_context as lab
import network_policy as policy
import pytest
import release as r
from test_network_lab_context import record

ORIGINAL = "1" * 64
NGINX = "2" * 64
GATEWAY = "3" * 64
CANDIDATE = "4" * 64
ROLLBACK = "5" * 64
DEFAULT = "a" * 64
DEDICATED = "b" * 64
FINGERPRINT = "c" * 64


def state_for(ctx):
    binding = ctx.journal_binding()
    consumers = {
        role: {
            "id": identity,
            "config_sha256": FINGERPRINT,
            "default_route_sha256": FINGERPRINT,
            "default_network_id": DEFAULT,
            "default_attachment_sha256": FINGERPRINT,
            "phase": "pending",
            "owned": False,
            "attachment_sha256": None,
        }
        for role, identity in (("nginx", NGINX), ("llm-proxy2", GATEWAY))
    }
    return {
        "release_schema": 1,
        "mode": "lab",
        "lab_context": binding,
        "phase": "prepared_not_deployed",
        "project": ctx.project,
        "original_id": ORIGINAL,
        "original_image": dict(ctx.images)["original"],
        "candidate_image": dict(ctx.images)["candidate"],
        "original_data": "/guest/store/volumes/retained/_data",
        "candidate_data": str(ctx.generation_root / "candidate-data"),
        "files": {
            key: str(ctx.configuration_root / name)
            for key, name in (
                ("compose", "docker-compose.yml"),
                ("fragment", "grok-bridge.conf"),
                ("nginx", "nginx.conf"),
            )
        },
        "inputs": {str(ctx.configuration_root / ".env"): None},
        "artifacts": {"compose.before": FINGERPRINT},
        "others": {"/" + ctx.resource_map()["nginx"]: [NGINX, "running"]},
        "stop_intent": False,
        "cutover_intent": False,
        "deployed": False,
        "network_intent": False,
        "network_contract": 1,
        "network_transaction": {
            "schema": 2,
            "mode": "lab",
            "lab_context": copy.deepcopy(binding),
            "transaction_id": "d" * 32,
            "phase": "prepared",
            "network": {"phase": "planned", "id": None, "proof": None},
            "consumers": consumers,
            "bridge_id": None,
        },
        "original_network_projection": {
            "name": r.SERVICE,
            "id": ORIGINAL,
            "config_sha256": FINGERPRINT,
            "default_route_sha256": FINGERPRINT,
            "networks": {
                policy.DEFAULT_NAME: {
                    "id": DEFAULT,
                    "attachment_sha256": FINGERPRINT,
                    "aliases": [r.SERVICE],
                    "gw_priority": 0,
                }
            },
        },
    }


def owned(state):
    state.update(network_intent=True, phase="attaching_consumers")
    nested = state["network_transaction"]
    nested["network"].update(phase="owned", id=DEDICATED, proof="creation_return_verified")
    return nested


def cutover(state):
    nested = owned(state)
    for entry in nested["consumers"].values():
        entry.update(phase="attached", owned=True, attachment_sha256=FINGERPRINT)
    nested["phase"] = "attached"
    state.update(phase="cutover_intent", stop_intent=True, cutover_intent=True)
    return nested


@pytest.fixture
def setup(tmp_path, monkeypatch):
    ctx = lab.from_record(record())
    tmp_path.chmod(0o700)
    state = state_for(ctx)

    def anchor(logical, generation):
        assert str(logical) == str(generation) == str(ctx.generation_root)
        info = tmp_path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise r.Refused("Synthetic generation symlink")
        return tmp_path, (info.st_dev, info.st_ino, info.st_mode)

    # Only root ownership and Windows permission representation are simulated,
    # and only for actual regular-file inodes inside this private temporary tree.
    metadata = r._journal_file_metadata

    def private_file(info):
        inodes = {
            (p.lstat().st_dev, p.lstat().st_ino) for p in tmp_path.iterdir() if not p.is_symlink()
        }
        if (info.st_dev, info.st_ino) not in inodes:
            return metadata(info)
        return metadata(
            SimpleNamespace(
                st_mode=(stat.S_IFMT(info.st_mode) | 0o600) if os.name == "nt" else info.st_mode,
                st_nlink=info.st_nlink,
                st_uid=0,
            )
        )

    def prohibited(*args, **kwargs):
        raise AssertionError("Journal test attempted a host action")

    monkeypatch.setattr(r.network_nginx, "_lab_filesystem_path", anchor)
    monkeypatch.setattr(r, "_journal_file_metadata", private_file)
    if os.name == "nt":
        # Windows fstat exposes change time while path lstat exposes creation
        # time as st_ctime_ns. Linux production retains the exact comparison.
        actual_metadata = r._journal_metadata

        def windows_metadata(info):
            values = actual_metadata(info)
            return (*values[:-1], 0)

        monkeypatch.setattr(r, "_journal_metadata", windows_metadata)
    monkeypatch.setattr(r.Host, "run", prohibited)
    return ctx, state, tmp_path


def save(setup, state=None, *, ctx=None, binding=None):
    original_ctx, original_state, _ = setup
    selected = ctx if ctx is not None else original_ctx
    r.save(
        selected.generation_root,
        original_state if state is None else state,
        lab_context=selected,
        lab_binding=original_ctx.journal_binding() if binding is None else binding,
    )


def load(setup, *, ctx=None, binding=None):
    original_ctx, _, _ = setup
    selected = ctx if ctx is not None else original_ctx
    return r.load_state(
        selected.generation_root,
        lab_context=selected,
        lab_binding=original_ctx.journal_binding() if binding is None else binding,
    )


def write(path, value):
    path.write_bytes(value if type(value) is bytes else json.dumps(value).encode())
    path.chmod(0o600)


@pytest.mark.parametrize(
    "stage",
    [
        "prepared",
        "creating",
        "connecting",
        "cutover-unknown",
        "candidate-before-bridge",
        "original-recovered",
        "original-restarted",
        "conflict",
        "network-only",
    ],
)
def test_roundtrip_retains_legitimate_ambiguous_and_recovery_states(setup, stage):
    ctx, state, folder = setup
    nested = state["network_transaction"]
    if stage == "creating":
        state.update(phase="attaching_consumers", network_intent=True)
        nested["network"]["phase"] = "creating"
    elif stage == "connecting":
        owned(state)["consumers"]["nginx"]["phase"] = "connecting"
    elif stage in {"cutover-unknown", "candidate-before-bridge", "original-recovered", "conflict"}:
        nested = cutover(state)
        if stage != "cutover-unknown":
            state["candidate_id"] = CANDIDATE
        if stage in {"original-recovered", "conflict"}:
            nested["bridge_id"] = CANDIDATE
            state.update(rollback_id=ROLLBACK, phase="original_runtime_recovered", deployed=True)
        if stage == "conflict":
            state.update(
                phase="original_runtime_recovered_config_conflict",
                deployed=False,
                configuration_conflicts=[state["files"]["nginx"]],
            )
    elif stage == "original-restarted":
        owned(state)
        state.update(stop_intent=True, rollback_id=ORIGINAL, phase="original_runtime_recovered")
    elif stage == "network-only":
        nested = owned(state)
        nested["phase"] = "rolled_back"
        for consumer in nested["consumers"].values():
            consumer["phase"] = "detached"
        state["phase"] = "network_only_rolled_back"
    before = copy.deepcopy(state)
    save(setup)
    assert {k: v for k, v in state.items() if k not in {"updated_utc", "_journal_sha256"}} == before
    assert load(setup) == state
    assert (
        state["_journal_sha256"]
        == hashlib.sha256((folder / r.LAB_JOURNAL).read_bytes()).hexdigest()
    )
    assert not (folder / "state.private.json").exists()
    assert state["lab_context"] == ctx.journal_binding()


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id", "source_commit", "images"])
def test_same_namespace_context_drift_refuses_without_file_mutation(setup, field):
    ctx, state, folder = setup
    save(setup)
    changes = {
        "vm_uuid": "12345678-1234-1234-1234-123456789abd",
        "vm_name": "codex-grok-lab-other",
        "engine_id": "other-engine",
        "source_commit": "e" * 40,
        "images": tuple(
            (role, "sha256:" + "7" * 64 if role == "nginx" else pin) for role, pin in ctx.images
        ),
    }
    changed = replace(ctx, **{field: changes[field]})
    assert changed.generation_root == ctx.generation_root
    before, data = copy.deepcopy(state), (folder / r.LAB_JOURNAL).read_bytes()
    for operation in (lambda: save(setup, ctx=changed), lambda: load(setup, ctx=changed)):
        with pytest.raises(r.Refused):
            operation()
    assert state == before and (folder / r.LAB_JOURNAL).read_bytes() == data


@pytest.mark.parametrize("binding", [None, {}, False, "binding"])
def test_binding_is_required_before_any_filesystem_access(setup, monkeypatch, binding):
    ctx, state, _ = setup
    accessed = []
    monkeypatch.setattr(
        r.network_nginx, "_lab_filesystem_path", lambda *args: accessed.append(True)
    )
    before = copy.deepcopy(state)
    for operation in (
        lambda: r.save(ctx.generation_root, state, lab_context=ctx, lab_binding=binding),
        lambda: r.load_state(ctx.generation_root, lab_context=ctx, lab_binding=binding),
    ):
        with pytest.raises(r.Refused):
            operation()
    assert accessed == [] and state == before


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize(
    "variant", ["outside", "parent", "traversal", "double-leading", "trailing", "child"]
)
def test_exact_generation_path_is_required_before_filesystem_access(
    setup, monkeypatch, operation, variant
):
    ctx, state, _ = setup
    base = str(ctx.generation_root)
    folder = {
        "outside": "/mnt/s/code/generation",
        "parent": str(ctx.generation_root.parent),
        "traversal": base + "/../" + ctx.generation_root.name,
        "double-leading": "/" + base,
        "trailing": base + "/",
        "child": base + "/nested",
    }[variant]
    touched = []
    monkeypatch.setattr(r.network_nginx, "_lab_filesystem_path", lambda *args: touched.append(True))
    with pytest.raises(r.Refused):
        if operation == "load":
            r.load_state(folder, lab_context=ctx, lab_binding=ctx.journal_binding())
        else:
            r.save(folder, state, lab_context=ctx, lab_binding=ctx.journal_binding())
    assert touched == []


@pytest.mark.parametrize(
    "change",
    [
        "schema",
        "mode",
        "missing-header",
        "project",
        "image",
        "candidate-path",
        "original-path",
        "file-path",
        "input-path",
        "artifact-path",
        "boolean",
        "phase",
        "nested-context",
        "nested-schema",
    ],
)
def test_malformed_envelope_refuses_without_timestamp_or_receipt_changes(setup, change):
    ctx, state, folder = setup
    if change == "schema":
        state["release_schema"] = True
    elif change == "mode":
        state["mode"] = "production"
    elif change == "missing-header":
        state.pop("lab_context")
    elif change == "project":
        state["project"] = "docker"
    elif change == "image":
        state["candidate_image"] = "mutable:latest"
    elif change == "candidate-path":
        state["candidate_data"] += "/../candidate-data"
    elif change == "original-path":
        state["original_data"] = "//guest/store"
    elif change == "file-path":
        state["files"]["nginx"] = "/production/nginx.conf"
    elif change == "input-path":
        state["inputs"] = {"/production/.env": None}
    elif change == "artifact-path":
        state["artifacts"] = {"../outside": FINGERPRINT}
    elif change == "boolean":
        state["network_intent"] = 0
    elif change == "phase":
        state["phase"] = "unknown"
    elif change == "nested-context":
        state["network_transaction"]["lab_context"]["context"]["engine_id"] = "other"
    else:
        state["network_transaction"]["schema"] = 1
    state["updated_utc"] = "retained-timestamp"
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused):
        save(setup)
    assert state == before and list(folder.iterdir()) == []


@pytest.mark.parametrize(
    "collision",
    [
        "candidate-original",
        "candidate-rollback",
        "consumer-original",
        "consumer-candidate",
        "bridge-mismatch",
    ],
)
def test_unexpected_container_identity_collisions_refuse(setup, collision):
    _, state, folder = setup
    nested = cutover(state)
    state.update(candidate_id=CANDIDATE, rollback_id=ROLLBACK, phase="original_runtime_recovered")
    nested["bridge_id"] = CANDIDATE
    if collision == "candidate-original":
        state["candidate_id"] = ORIGINAL
    elif collision == "candidate-rollback":
        state["rollback_id"] = CANDIDATE
    elif collision == "consumer-original":
        nested["consumers"]["nginx"]["id"] = ORIGINAL
    elif collision == "consumer-candidate":
        nested["consumers"]["nginx"]["id"] = CANDIDATE
    else:
        nested["bridge_id"] = ROLLBACK
    with pytest.raises(r.Refused):
        save(setup)
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize("effect", ["creating", "owned", "connecting", "attached", "rolling-back"])
def test_nested_effects_require_outer_network_intent(setup, effect):
    _, state, _ = setup
    nested = state["network_transaction"]
    if effect == "creating":
        nested["network"]["phase"] = "creating"
    else:
        owned(state)
        state.update(network_intent=False, phase="prepared_not_deployed")
        if effect == "connecting":
            nested["consumers"]["nginx"]["phase"] = "connecting"
        elif effect == "attached":
            nested["consumers"]["nginx"].update(
                phase="attached", owned=True, attachment_sha256=FINGERPRINT
            )
        elif effect == "rolling-back":
            nested["phase"] = "rolling_back"
    with pytest.raises(r.Refused, match="effects lack"):
        save(setup)


@pytest.mark.parametrize("strip", ["none", "headers", "headers-and-nested", "all-markers-but-path"])
def test_renamed_or_stripped_lab_journal_is_not_consumed_as_production(setup, strip):
    _, state, folder = setup
    forged = copy.deepcopy(state)
    if strip != "none":
        for key in ("release_schema", "mode", "lab_context"):
            forged.pop(key)
    if strip in {"headers-and-nested", "all-markers-but-path"}:
        forged.pop("network_transaction")
    if strip == "all-markers-but-path":
        forged["project"] = "docker"
    write(folder / "state.private.json", forged)
    before = copy.deepcopy(forged)
    with pytest.raises(r.Refused):
        r.load_state(folder)
    with pytest.raises(r.Refused):
        r.save(folder, forged)
    assert forged == before


@pytest.mark.parametrize("operation", ["load", "save"])
@pytest.mark.parametrize("mode", ["lab", "production"])
def test_opposite_mode_file_even_when_unparseable_refuses(setup, operation, mode):
    _, _, folder = setup
    opposite = "state.private.json" if mode == "lab" else r.LAB_JOURNAL
    write(folder / opposite, b"not-json")
    with pytest.raises(r.Refused, match="Opposite-mode"):
        if mode == "lab":
            load(setup) if operation == "load" else save(setup)
        elif operation == "load":
            r.load_state(folder)
        else:
            r.save(folder, {"phase": "legacy"})


@pytest.mark.parametrize("which", ["own", "opposite"])
def test_broken_symlink_never_becomes_absent_journal(setup, monkeypatch, which):
    _, _, folder = setup
    target = folder / (r.LAB_JOURNAL if which == "own" else "state.private.json")
    if os.name == "posix":
        target.symlink_to(folder / "missing")
    else:
        actual = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or actual(path))
    with pytest.raises(r.Refused):
        save(setup)
    assert not list(folder.glob(".state.lab.private-*.tmp"))


@pytest.mark.parametrize("operation", ["load", "save"])
def test_hardlinked_journal_refuses(setup, operation):
    _, state, folder = setup
    save(setup)
    os.link(folder / r.LAB_JOURNAL, folder / "retained-link")
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused, match="single-link"):
        load(setup) if operation == "load" else save(setup)
    assert state == before


def test_nonregular_journal_refuses_without_waiting(setup):
    _, _, folder = setup
    path = folder / r.LAB_JOURNAL
    if os.name == "posix":
        os.mkfifo(path, 0o600)
        with pytest.raises(r.Refused, match="regular"):
            r.read_journal(path, lab_private=True)
    else:
        path.mkdir(mode=0o700)
        with pytest.raises(r.Refused, match="regular"):
            load(setup)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"mode":"lab","mode":"production"}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"private":"sensitive-canary"',
        b"\xff",
        b"[]",
    ],
)
def test_invalid_json_and_duplicates_are_sanitized(setup, payload):
    _, _, folder = setup
    write(folder / r.LAB_JOURNAL, payload)
    with pytest.raises(r.Refused) as caught:
        load(setup)
    assert "sensitive-canary" not in str(caught.value) and str(folder) not in str(caught.value)


def test_oversized_journal_is_bounded_before_json_parsing(setup, monkeypatch):
    _, _, folder = setup
    path = folder / r.LAB_JOURNAL
    write(path, b"x" * (r.LAB_JOURNAL_MAX_BYTES + 1))
    parsed = []
    monkeypatch.setattr(r, "_journal_parse", lambda *args: parsed.append(True))
    with pytest.raises(r.Refused, match="read bound"):
        load(setup)
    assert parsed == []


def test_read_growth_is_bounded_using_descriptor_not_initial_size(setup, monkeypatch):
    _, _, folder = setup
    path = folder / r.LAB_JOURNAL
    write(path, b"small")
    actual = os.fdopen

    class Growing:
        def __init__(self, fd, mode):
            self.stream = actual(fd, mode)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, bound):
            assert bound == 33
            with path.open("ab") as append:
                append.write(b"x" * 64)
            return self.stream.read(bound)

    monkeypatch.setattr(r.os, "fdopen", Growing)
    with pytest.raises(r.Refused, match="read bound"):
        r.read_journal(path, max_bytes=32, lab_private=True)


@pytest.mark.parametrize("fault", ["stale", "disappeared", "foreign-context-with-matching-digest"])
def test_cas_and_stored_context_refuse_before_any_pending_write(setup, fault):
    ctx, state, folder = setup
    save(setup)
    path = folder / r.LAB_JOURNAL
    if fault == "stale":
        write(path, {"concurrent": "retained"})
    elif fault == "disappeared":
        path.rename(folder / "retained-journal")
    else:
        foreign = state_for(replace(ctx, engine_id="other-engine"))
        write(path, foreign)
        state["_journal_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused):
        save(setup)
    assert state == before and not list(folder.glob(".state.lab.private-*.tmp"))


@pytest.mark.parametrize(
    "fault",
    [
        "partial-pending",
        "replace-failure",
        "replace-after-effect",
        "sync-failure",
        "sync-edit",
        "sync-context",
        "sync-opposite",
    ],
)
def test_failures_preserve_caller_timestamp_receipt_and_retained_evidence(
    setup, monkeypatch, fault
):
    ctx, state, folder = setup
    save(setup)
    state["phase"] = "rollback_intent"
    before = copy.deepcopy(state)
    old = (folder / r.LAB_JOURNAL).read_bytes()
    original_replace, original_sync = r.os.replace, r._journal_sync_directory
    if fault == "partial-pending":

        def partial(path, data):
            write(path, data[:30])
            raise OSError("sensitive-canary")

        monkeypatch.setattr(r, "private_write", partial)
    elif fault.startswith("replace"):

        def replace_fault(source, destination):
            if fault == "replace-after-effect":
                original_replace(source, destination)
            raise OSError("sensitive-canary")

        monkeypatch.setattr(r.os, "replace", replace_fault)
    else:

        def sync_fault(path):
            original_sync(path)
            if fault == "sync-failure":
                raise OSError("sensitive-canary")
            if fault == "sync-edit":
                write(folder / r.LAB_JOURNAL, {"concurrent": "retained"})
            elif fault == "sync-context":
                object.__setattr__(ctx, "engine_id", "changed-engine")
            else:
                write(folder / "state.private.json", b"opposite-mode")

        monkeypatch.setattr(r, "_journal_sync_directory", sync_fault)
    with pytest.raises(r.Refused) as caught:
        save(setup)
    assert state == before and "sensitive-canary" not in str(caught.value)
    if fault in {"partial-pending", "replace-failure"}:
        assert (folder / r.LAB_JOURNAL).read_bytes() == old
        assert list(folder.glob(".state.lab.private-*.tmp"))
    elif fault == "sync-edit":
        assert json.loads((folder / r.LAB_JOURNAL).read_bytes()) == {"concurrent": "retained"}
    else:
        assert (folder / r.LAB_JOURNAL).read_bytes() != old


@pytest.mark.parametrize("change", ["stored-edit", "opposite", "caller", "path"])
def test_guard_after_pending_write_refuses_concurrent_changes(setup, monkeypatch, change):
    _, state, folder = setup
    save(setup)
    before = copy.deepcopy(state)
    original_write = r.private_write
    walker = r.network_nginx._lab_filesystem_path
    changed = []

    def after_write(path, data):
        original_write(path, data)
        if change == "stored-edit":
            write(folder / r.LAB_JOURNAL, {"concurrent": "retained"})
        elif change == "opposite":
            write(folder / "state.private.json", b"other")
        elif change == "caller":
            state["phase"] = "concurrent-caller-edit"
        else:
            changed.append(True)

    def drifted(*args):
        path, fingerprint = walker(*args)
        return path, (*fingerprint, "changed") if changed else fingerprint

    monkeypatch.setattr(r, "private_write", after_write)
    monkeypatch.setattr(r.network_nginx, "_lab_filesystem_path", drifted)
    with pytest.raises(r.Refused):
        save(setup)
    assert (
        state["updated_utc"] == before["updated_utc"]
        and state["_journal_sha256"] == before["_journal_sha256"]
    )
    assert state["phase"] == ("concurrent-caller-edit" if change == "caller" else before["phase"])
    assert list(folder.glob(".state.lab.private-*.tmp"))


def test_interruption_retains_pending_and_caller_state(setup, monkeypatch):
    _, state, folder = setup
    save(setup)
    before = copy.deepcopy(state)
    actual = r.private_write

    def interrupted(path, data):
        actual(path, data)
        raise KeyboardInterrupt

    monkeypatch.setattr(r, "private_write", interrupted)
    with pytest.raises(KeyboardInterrupt):
        save(setup)
    assert state == before and list(folder.glob(".state.lab.private-*.tmp"))


def test_failed_commit_can_be_loaded_fresh_without_rebasing_failed_caller(setup, monkeypatch):
    _, state, _ = setup
    save(setup)
    state["phase"] = "rollback_intent"
    before = copy.deepcopy(state)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            r, "_journal_sync_directory", lambda path: (_ for _ in ()).throw(OSError("simulated"))
        )
        with pytest.raises(r.Refused):
            save(setup)
    assert state == before
    fresh = load(setup)
    assert (
        fresh["phase"] == "rollback_intent"
        and fresh["_journal_sha256"] != before["_journal_sha256"]
    )


def test_production_legacy_roundtrip_preserves_default_filename(tmp_path):
    state = {"phase": "legacy", "stop_intent": True, "original_data": "/retained/volume"}
    r.save(tmp_path, state)
    assert r.load_state(tmp_path) == state
    assert (tmp_path / "state.private.json").is_file()
    assert not (tmp_path / r.LAB_JOURNAL).exists()


@pytest.mark.parametrize(
    "change",
    [
        "original-identity",
        "original-data",
        "files",
        "inputs",
        "artifacts",
        "others",
        "managed-add",
        "managed-remove",
        "managed-drift",
        "original-projection",
        "transaction",
        "consumer-id",
        "consumer-config",
        "consumer-route",
        "consumer-network",
        "consumer-attachment",
    ],
)
def test_matching_receipt_cannot_rebase_retained_preparation(setup, change):
    ctx, state, folder = setup
    if change in {"managed-remove", "managed-drift"}:
        state["managed_environment"] = {"GROK_HOST": "https://grok-lab.invalid"}
    save(setup)
    path = folder / r.LAB_JOURNAL
    previous = path.read_bytes()
    nested = state["network_transaction"]
    if change == "original-identity":
        state["original_id"] = "6" * 64
        state["original_network_projection"]["id"] = "6" * 64
    elif change == "original-data":
        state["original_data"] = "/guest/store/volumes/replaced/_data"
    elif change == "files":
        state["files"]["nginx"] = str(ctx.configuration_root / "changed-nginx.conf")
    elif change == "inputs":
        state["inputs"][str(ctx.configuration_root / ".env")] = "7" * 64
    elif change == "artifacts":
        state["artifacts"]["compose.before"] = "7" * 64
    elif change == "others":
        state["others"] = {}
    elif change == "managed-add":
        state["managed_environment"] = None
    elif change == "managed-remove":
        state.pop("managed_environment")
    elif change == "managed-drift":
        state["managed_environment"]["GROK_HOST"] = "https://changed.invalid"
    elif change == "original-projection":
        state["original_network_projection"]["config_sha256"] = "7" * 64
    elif change == "transaction":
        nested["transaction_id"] = "7" * 32
    elif change == "consumer-network":
        for entry in nested["consumers"].values():
            entry["default_network_id"] = "7" * 64
        state["original_network_projection"]["networks"][policy.DEFAULT_NAME]["id"] = "7" * 64
    else:
        key = {
            "consumer-id": "id",
            "consumer-config": "config_sha256",
            "consumer-route": "default_route_sha256",
            "consumer-attachment": "default_attachment_sha256",
        }[change]
        nested["consumers"]["nginx"][key] = "7" * 64
    # These are independently valid envelopes with a genuine matching CAS receipt.
    r._journal_lab_envelope(state, ctx, ctx.journal_binding())
    assert state["_journal_sha256"] == hashlib.sha256(previous).hexdigest()
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused):
        save(setup)
    assert state == before and path.read_bytes() == previous
    assert not list(folder.glob(".state.lab.private-*.tmp"))


@pytest.mark.parametrize("receipt", ["candidate_id", "snapshot"])
@pytest.mark.parametrize("change", ["remove", "replace"])
def test_recorded_candidate_and_snapshot_receipts_cannot_be_rebased(setup, receipt, change):
    _, state, folder = setup
    cutover(state)
    state.update(candidate_id=CANDIDATE, snapshot={"files": 3, "sha256": FINGERPRINT})
    save(setup)
    previous = (folder / r.LAB_JOURNAL).read_bytes()
    if change == "remove":
        state.pop(receipt)
    else:
        state[receipt] = (
            "7" * 64 if receipt == "candidate_id" else {"files": 4, "sha256": FINGERPRINT}
        )
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused, match="receipt changed"):
        save(setup)
    assert state == before and (folder / r.LAB_JOURNAL).read_bytes() == previous
    assert not list(folder.glob(".state.lab.private-*.tmp"))


def test_initial_receipts_and_nested_progress_are_preserved_across_saves(setup):
    _, state, _ = setup
    save(setup)
    nested = cutover(state)
    state.update(candidate_id=CANDIDATE, snapshot={"files": 3, "sha256": FINGERPRINT})
    save(setup)
    nested["bridge_id"] = CANDIDATE
    state.update(phase="deployed_local_checks_passed", deployed=True)
    save(setup)
    state.update(phase="original_runtime_recovered", rollback_id=ORIGINAL)
    nested["phase"] = "rolling_back"
    save(setup)
    assert load(setup) == state


@pytest.mark.parametrize(
    "change", ["network-id", "network-proof", "remove-network", "remove-bridge"]
)
def test_recorded_network_ownership_and_bridge_are_preserved(setup, change):
    ctx, state, folder = setup
    nested = owned(state) if change == "remove-network" else cutover(state)
    if change != "remove-network":
        state["candidate_id"] = CANDIDATE
        nested["bridge_id"] = CANDIDATE
    save(setup)
    previous = (folder / r.LAB_JOURNAL).read_bytes()
    if change == "network-id":
        nested["network"]["id"] = "7" * 64
    elif change == "network-proof":
        nested["network"]["proof"] = "creation_intent_reconciled"
    elif change == "remove-network":
        nested["network"].update(phase="creating", id=None, proof=None)
    else:
        nested["bridge_id"] = None
    r._journal_lab_envelope(state, ctx, ctx.journal_binding())
    before = copy.deepcopy(state)
    with pytest.raises(r.Refused):
        save(setup)
    assert state == before and (folder / r.LAB_JOURNAL).read_bytes() == previous
    assert not list(folder.glob(".state.lab.private-*.tmp"))
