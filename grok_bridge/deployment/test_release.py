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
import test_network_transaction as network_fixture
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
        "network_plan_reviewed": True,
        "network_recovery_acceptance_passed": True,
        "network_outsider_denial_passed": True,
        "multi_network_nginx_validation_passed": True,
        "operator_deployment_approved": True,
        "source_commit": "a" * 40,
        "gate_evidence_reference": "synthetic-test-evidence",
    }


def test_gateway_waiver_cannot_imply_grok_gate():
    evidence = approval()
    evidence["grok_release_gate_satisfied"] = False
    evidence["gateway_waiver"] = "5.22.54 only"
    with pytest.raises(r.Refused, match="grok_release_gate"):
        r.gate(evidence)


ORIGINAL_ID = "1" * 64
CANDIDATE_ID = "2" * 64
ROLLBACK_ID = "3" * 64
NGINX_ID = network_fixture.checksum("nginx")
DEFAULT_ID = network_fixture.checksum("original-network")


class ReleaseNetworkHost(network_fixture.FakeHost):
    """Use the real journal with synthetic adapters; verify on-disk intent."""

    def __init__(self, owner, folder):
        super().__init__()
        self.owner, self.folder = owner, folder
        self.enabled = False
        self.allowed_bridge = None
        self.allowed_consumers = None

    def container(self, name):
        if name == r.SERVICE:
            current = self.owner.current
            assert current is not None and current["State"]["Running"]
            return {
                "name": name,
                "id": current["Id"],
                "config_sha256": network_fixture.checksum("original-grok-config"),
                "default_route_sha256": network_fixture.checksum("original-grok-route"),
                "networks": {
                    r.network_policy.DEFAULT_NAME: {
                        "id": DEFAULT_ID,
                        "attachment_sha256": network_fixture.checksum("original-grok-endpoint"),
                        "aliases": [name],
                        "gw_priority": 0,
                    }
                },
            }
        result = super().container(name)
        if self.allowed_consumers:
            assert result["id"] == self.allowed_consumers[name]
        return result

    def _intent(self):
        assert self.enabled
        disk = json.loads((self.folder / "state.private.json").read_text())
        assert disk["network_intent"]
        self.journal = [disk["network_transaction"]]

    def create_network(self, *args):
        self._intent()
        self.owner.calls.append(("network-create",))
        return super().create_network(*args)

    def connect(self, *args):
        self._intent()
        self.owner.calls.append(("network-connect", args[2]))
        return super().connect(*args)

    def disconnect(self, *args):
        self._intent()
        assert self.owner.current["Image"] == r.ORIGINAL
        assert self.owner.current["State"]["Running"]
        self.owner.calls.append(("network-disconnect", args[1]))
        return super().disconnect(*args)

    def network(self, name):
        value = super().network(name)
        if value and r.SERVICE in value["members"].values():
            cid = next(cid for cid, role in value["members"].items() if role == r.SERVICE)
            assert cid == self.allowed_bridge
        return value


