"""Pure fixtures and simulated commands. Never invokes Docker, SSH or providers."""

import contextlib
import copy
import io
import json
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import rehearse_release as lifecycle
import release as r
import yaml


class ProbeResponse(io.BytesIO):
    def __init__(self, status, body):
        super().__init__(body)
        self.status = status


def probe_host(monkeypatch, override=None):
    """Execute the maintained in-container probe with synthetic HTTP only."""
    token = "synthetic-machine-probe-token"
    monkeypatch.setenv("BRIDGE_TOKEN", token)
    monkeypatch.setattr(r.socket, "gethostname", lambda: "synthetic-probe-container")
    monkeypatch.setattr(r.socket, "gethostbyname", lambda _: "192.0.2.9")
    calls = []
    responses = {
        ("GET", "/healthz", None): (200, b'{"status":"ok"}'),
        ("GET", "/api/status", None): (401, b'{"detail":"login required"}'),
        ("GET", "/api/status", token): (401, b'{"detail":"login required"}'),
        ("POST", "/api/chat", None): (401, b'{"detail":"invalid token"}'),
        ("POST", "/api/chat", token + "-invalid-probe-value"): (401, b'{"detail":"invalid token"}'),
        ("POST", "/api/chat", token): (400, b'{"detail":"conversation_id is required"}'),
    }
    if override:
        responses.update(override(token))

    class Opener:
        def open(self, req, timeout):
            assert timeout == 5
            assert req.full_url.startswith("http://192.0.2.9:8443/")
            assert not req.has_header("Cookie") and not req.has_header("Origin")
            path = req.full_url.removeprefix("http://192.0.2.9:8443")
            key = (req.get_method(), path, req.get_header("X-bridge-token"))
            calls.append(key)
            if req.get_method() == "POST":
                assert path == "/api/chat" and req.data == b"{}"
                assert req.get_header("Content-type") == "application/json"
            else:
                assert req.data is None
            status, body = responses[key]
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "synthetic", None, io.BytesIO(body)
                )
            return ProbeResponse(status, body)

    def opener(*handlers):
        assert len(handlers) == 2 and handlers[0].proxies == {}
        assert isinstance(handlers[1], urllib.request.HTTPRedirectHandler)
        assert handlers[1].redirect_request(None, None, None, None, None, None) is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", opener)

    class Host:
        def run(self, args):
            assert args[:5] == ["docker", "exec", "synthetic-exact-id", "python", "-c"]
            assert len(args) == 6 and token not in args[5]
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                # Execute our maintained literal with intercepted HTTP and no subprocess.
                exec(compile(args[5], "<nonprovider-probe>", "exec"), {})  # noqa: S102
            return stream.getvalue()

    return Host(), calls


def test_nonprovider_probe_accepts_final_machine_browser_separation(monkeypatch):
    host, calls = probe_host(monkeypatch)
    r.nonprovider_probe(host, {"Id": "synthetic-exact-id"})
    assert len(calls) == 6
    assert calls[-1] == ("POST", "/api/chat", "synthetic-machine-probe-token")


def test_nonprovider_probe_rejects_old_token_browser_bypass(monkeypatch):
    host, _ = probe_host(monkeypatch, lambda token: {("GET", "/api/status", token): (200, b"{}")})
    with pytest.raises(AssertionError):
        r.nonprovider_probe(host, {"Id": "synthetic-exact-id"})


@pytest.mark.parametrize("status", [200, 401, 403, 500])
def test_nonprovider_probe_rejects_missing_positive_machine_proof(monkeypatch, status):
    host, _ = probe_host(monkeypatch, lambda token: {("POST", "/api/chat", token): (status, b"{}")})
    with pytest.raises(AssertionError):
        r.nonprovider_probe(host, {"Id": "synthetic-exact-id"})


@pytest.mark.parametrize("body", [b"{}", b'{"detail":"some other error"}', b"[]", b"x" * 65537])
def test_nonprovider_probe_requires_exact_early_validation_response(monkeypatch, body):
    host, _ = probe_host(monkeypatch, lambda token: {("POST", "/api/chat", token): (400, body)})
    with pytest.raises(AssertionError):
        r.nonprovider_probe(host, {"Id": "synthetic-exact-id"})


@pytest.mark.parametrize("which", ["status", "missing-token", "wrong-token"])
def test_nonprovider_probe_rejects_anonymous_or_bad_token_success(monkeypatch, which):
    def override(token):
        keys = {
            "status": ("GET", "/api/status", None),
            "missing-token": ("POST", "/api/chat", None),
            "wrong-token": ("POST", "/api/chat", token + "-invalid-probe-value"),
        }
        return {keys[which]: (200, b"{}")}

    host, _ = probe_host(monkeypatch, override)
    with pytest.raises(AssertionError):
        r.nonprovider_probe(host, {"Id": "synthetic-exact-id"})