class FakeHost:
    def __init__(self, old, candidate_data):
        self.old = copy.deepcopy(old)
        self.current = copy.deepcopy(old)
        self.candidate_data = str(candidate_data)
        self.calls = []
        self.stop_error = False
        self.up_timeout = False
        self.up_disappears = False
        self.network_host = None

    def network_adapter(self, state=None, *, mutate=False):
        adapter = self.network_host
        adapter.enabled = mutate
        if state is not None:
            journal = state["network_transaction"]
            adapter.allowed_bridge = journal["bridge_id"]
            adapter.allowed_consumers = {
                name: record["id"] for name, record in journal["consumers"].items()
            }
        return adapter

    def inspect(self, name):
        return self.current if name != "nginx" else {"Id": NGINX_ID}

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
        return {"/nginx": [NGINX_ID, "running"]}

    def exclusive_volume(self, source, owner):
        assert owner == ORIGINAL_ID

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
        if self.network_host.network_value:
            self.network_host.network_value["members"].pop(CANDIDATE_ID, None)
        if Path(path).name == "original-resolved.json":
            self.current["Id"] = ROLLBACK_ID
        else:
            if self.up_disappears:
                self.current = None
                raise TimeoutError("original removed; candidate create not completed")
            self.current["Id"] = CANDIDATE_ID
            self.current["Image"] = r.CANDIDATE
            self.current["HostConfig"]["NetworkMode"] = r.network_policy.NETWORK_NAME
            self.current["NetworkSettings"]["Networks"] = {
                r.network_policy.NETWORK_NAME: {"NetworkID": self.network_host.network_value["id"]}
            }
            self.network_host.network_value["members"][CANDIDATE_ID] = r.SERVICE
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
        "Id": ORIGINAL_ID,
        "Image": r.ORIGINAL,
        "State": {"Running": True},
        "RestartCount": 0,
        "HostConfig": {"NetworkMode": r.network_policy.DEFAULT_NAME},
        "NetworkSettings": {"Networks": {r.network_policy.DEFAULT_NAME: {"NetworkID": DEFAULT_ID}}},
        "Config": {
            "Env": ["BRIDGE_TOKEN=synthetic", "BRIDGE_PUBLIC_PATH=/grok-bridge"],
            "Labels": {
                "com.docker.compose.project": "docker",
                "com.docker.compose.service": r.SERVICE,
            },
        },
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
    (tmp_path / "nginx-inspect.json").write_text(json.dumps({"Id": NGINX_ID}))
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
        "others": {"/nginx": [NGINX_ID, "running"]},
        "stop_intent": False,
        "cutover_intent": False,
        "deployed": False,
    }
    state["artifacts"] = {
        p.name: r.file_digest(p) for p in tmp_path.iterdir() if not p.name.endswith(".live")
    }
    host = FakeHost(old, data)
    host.network_host = ReleaseNetworkHost(host, tmp_path)
    r.prepare_network(tmp_path, state, host)

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
    assert ("recover-original", ORIGINAL_ID) in host.calls
    assert host.current["State"]["Running"] and host.current["Id"] == ORIGINAL_ID
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
    assert host.current["State"]["Running"] and host.current["Id"] == ORIGINAL_ID
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
    assert state["candidate_id"] == CANDIDATE_ID and state["phase"] == "rolled_back"
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
    live["Id"] = CANDIDATE_ID
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
    assert host.current["Id"] == ORIGINAL_ID and host.current["State"]["Running"]
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
    assert r.BACKUPS.as_posix() == "/mnt/s/backups/codex-private-security-20261109/grok-releases"
    assert r.LEGACY_NAS_BACKUPS.as_posix() == "/mnt/s/backups/codex-security-20261109/grok-releases"
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


@pytest.mark.parametrize("mode", ["prepare", "deploy"])
def test_old_nas_private_release_path_is_rollback_only(tmp_path, monkeypatch, mode):
    monkeypatch.setattr(r, "BACKUPS", tmp_path / "private-nas")
    monkeypatch.setattr(r, "LEGACY_NAS_BACKUPS", tmp_path / "old-checkpoint-tree")
    with pytest.raises(r.Refused, match="rollback-only"):
        r.validate_release_path(r.LEGACY_NAS_BACKUPS / "retained-generation", mode)
    r.validate_release_path(r.LEGACY_NAS_BACKUPS / "retained-generation", "rollback")
    assert not r.LEGACY_NAS_BACKUPS.exists()


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


def network_compose_fixture():
    raw = (
        COMPOSE.replace(
            b"services:\n",
            b"services:\n  nginx:\n    image: nginx:synthetic\n"
            b"  llm-proxy2:\n    image: gateway:synthetic\n",
            1,
        )
        + b"networks:\n  default:\n"
    )
    resolved = yaml.safe_load(raw)
    for service in resolved["services"].values():
        service["networks"] = {"default": None}
    resolved["networks"] = {"default": {"name": "docker_default", "ipam": {}}}
    return raw, resolved


def test_combined_renderer_has_only_reviewed_image_data_environment_network_delta():
    raw, before = network_compose_fixture()
    result = r.render_network_compose(raw, before, "/private/candidate-data")
    after = yaml.safe_load(result)
    # Model only the exact Compose defaults established by read-only acceptance.
    for service in after["services"].values():
        service.setdefault("networks", {"default": None})
    after["networks"]["default"] = copy.deepcopy(before["networks"]["default"])
    new = after["networks"]["grok_security"]
    new.pop("internal")
    new["ipam"] = {}
    r.assert_network_resolved_scope(before, after, "/private/candidate-data")
    assert after["services"]["other"] == before["services"]["other"]
    assert b"unrelated:tag # concurrent owner" in result
    assert set(after["services"][r.SERVICE]["networks"]) == {"grok_security"}
    after["services"]["other"]["image"] = "unreviewed"
    with pytest.raises(r.Refused, match="scope"):
        r.assert_network_resolved_scope(before, after, "/private/candidate-data")