@pytest.fixture(autouse=True)
def no_real_host_commands(monkeypatch):
    def prohibited(*args, **kwargs):
        raise AssertionError("A unit test attempted a real host command")

    monkeypatch.setattr(r.subprocess, "run", prohibited)
    monkeypatch.setattr(r.time, "sleep", lambda _: None)
    if os.name != "posix":
        monkeypatch.setitem(
            sys.modules,
            "fcntl",
            SimpleNamespace(LOCK_EX=1, LOCK_NB=2, flock=lambda *args: None),
        )


COMPOSE = b"""# retained top comment
services:
  other:
    image: unrelated:tag # concurrent owner
    env_file: private.env
  llm-proxy2-grok-bridge:
    image: old:mutable
    build: ./grok
    environment:
      BRIDGE_TOKEN: ${PRIVATE_TOKEN}
      BRIDGE_PUBLIC_PATH: /grok-bridge
    volumes:
      - llm-proxy2-grok-bridge-data:/data
      - ./unchanged:/unchanged:ro
    restart: unless-stopped
  next:
    image: another:tag
volumes:
  llm-proxy2-grok-bridge-data:
"""


def test_compose_changes_only_grok_and_preserves_surrounding_bytes():
    after = r.render_compose(COMPOSE, "/private/release/candidate-data")
    a, b = r.service_span(COMPOSE.decode())
    c, d = r.service_span(after.decode())
    assert COMPOSE[:a] == after[:c] and COMPOSE[b:] == after[d:]
    original, updated = yaml.safe_load(COMPOSE), yaml.safe_load(after)
    service = updated["services"][r.SERVICE]
    assert service["image"] == r.CANDIDATE
    assert service["build"] == "./grok"
    assert service["environment"]["BRIDGE_TOKEN"] == "${PRIVATE_TOKEN}"
    assert service["volumes"][1] == "./unchanged:/unchanged:ro"
    assert updated["volumes"] == original["volumes"]
    assert service["volumes"][0]["bind"]["create_host_path"] is False


@pytest.mark.parametrize(
    "change",
    [
        lambda x: x.replace(
            b"llm-proxy2-grok-bridge-data:/data",
            b"llm-proxy2-grok-bridge-data:/data:ro",
        ),
        lambda x: x.replace(b"BRIDGE_PUBLIC_PATH: /grok-bridge", b"BRIDGE_PUBLIC_PATH: /wrong"),
        lambda x: x.replace(b"BRIDGE_TOKEN: ${PRIVATE_TOKEN}", b"BRIDGE_TOKEN: ''"),
        lambda x: x.replace(b"  next:", b"  llm-proxy2-grok-bridge:"),
    ],
)
def test_ambiguous_compose_refused(change):
    with pytest.raises(r.Refused):
        r.render_compose(change(COMPOSE), "/candidate")


def test_resolved_scope_detects_unrelated_edit():
    before = yaml.safe_load(COMPOSE)
    after = yaml.safe_load(r.render_compose(COMPOSE, "/candidate"))
    r.assert_resolved_scope(before, after, "/candidate")
    after["services"]["other"]["image"] = "unexpected"
    with pytest.raises(r.Refused, match="scope"):
        r.assert_resolved_scope(before, after, "/candidate")


def test_nginx_replaces_only_marked_block():
    before = (
        "server {\n  " + r.MARKER + "\n    old;\n"
        "  # ── from projects-locations.d/unrelated.conf ──\n  keep;\n}\n"
    ).encode()
    fragment = b"location /vnc { proxy_pass http://bridge:8443/vnc/websockify; }\n"
    after = r.render_nginx(before, fragment)
    assert after.startswith(b"server {\n  ")
    assert after.endswith("# ── from projects-locations.d/unrelated.conf ──\n  keep;\n}\n".encode())
    assert b"old;" not in after
    with pytest.raises(r.Refused):
        r.render_nginx(before.replace(r.MARKER.encode(), b"wrong"), fragment)


def test_cas_preserves_inode_and_refuses_concurrent_edit(tmp_path):
    path = tmp_path / "nginx.conf"
    path.write_bytes(b"original")
    inode = path.stat().st_ino
    r.cas_write(path, b"original", b"replacement longer")
    assert path.stat().st_ino == inode and path.read_bytes() == b"replacement longer"
    path.write_bytes(b"concurrent owner's edit")
    with pytest.raises(r.Refused, match="CAS"):
        r.cas_write(path, b"replacement longer", b"original")
    assert path.read_bytes() == b"concurrent owner's edit"


@pytest.mark.parametrize("candidate", ["/original", "/original/child", "/"])
def test_candidate_volume_must_be_independent(candidate):
    with pytest.raises(r.Refused, match="overlap"):
        r.assert_separate_volume("/original", candidate)


def make_tar(path, name="document", content=b"synthetic", link=None):
    with tarfile.open(path, "w") as tar:
        item = tarfile.TarInfo(name)
        item.mode = 0o600
        item.uid = os.getuid() if hasattr(os, "getuid") else 0
        item.gid = os.getgid() if hasattr(os, "getgid") else 0
        if link:
            item.type = tarfile.SYMTYPE
            item.linkname = link
            tar.addfile(item)
        else:
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))


def test_independent_restore_checks_actual_bytes(tmp_path):
    archive = tmp_path / "snapshot.tar"
    make_tar(archive)
    manifest = r.archive_manifest(archive)
    data = tmp_path / "restore"
    data.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(data, filter="data")
    if os.name != "posix":
        # Windows cannot represent the full POSIX mode. The bytes check remains
        # meaningful here; UID/GID and real tar ACL/xattr acceptance is Linux-only.
        manifest[0]["mode"] = (data / "document").stat().st_mode & 0o7777
    r.verify_restore(data, manifest)
    (data / "document").write_bytes(b"tampered")
    with pytest.raises(r.Refused, match="differs"):
        r.verify_restore(data, manifest)


@pytest.mark.parametrize(
    "name,link", [("../escape", None), ("/absolute", None), ("link", "../escape")]
)
def test_archive_escape_rejected(tmp_path, name, link):
    archive = tmp_path / "bad.tar"
    make_tar(archive, name=name, link=link)
    with pytest.raises(r.Refused):
        r.archive_manifest(archive)


def test_environment_absence_is_a_cas_input(tmp_path):
    path = tmp_path / "private.env"
    state = {"inputs": {str(path): None}}
    r.assert_inputs(state)
    path.write_text("TOKEN=synthetic")
    with pytest.raises(r.Refused):
        r.assert_inputs(state)


def approval():
    return {
        "candidate_image": r.CANDIDATE,
        "source_merged": True,
        "required_checks_passed": True,
        "grok_release_gate_satisfied": True,
        "browser_acceptance_passed": True,
        "private_recovery_passed": True,
        "source_commit": "a" * 40,
        "gate_evidence_reference": "synthetic-test-evidence",
    }


def test_gateway_waiver_cannot_imply_grok_gate():
    evidence = approval()
    evidence["grok_release_gate_satisfied"] = False
    evidence["gateway_waiver"] = "5.22.54 only"
    with pytest.raises(r.Refused, match="grok_release_gate"):
        r.gate(evidence)


class FakeHost:
    def __init__(self, old, candidate_data):
        self.old = copy.deepcopy(old)
        self.current = copy.deepcopy(old)
        self.candidate_data = str(candidate_data)
        self.calls = []
        self.stop_error = False
        self.up_timeout = False
        self.up_disappears = False

    def inspect(self, name):
        return self.current if name != "nginx" else {"Id": "nginx-original"}

    def optional_service(self):
        return self.current

    def healthy(self, image, exact_id=None):
        assert self.current["Image"] == image
        if exact_id:
            assert self.current["Id"] == exact_id
        assert self.current["State"]["Running"]
        return self.current

    def image(self, image):
        assert image in (r.ORIGINAL, r.CANDIDATE)

    def others(self):
        return {"/nginx": ["nginx-original", "running"]}

    def exclusive_volume(self, source, owner):
        assert owner == "original-id"

    def stop(self, cid, allow_unclean=False):
        self.calls.append(("stop", cid, allow_unclean))
        assert self.current["Id"] == cid
        self.current["State"]["Running"] = False
        if self.stop_error:
            raise TimeoutError("ambiguous client timeout")

    def start_original(self, cid):
        self.calls.append(("recover-original", cid))
        assert self.current["Id"] == cid and self.current["Image"] == r.ORIGINAL
        self.current["State"]["Running"] = True

    def validate_nginx(self, *args):
        self.calls.append(("validate-nginx",))

    def reload_nginx(self, *args):
        self.calls.append(("reload-nginx",))

    def compose(self, path, project, *command):
        self.calls.append(("up", str(path), command))
        assert "--no-deps" in command and "--no-build" in command
        assert command[-1] == r.SERVICE and "down" not in command
        self.current = copy.deepcopy(self.old)
        if Path(path).name == "original-resolved.json":
            self.current["Id"] = "rollback-id"
        else:
            if self.up_disappears:
                self.current = None
                raise TimeoutError("original removed; candidate create not completed")
            self.current["Id"] = "candidate-id"
            self.current["Image"] = r.CANDIDATE
            self.current["Mounts"][0].update(Source=self.candidate_data, Type="bind")
            self.current["Config"]["Env"] += [
                k + "=" + v for k, v in r.ENV_ADDITIONS.items() if k != "BRIDGE_PUBLIC_PATH"
            ]
            if self.up_timeout:
                raise TimeoutError("compose accepted but CLI timed out")