def test_combined_renderer_rejects_duplicates_before_service_yaml_rewrite():
    raw, before = network_compose_fixture()
    raw = raw.replace(
        b"    restart: unless-stopped", b"    restart: no\n    restart: unless-stopped"
    )
    with pytest.raises(r.network_compose.ComposeNetworkError, match="Ambiguous"):
        r.render_network_compose(raw, before, "/private/candidate-data")


@pytest.mark.parametrize("kind", ["extra-network", "old-ipam", "false-zero", "new-ipam"])
def test_combined_scope_refuses_unreviewed_network_defaults(kind):
    _, before = network_compose_fixture()
    after, _ = r.network_policy.plan_networks(before)
    after["services"][r.SERVICE] = r.candidate_service(after["services"][r.SERVICE], "/candidate")
    if kind == "extra-network":
        after["services"][r.SERVICE]["networks"]["default"] = None
    elif kind == "old-ipam":
        after["networks"]["default"]["internal"] = False
    elif kind == "false-zero":
        after["networks"]["grok_security"]["internal"] = 0
    else:
        after["networks"]["grok_security"]["ipam"] = {"driver": "default"}
    with pytest.raises(r.Refused):
        r.assert_network_resolved_scope(before, after, "/candidate")


@pytest.mark.parametrize(
    "field",
    [
        "network_plan_reviewed",
        "network_recovery_acceptance_passed",
        "network_outsider_denial_passed",
        "multi_network_nginx_validation_passed",
        "operator_deployment_approved",
    ],
)
def test_network_and_operator_acceptance_are_explicit_gates(transaction, field):
    folder, state, host = transaction
    evidence = approval()
    evidence.pop(field)
    with pytest.raises(r.Refused, match=field):
        r.deploy(folder, state, host, evidence)
    assert not host.calls


def test_network_attach_and_durable_intents_precede_original_stop(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    names = [call[0] for call in host.calls]
    assert names.count("network-connect") == 2
    assert max(i for i, name in enumerate(names) if name == "network-connect") < names.index("stop")
    saved = r.load_state(folder)
    assert saved["network_transaction"]["bridge_id"] == CANDIDATE_ID
    assert saved["candidate_id"] == CANDIDATE_ID
    assert saved["network_transaction"]["phase"] == "attached"


def test_pre_stop_attach_failure_recovers_network_without_stopping_original(transaction):
    folder, state, host = transaction
    host.network_host.behavior["connect-llm-proxy2"] = "before"
    with pytest.raises(r.network_transaction.TransactionError):
        r.deploy(folder, state, host, approval())
    assert not state["stop_intent"]
    assert host.current["Id"] == ORIGINAL_ID and host.current["State"]["Running"]
    assert not any(call[0] in {"stop", "up", "recover-original"} for call in host.calls)
    assert state["phase"] == "pre_stop_failed_network_recovered"
    assert state["network_transaction"]["phase"] == "rolled_back"
    assert host.network_host.network_value["members"] == {}


def test_new_controller_recovers_post_effect_attachment_crash_without_grok_stop(transaction):
    folder, state, host = transaction
    state["network_intent"] = True
    r.save(folder, state)
    host.network_host.behavior["connect-nginx"] = "crash"
    with pytest.raises(network_fixture.Crash):
        r.network_engine(folder, state, host, mutate=True).attach_consumers()
    saved = r.load_state(folder)
    assert saved["network_transaction"]["consumers"]["nginx"]["phase"] == "connecting"
    host.network_host.behavior.clear()
    r.rollback(folder, saved, host)
    assert saved["phase"] == "network_only_rolled_back"
    assert host.network_host.network_value["members"] == {}
    assert not any(call[0] in {"stop", "up", "recover-original"} for call in host.calls)


def test_new_controller_reconciles_create_intent_before_missing_id_checkpoint(transaction):
    folder, state, host = transaction
    state["network_intent"] = True
    r.save(folder, state)
    host.network_host.behavior["create"] = "crash"
    with pytest.raises(network_fixture.Crash):
        r.network_engine(folder, state, host, mutate=True).attach_consumers()
    saved = r.load_state(folder)
    assert saved["network_transaction"]["network"] == {
        "phase": "creating",
        "id": None,
        "proof": None,
    }
    observed_id = host.network_host.network_value["id"]
    host.network_host.behavior.clear()
    r.rollback(folder, saved, host)
    assert saved["phase"] == "network_only_rolled_back"
    assert saved["network_transaction"]["network"]["id"] == observed_id
    assert saved["network_transaction"]["network"]["proof"] == "creation_intent_reconciled"
    assert host.network_host.network_value["members"] == {}
    assert [call[0] for call in host.calls] == ["network-create"]


def test_create_intent_recovery_refuses_foreign_replacement_network(transaction):
    folder, state, host = transaction
    state["network_intent"] = True
    r.save(folder, state)
    host.network_host.behavior["create"] = "crash"
    with pytest.raises(network_fixture.Crash):
        r.network_engine(folder, state, host, mutate=True).attach_consumers()
    saved = r.load_state(folder)
    host.network_host.network_value["id"] = "f" * 64
    host.network_host.network_value["transaction_id"] = "f" * 32
    before = copy.deepcopy(host.network_host.network_value)
    with pytest.raises(r.network_transaction.TransactionError, match="ownership"):
        r.rollback(folder, saved, host)
    assert host.network_host.network_value == before
    assert [call[0] for call in host.calls] == ["network-create"]
    assert host.current["Id"] == ORIGINAL_ID and host.current["State"]["Running"]


def test_drifted_consumer_route_refuses_automatic_detach_and_preserves_original(transaction):
    folder, state, host = transaction
    host.network_host.behavior["connect-nginx"] = "route-drift"
    with pytest.raises(r.network_transaction.TransactionError, match="route drift"):
        r.deploy(folder, state, host, approval())
    assert host.current["Id"] == ORIGINAL_ID and host.current["State"]["Running"]
    assert not any(call[0] in {"stop", "up", "network-disconnect"} for call in host.calls)
    assert host.network_host.network_value["members"] == {NGINX_ID: "nginx"}


def test_private_journal_cas_does_not_overwrite_concurrent_owner_edit(transaction):
    folder, state, host = transaction
    path = folder / "state.private.json"
    path.write_text('{"concurrent":"retained"}')
    with pytest.raises(r.Refused, match="journal CAS"):
        r.deploy(folder, state, host, approval())
    assert path.read_text() == '{"concurrent":"retained"}'
    assert not host.network_host.actions and host.current["State"]["Running"]


def test_failed_atomic_journal_replace_preserves_last_recovery_state(transaction, monkeypatch):
    folder, state, host = transaction
    path = folder / "state.private.json"
    original = path.read_bytes()
    monkeypatch.setattr(r.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("synthetic")))
    with pytest.raises(OSError):
        r.deploy(folder, state, host, approval())
    assert path.read_bytes() == original
    assert r.load_state(folder)["phase"] == "prepared_not_deployed"
    assert not host.network_host.actions and host.current["State"]["Running"]


@pytest.mark.parametrize("operation", ["save", "load"])
def test_private_journal_rejects_symlink_even_when_target_is_absent(
    tmp_path, monkeypatch, operation
):
    monkeypatch.setattr(Path, "is_symlink", lambda _: True)
    with pytest.raises(r.Refused, match="regular"):
        if operation == "save":
            r.save(tmp_path, {"phase": "prepared_not_deployed"})
        else:
            r.load_state(tmp_path)
    assert not list(tmp_path.iterdir())


def test_original_network_or_config_drift_refuses_before_attach(transaction):
    folder, state, host = transaction
    host.current["Config"]["Env"].append("CONCURRENT=preserve")
    with pytest.raises(r.Refused, match="Original runtime changed"):
        r.deploy(folder, state, host, approval())
    assert not host.network_host.actions and not host.calls