@pytest.fixture
def transaction(tmp_path, monkeypatch):
    data = tmp_path / "candidate-data"
    old = {
        "Id": "original-id",
        "Image": r.ORIGINAL,
        "State": {"Running": True},
        "RestartCount": 0,
        "HostConfig": {},
        "NetworkSettings": {"Networks": {"canonical": {}}},
        "Config": {"Env": ["BRIDGE_TOKEN=synthetic", "BRIDGE_PUBLIC_PATH=/grok-bridge"]},
        "Mounts": [
            {
                "Destination": "/data",
                "Type": "volume",
                "Source": "/original-data",
                "RW": True,
            }
        ],
    }
    files = {}
    for key in ("compose", "fragment", "nginx"):
        path = tmp_path / (key + ".live")
        path.write_bytes(b"before " + key.encode())
        (tmp_path / (key + ".before")).write_bytes(path.read_bytes())
        (tmp_path / (key + ".after")).write_bytes(b"after " + key.encode())
        files[key] = str(path)
    (tmp_path / "original-inspect.json").write_text(json.dumps(old))
    (tmp_path / "nginx-inspect.json").write_text(json.dumps({"Id": "nginx-original"}))
    (tmp_path / "original-resolved.json").write_text(json.dumps({"image": r.ORIGINAL}))
    state = {
        "phase": "prepared_not_deployed",
        "project": "docker",
        "original_id": old["Id"],
        "original_data": "/original-data",
        "candidate_data": str(data),
        "files": files,
        "inputs": {},
        "artifacts": {},
        "others": {"/nginx": ["nginx-original", "running"]},
        "stop_intent": False,
        "cutover_intent": False,
        "deployed": False,
    }
    state["artifacts"] = {
        p.name: r.file_digest(p) for p in tmp_path.iterdir() if not p.name.endswith(".live")
    }
    host = FakeHost(old, data)

    def snapshot(folder, s, h):
        assert s["stop_intent"] and not h.current["State"]["Running"]
        assert json.loads((folder / "state.private.json").read_text())["stop_intent"]
        data.mkdir()
        s["snapshot"] = {"restore_verified": True}

    monkeypatch.setattr(r, "fresh_snapshot", snapshot)
    monkeypatch.setattr(r, "nonprovider_probe", lambda *args: None)
    monkeypatch.setattr(r, "capacity", lambda *args: None)
    return tmp_path, state, host