def test_legacy_preparation_cannot_silently_skip_network_gate(transaction):
    folder, state, host = transaction
    del state["network_contract"]
    with pytest.raises(r.Refused, match="new transaction"):
        r.deploy(folder, state, host, approval())
    assert not host.network_host.actions


@pytest.mark.parametrize("contract", [None, 2, True, "1"])
def test_malformed_network_schema_never_falls_back_to_legacy_rollback(transaction, contract):
    folder, state, host = transaction
    if contract is None:
        state.pop("network_contract")
    else:
        state["network_contract"] = contract
    with pytest.raises(r.Refused, match="network preparation"):
        r.rollback(folder, state, host)
    assert not host.calls and not host.network_host.actions


def test_genuine_legacy_journal_retains_original_rollback_path(transaction):
    folder, state, host = transaction
    for marker in (
        "network_contract",
        "network_transaction",
        "network_intent",
        "original_network_projection",
    ):
        state.pop(marker)
    state["stop_intent"] = True
    host.current["State"]["Running"] = False
    r.rollback(folder, state, host)
    assert state["phase"] == "rolled_back"
    assert host.current["Image"] == r.ORIGINAL and host.current["State"]["Running"]
    assert not host.network_host.actions


def test_invalid_private_journal_json_has_sanitized_error(tmp_path):
    secret = "synthetic-private-value-must-not-leak"
    (tmp_path / "state.private.json").write_text('{"secret":"' + secret)
    with pytest.raises(r.Refused, match="Invalid private journal JSON") as captured:
        r.load_state(tmp_path)
    assert secret not in str(captured.value)


def test_config_conflict_after_cutover_retains_consumer_attachments(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    Path(state["files"]["compose"]).write_bytes(b"preserve concurrent network plan")
    with pytest.raises(r.Refused, match="configuration conflict"):
        r.rollback(folder, state, host)
    assert state["phase"] == "original_runtime_recovered_config_conflict"
    assert host.current["Image"] == r.ORIGINAL
    assert set(host.network_host.network_value["members"].values()) == {"nginx", "llm-proxy2"}
    assert not any(call[0] == "network-disconnect" for call in host.calls)


def test_rollback_reconciles_candidate_binding_interruption(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    state["network_transaction"]["bridge_id"] = None
    r.save(folder, state)
    loaded = r.load_state(folder)
    r.rollback(folder, loaded, host)
    assert loaded["phase"] == "rolled_back"
    assert loaded["network_transaction"]["bridge_id"] == CANDIDATE_ID
    assert host.network_host.network_value["members"] == {}


def test_rollback_id_is_durable_before_reload_failure(transaction, monkeypatch):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    monkeypatch.setattr(host, "reload_nginx", lambda *_: (_ for _ in ()).throw(r.Refused("reload")))
    with pytest.raises(r.Refused, match="reload"):
        r.rollback(folder, state, host)
    loaded = r.load_state(folder)
    assert loaded["rollback_id"] == host.current["Id"] == ROLLBACK_ID
    assert loaded["phase"] == "original_runtime_recovered"
    monkeypatch.setattr(host, "reload_nginx", lambda *_: None)
    r.rollback(folder, loaded, host)
    assert loaded["phase"] == "rolled_back"


def test_wrong_dedicated_network_never_authorizes_candidate_stop(transaction):
    folder, state, host = transaction
    r.deploy(folder, state, host, approval())
    state["network_transaction"]["bridge_id"] = None
    host.current["NetworkSettings"]["Networks"][r.network_policy.NETWORK_NAME]["NetworkID"] = (
        "f" * 64
    )
    r.save(folder, state)
    previous_stops = sum(call[0] == "stop" for call in host.calls)
    with pytest.raises(r.Refused, match="Candidate network"):
        r.rollback(folder, state, host)
    assert sum(call[0] == "stop" for call in host.calls) == previous_stops


def test_host_run_pins_local_docker_socket(tmp_path, monkeypatch):
    seen = []

    def fake_run(args, **kwargs):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout=b"synthetic", stderr=b"")

    monkeypatch.setattr(r.subprocess, "run", fake_run)
    monkeypatch.setenv("DOCKER_HOST", "tcp://unreviewed.example:2375")
    r.Host(tmp_path).run(["docker", "version"])
    assert seen == [["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "version"]]