def test_success_is_single_service_and_original_volume_retained(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    assert state["deployed"] and state["phase"] == "deployed_local_checks_passed"
    assert host.current["Mounts"][0]["Source"] == state["candidate_data"]
    assert host.old["Mounts"][0]["Source"] == "/original-data"
    assert not any("volume" in call for call in host.calls)
    assert state["provider_acceptance"] is False


def test_cas_conflict_before_stop_mutates_nothing(transaction):
    folder, state, host = transaction
    Path(state["files"]["compose"]).write_bytes(b"concurrent edit")
    with pytest.raises(r.Refused, match="CAS"):
        r.deploy(folder, state, host, approval())
    assert not host.calls and host.current["State"]["Running"]


def test_stop_timeout_reconciles_and_restarts_exact_original(transaction):
    folder, state, host = transaction
    host.stop_error = True
    with pytest.raises(TimeoutError):
        r.deploy(folder, state, host, approval())
    assert state["stop_intent"] and not state["cutover_intent"]
    assert ("recover-original", "original-id") in host.calls
    assert host.current["State"]["Running"] and host.current["Id"] == "original-id"
    assert state["phase"] == "pre_cutover_failed_original_restarted"


def test_snapshot_failure_always_recovers_original(transaction, monkeypatch):
    folder, state, host = transaction
    monkeypatch.setattr(
        r,
        "fresh_snapshot",
        lambda *args: (_ for _ in ()).throw(OSError("disk fixture")),
    )
    with pytest.raises(OSError):
        r.deploy(folder, state, host, approval())
    assert host.current["State"]["Running"] and host.current["Id"] == "original-id"
    assert not state["cutover_intent"]


def test_probe_failure_rolls_back_exact_original_pair(transaction, monkeypatch):
    folder, state, host = transaction
    monkeypatch.setattr(
        r, "nonprovider_probe", lambda *args: (_ for _ in ()).throw(r.Refused("probe"))
    )
    with pytest.raises(r.Refused, match="probe"):
        r.deploy(folder, state, host, approval())
    assert state["phase"] == "rolled_back" and not state["deployed"]
    assert host.current["Image"] == r.ORIGINAL
    assert host.current["Mounts"][0]["Source"] == "/original-data"
    assert (folder / "candidate-data").exists()
    r.assert_files(folder, state, "before")


def test_ambiguous_compose_reconciles_owned_candidate_then_rolls_back(transaction):
    folder, state, host = transaction
    host.up_timeout = True
    with pytest.raises(TimeoutError):
        r.deploy(folder, state, host, approval())
    assert state["candidate_id"] == "candidate-id" and state["phase"] == "rolled_back"
    assert host.current["Image"] == r.ORIGINAL


def test_missing_service_after_failed_create_restores_frozen_original(transaction):
    folder, state, host = transaction
    host.up_disappears = True
    with pytest.raises(TimeoutError):
        r.deploy(folder, state, host, approval())
    assert state["phase"] == "rolled_back"
    assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
    assert host.current["Mounts"][0]["Source"] == "/original-data"
    r.assert_files(folder, state, "before")


def test_optional_service_does_not_treat_daemon_failure_as_absence(tmp_path, monkeypatch):
    host = r.Host(tmp_path)
    monkeypatch.setattr(
        host,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(r.Refused("daemon unavailable")),
    )
    with pytest.raises(r.Refused, match="daemon unavailable"):
        host.optional_service()


def test_effective_environment_detects_undocumented_runtime_override():
    image = {"Config": {"Env": ["PATH=/image", "DEFAULT=value"]}}
    service = {"environment": {"BRIDGE_TOKEN": "synthetic"}}
    live = {"Config": {"Env": ["PATH=/image", "DEFAULT=value", "BRIDGE_TOKEN=synthetic"]}}
    r.assert_original_environment(image, service, live)
    live["Config"]["Env"].append("UNTRACKED_OVERRIDE=keep-me")
    with pytest.raises(r.Refused, match="Effective original"):
        r.assert_original_environment(image, service, live)


def test_all_managed_environment_is_preserved_but_image_defaults_can_change(
    transaction,
):
    _, state, host = transaction
    live = copy.deepcopy(host.old)
    live["Id"] = "candidate-id"
    live["Image"] = r.CANDIDATE
    live["Mounts"][0].update(Source=state["candidate_data"], Type="bind")
    live["Config"]["Env"] = ["BRIDGE_TOKEN=synthetic", "PATH=/new/image/default"] + [
        key + "=" + value for key, value in r.ENV_ADDITIONS.items()
    ]
    managed = {"BRIDGE_TOKEN": "synthetic"}
    r.assert_runtime(host.old, live, state["candidate_data"], r.CANDIDATE, managed)
    managed["CUSTOM_SERVICE_FLAG"] = "must-stay"
    with pytest.raises(r.Refused, match="Managed environment"):
        r.assert_runtime(host.old, live, state["candidate_data"], r.CANDIDATE, managed)


def test_explicit_rollback_can_recover_interrupted_pre_cutover_stop(transaction):
    folder, state, host = transaction
    state["stop_intent"] = True
    state["phase"] = "quiescing_original"
    host.current["State"]["Running"] = False
    r.rollback(folder, state, host)
    assert host.current["Id"] == "original-id" and host.current["State"]["Running"]
    assert not any(call[0] == "up" for call in host.calls)


def test_staged_nginx_validation_runs_before_quiescing(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    names = [call[0] for call in host.calls]
    assert names.index("validate-nginx") < names.index("stop")


def test_rollback_preserves_concurrent_edit_but_restores_original_runtime(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    path = Path(state["files"]["compose"])
    path.write_bytes(b"unrelated concurrent edit must survive")
    with pytest.raises(r.Refused, match="configuration conflict"):
        r.rollback(folder, state, host)
    assert path.read_bytes() == b"unrelated concurrent edit must survive"
    assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
    assert state["phase"] == "original_runtime_recovered_config_conflict"


def test_unknown_rollback_container_is_never_stopped(transaction):
    folder, state, host = transaction
    host.current["Image"] = "sha256:" + "b" * 64
    with pytest.raises(r.Refused, match="Unexpected rollback"):
        r.rollback(folder, state, host)
    assert not host.calls


def test_stop_cli_timeout_accepts_only_actual_clean_stopped_id(tmp_path, monkeypatch):
    host = r.Host(tmp_path)
    monkeypatch.setattr(host, "run", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    monkeypatch.setattr(
        host,
        "inspect",
        lambda cid: {
            "Id": cid,
            "State": {"Running": False, "ExitCode": 0, "OOMKilled": False},
        },
    )
    host.stop("exact-id")


def test_changed_prepared_artifact_refuses_before_runtime_mutation(transaction):
    folder, state, host = transaction
    (folder / "compose.after").write_bytes(b"tampered candidate")
    with pytest.raises(r.Refused, match="artifact"):
        r.deploy(folder, state, host, approval())
    assert not host.calls


def test_resolved_scope_allows_only_unused_original_volume_omission():
    before = yaml.safe_load(COMPOSE)
    before["volumes"]["unrelated-data"] = {"external": True}
    after = copy.deepcopy(before)
    after["services"][r.SERVICE] = r.candidate_service(before["services"][r.SERVICE], "/candidate")
    del after["volumes"]["llm-proxy2-grok-bridge-data"]
    r.assert_resolved_scope(before, after, "/candidate")
    del after["volumes"]["unrelated-data"]
    with pytest.raises(r.Refused, match="scope"):
        r.assert_resolved_scope(before, after, "/candidate")


def test_frozen_compose_values_prevent_second_secret_interpolation():
    original = {
        "services": {
            "bridge": {
                "environment": {"TOKEN": "synthetic$VALUE", "N": 7},
                "command": ["echo", "${SYNTHETIC}"],
            }
        }
    }
    frozen = r.literal_compose_values(original)
    assert frozen["services"]["bridge"]["environment"] == {"TOKEN": "synthetic$$VALUE", "N": 7}
    assert frozen["services"]["bridge"]["command"][1] == "$${SYNTHETIC}"
    assert original["services"]["bridge"]["environment"]["TOKEN"] == "synthetic$VALUE"


def test_new_release_default_is_requested_nas_location():
    assert r.BACKUPS.as_posix() == "/mnt/s/backups/codex-security-20261109/grok-releases"
    assert r.LEGACY_BACKUPS.as_posix() == "/var/backups/codex-grok-releases"


@pytest.mark.parametrize("mode", ["prepare", "deploy", "rollback"])
def test_nas_release_path_allowed_and_parent_itself_refused(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(r, "BACKUPS", tmp_path / "nas")
    r.validate_release_path(r.BACKUPS / "new-generation", mode)
    with pytest.raises(r.Refused, match="dedicated"):
        r.validate_release_path(r.BACKUPS, mode)


@pytest.mark.parametrize("mode", ["prepare", "deploy"])
def test_new_root_snapshot_generations_refused(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(r, "BACKUPS", tmp_path / "nas")
    monkeypatch.setattr(r, "LEGACY_BACKUPS", tmp_path / "retained-local")
    with pytest.raises(r.Refused, match="rollback-only"):
        r.validate_release_path(r.LEGACY_BACKUPS / "retained-generation", mode)


def test_legacy_rollback_path_still_allowed_without_relocating(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "BACKUPS", tmp_path / "nas")
    monkeypatch.setattr(r, "LEGACY_BACKUPS", tmp_path / "retained-local")
    r.validate_release_path(r.LEGACY_BACKUPS / "retained-generation", "rollback")
    assert not r.LEGACY_BACKUPS.exists()


def test_unmounted_nas_fails_closed(monkeypatch):
    monkeypatch.setattr(r.os.path, "ismount", lambda _: False)
    with pytest.raises(r.Refused, match="not mounted separately"):
        r.require_nas_storage()


@pytest.mark.parametrize("separate", [False, True])
def test_nas_mount_must_not_resolve_to_root_filesystem(monkeypatch, separate):
    monkeypatch.setattr(r.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p: SimpleNamespace(st_dev=2 if separate and p.as_posix() == "/mnt/s" else 1),
    )
    if separate:
        r.require_nas_storage()
    else:
        with pytest.raises(r.Refused, match="root-filesystem"):
            r.require_nas_storage()


def lifecycle_roots(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle, "LEGACY_TEST_ROOT", tmp_path / "legacy")
    monkeypatch.setattr(lifecycle, "NAS_TEST_ROOT", tmp_path / "nas")


def test_lifecycle_allows_new_named_nas_and_legacy_attempts(tmp_path, monkeypatch):
    lifecycle_roots(tmp_path, monkeypatch)
    lifecycle.validate_attempt_root(lifecycle.NAS_TEST_ROOT / "grok-final-lifecycle-20261112t0900")
    lifecycle.validate_attempt_root(lifecycle.LEGACY_TEST_ROOT / "old-ci-compatible")


@pytest.mark.parametrize(
    "suffix",
    [
        "unrelated",
        "grok-final-lifecycle-",
        "grok-final-lifecycle-UPPER",
        "nested/grok-final-lifecycle-valid",
    ],
)
def test_lifecycle_rejects_broad_or_nested_nas_paths(tmp_path, monkeypatch, suffix):
    lifecycle_roots(tmp_path, monkeypatch)
    with pytest.raises(AssertionError):
        lifecycle.validate_attempt_root(lifecycle.NAS_TEST_ROOT / suffix)


def test_lifecycle_refuses_existing_attempt_data(tmp_path, monkeypatch):
    lifecycle_roots(tmp_path, monkeypatch)
    path = lifecycle.NAS_TEST_ROOT / "grok-final-lifecycle-retained"
    path.mkdir(parents=True)
    (path / "keep.txt").write_text("prior evidence")
    with pytest.raises(AssertionError, match="never reuse"):
        lifecycle.validate_attempt_root(path)
    assert (path / "keep.txt").read_text() == "prior evidence"


MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
tmpfs /run tmpfs rw,nosuid 0 0
192.0.2.5:/disk0 /mnt/s nfs4 rw,noatime,vers=4.1 0 0
/dev/sdb1 /mnt/s/local-island ext4 rw 0 0
//192.0.2.6/share /mnt/win\\040share cifs rw 0 0
user@192.0.2.7:/srv /mnt/remote fuse.sshfs rw 0 0
192.0.2.8:/old /mnt/legacy nfs rw 0 0
"""


def warning_codes(path, mounts=MOUNTS):
    return [w["code"] for w in r.storage_warnings(Path(path), mounts)]


@pytest.mark.parametrize(
    "path,kind,point",
    [
        ("/mnt/s/backups/codex-security-20261109/grok-releases/txn", "nfs4", "/mnt/s"),
        ("/mnt/legacy", "nfs", "/mnt/legacy"),  # the mount point itself
        ("/mnt/win share/grok", "cifs", "/mnt/win share"),
        ("/mnt/remote/grok", "fuse.sshfs", "/mnt/remote"),
        ("/mnt/legacy/grok", "nfs", "/mnt/legacy"),
    ],
)
def test_storage_warning_names_network_filesystem(path, kind, point):
    found = r.storage_warnings(Path(path), MOUNTS)
    assert [w["code"] for w in found] == ["network_filesystem"]
    assert f"({kind} mounted at {point})" in found[0]["message"]


@pytest.mark.parametrize(
    "path",
    [
        "/var/backups/codex-grok-releases/txn",  # local root filesystem
        "/mnt/s/local-island/txn",  # longest prefix is a local mount inside the share
        "/mnt/sibling/txn",  # shares a string prefix with /mnt/s but is not under it
        "/run/grok",
    ],
)
def test_storage_warning_absent_for_local_filesystems(path):
    assert warning_codes(path) == []


@pytest.mark.parametrize("kind", ["smb3", "smbfs", "NFS4"])
def test_storage_warning_covers_remaining_network_types(kind):
    assert warning_codes("/share/x", f"srv:/x /share {kind} rw 0 0\n") == ["network_filesystem"]


def test_storage_warning_uses_last_mount_at_same_point():
    stacked = "srv:/x /share nfs4 rw 0 0\n/dev/sdc1 /share ext4 rw 0 0\n"
    assert warning_codes("/share/x", stacked) == []
    assert warning_codes("/share/x", "\n".join(reversed(stacked.splitlines()))) == [
        "network_filesystem"
    ]


@pytest.mark.parametrize(
    "path,warned",
    [
        ("/tmp/codex-remediate-20261109/attempt", True),
        ("/var/tmp/grok", True),
        ("/tmpfiles/grok", False),
        ("/var/tmpx/grok", False),
    ],
)
def test_storage_warning_for_temporary_directories(path, warned):
    assert warning_codes(path) == (["temporary_storage"] if warned else [])


def test_storage_warning_for_group_or_world_access(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "TEMPORARY_ROOTS", ())
    folder = tmp_path / "release"
    folder.mkdir(mode=0o700)
    (folder / "state.private.json").write_text("{}")
    (folder / "state.private.json").chmod(0o600)
    assert warning_codes(folder) == []

    (folder / "snapshot.tar").write_text("synthetic")
    (folder / "snapshot.tar").chmod(0o640)
    (folder / "candidate-data").mkdir()
    (folder / "candidate-data").chmod(0o755)
    (folder / "link").symlink_to("state.private.json")  # link modes are not meaningful
    found = r.storage_warnings(folder, MOUNTS)
    assert [w["code"] for w in found] == ["files_group_or_world_readable"]
    assert found[0]["message"].startswith("2 top-level entries")
    assert "snapshot.tar" not in found[0]["message"]

    folder.chmod(0o750)
    assert warning_codes(folder) == [
        "directory_group_or_world_accessible",
        "files_group_or_world_readable",
    ]
    assert "mode 0750" in r.storage_warnings(folder, MOUNTS)[0]["message"]


def test_storage_warning_checks_location_before_directory_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "TEMPORARY_ROOTS", (tmp_path,))
    assert warning_codes(tmp_path / "not-created-yet") == ["temporary_storage"]


def test_storage_warning_output_explains_risk_and_fix_without_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "TEMPORARY_ROOTS", ())
    secret = "synthetic-bridge-token-value"
    folder = tmp_path / "release"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    (folder / "private.env").write_text("BRIDGE_TOKEN=" + secret)
    (folder / "private.env").chmod(0o644)
    stream = io.StringIO()
    codes = r.warn_snapshot_storage(folder, f"srv:/x {tmp_path} nfs4 rw 0 0\n", stream)
    assert codes == [
        "network_filesystem",
        "directory_group_or_world_accessible",
        "files_group_or_world_readable",
    ]
    lines = stream.getvalue().splitlines()
    assert len(lines) == 3 and all(line.startswith("WARNING: ") for line in lines)
    for line in lines:
        assert "bridge token and grok.com session cookies" in line
        assert "local directory with mode 0700" in line
        assert "not blocked" in line
    assert secret not in stream.getvalue() and "private.env" not in stream.getvalue()


def test_storage_warning_defaults_to_stderr(capsys):
    assert r.warn_snapshot_storage(Path("/tmp/grok"), "") == ["temporary_storage"]
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.startswith("WARNING: Grok release snapshot")


def test_storage_warning_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "TEMPORARY_ROOTS", ())
    assert r.storage_warnings(tmp_path / "x", "garbage\n\x00 \\999 \n one two\n") == []

    def unreadable(*_):
        raise PermissionError("synthetic")

    monkeypatch.setattr(r.os, "lstat", unreadable)
    monkeypatch.setattr(Path, "read_text", unreadable)
    assert r.warn_snapshot_storage(tmp_path) == []
    monkeypatch.setattr(r, "storage_warnings", unreadable)
    assert r.warn_snapshot_storage(tmp_path) == []


def test_main_reports_storage_warnings_without_changing_outcome(tmp_path, monkeypatch, capsys):
    """A shared-storage release directory still completes and exits normally."""
    monkeypatch.setattr(r, "BACKUPS", tmp_path / "nas")
    folder = r.BACKUPS / "generation"
    folder.mkdir(parents=True, mode=0o700)
    (folder / "state.private.json").write_text(json.dumps({"stop_intent": True}))
    monkeypatch.setattr(
        Path,
        "read_text",
        lambda p, *a, **k: MOUNTS if str(p) == "/proc/mounts" else p.open().read(),
    )
    monkeypatch.setattr(r, "TEMPORARY_ROOTS", (tmp_path,))
    monkeypatch.setattr(r, "operation_lock", contextlib.nullcontext)
    monkeypatch.setattr(r, "Host", lambda _: None)
    monkeypatch.setattr(
        r, "rollback", lambda folder, state, host: state.update(phase="rolled_back", deployed=False)
    )
    real_stat = Path.stat
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, **k: SimpleNamespace(st_uid=0, st_mode=0o40700) if p == folder else real_stat(p),
    )
    monkeypatch.setattr(sys, "argv", ["release.py", "rollback", str(folder)])
    r.main()
    captured = capsys.readouterr()
    summary = json.loads(captured.out)
    assert summary["phase"] == "rolled_back"
    assert "temporary_storage" in summary["storage_warnings"]
    assert captured.err.count("WARNING: Grok release") == len(summary["storage_warnings"])
