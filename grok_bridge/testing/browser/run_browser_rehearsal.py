"""Run under capacity_guard; only synthetic state and an isolated network."""

import datetime
import json
import os
import subprocess
import time
from pathlib import Path

UTC = datetime.timezone.utc  # noqa: UP017 - host operator also runs on Python 3.10
W = Path(__file__).resolve().parent
assert os.geteuid() == 0
assert b"capacity_guard.py" in Path(f"/proc/{os.getppid()}/cmdline").read_bytes()
assert not (W / "state.json").exists(), "Inspect the prior rehearsal before retrying"
BRIDGE = (
    os.environ.get("GROK_TEST_IMAGE")
    or "sha256:587311da177cc015292981a01649226c8b909e8310d74fdbab481175cb2b16e9"
)
GATEWAY = (
    os.environ.get("GROK_TEST_GATEWAY_IMAGE")
    or "sha256:233b7c197bf012671079d0efe968b6b37b66cfd008c3fd2cdcbfc93c4e493932"
)
NGINX = (
    os.environ.get("GROK_TEST_NGINX_IMAGE")
    or "sha256:67a10554437ff2017327a3ebb36c8a779f38a7f67f861980109c1cd81e133c4a"
)
for image in (BRIDGE, GATEWAY, NGINX):
    assert image.startswith("sha256:") and len(image) == 71
    assert all(c in "0123456789abcdef" for c in image[7:]), "Use verified immutable image IDs"
    assert (
        subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True
        ).strip()
        == image
    ), "Required immutable image is not present locally"
NET = "codex-grok-browser-network"
AUTH = "codex-grok-browser-auth"
APP = "codex-grok-browser-app"
PROXY = "codex-grok-browser-proxy"
CLIENT = "codex-grok-browser-client"
names = (CLIENT, PROXY, APP, AUTH)
owned = []
network_id = None
state = {
    "started_utc": datetime.datetime.now(UTC).isoformat(),
    "production_modified": False,
    "real_account_used": False,
    "images": {"bridge": BRIDGE, "gateway_auth": GATEWAY, "nginx": NGINX},
}


def save(phase):
    state.update(phase=phase, updated_utc=datetime.datetime.now(UTC).isoformat())
    (W / "state.json").write_text(json.dumps(state, indent=2) + "\n")


def out(args, timeout=180):
    return subprocess.check_output(args, text=True, stderr=subprocess.STDOUT, timeout=timeout)


def owned_run(args):
    container_id = out(args).strip()
    assert len(container_id) == 64 and all(c in "0123456789abcdef" for c in container_id)
    owned.append(container_id)
    return container_id


def ready(name, url):
    until = time.monotonic() + 180
    while time.monotonic() < until:
        running = out(["docker", "inspect", "--format", "{{.State.Running}}", name]).strip()
        assert running == "true", name + " exited before readiness"
        try:
            p = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "python",
                    "-c",
                    "import urllib.request; "
                    f"assert urllib.request.urlopen({url!r},timeout=2).status==200",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            # Docker exec may wait on daemon/storage work before Python starts.
            # A timeout does not establish app failure; retain a bounded deadline.
            time.sleep(1)
            continue
        if p.returncode == 0:
            return
        time.sleep(1)
    raise AssertionError(name + " not healthy")


for name in names:
    assert (
        subprocess.run(
            ["docker", "inspect", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        ).returncode
        != 0
    ), "Existing test container " + name
assert (
    subprocess.run(
        ["docker", "network", "inspect", NET],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode
    != 0
), "Existing test network; inspect it before retrying"
try:
    save("preparing_synthetic_tls_and_data")
    for name in ("tls", "data", "profile", "evidence"):
        (W / name).mkdir(exist_ok=True)
    uid, gid = json.loads(
        out(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--entrypoint",
                "python",
                GATEWAY,
                "-c",
                "import os,json;print(json.dumps([os.getuid(),os.getgid()]))",
            ]
        )
    )
    state["auth_fixture_uid"] = uid
    out(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "2",
            "-subj",
            "/CN=operator.example.test",
            "-addext",
            "subjectAltName=DNS:operator.example.test",
            "-keyout",
            str(W / "tls/key.pem"),
            "-out",
            str(W / "tls/cert.pem"),
        ]
    )
    network_id = out(["docker", "network", "create", "--internal", NET]).strip()
    owned_run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            AUTH,
            "--network",
            NET,
            "--network-alias",
            "llm-proxy2",
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--entrypoint",
            "python",
            "-v",
            str(W / "gateway_auth_fixture.py") + ":/fixture/gateway_auth_fixture.py:ro",
            "--tmpfs",
            f"/test-data:rw,size=64m,mode=0700,uid={uid},gid={gid}",
            "-e",
            "DATABASE_URL=sqlite+aiosqlite:////test-data/auth.db",
            "-e",
            "SESSION_COOKIE_SECURE=true",
            "-e",
            "SESSION_COOKIE_PATH=/",
            GATEWAY,
            "-m",
            "uvicorn",
            "gateway_auth_fixture:app",
            "--app-dir",
            "/fixture",
            "--host",
            "0.0.0.0",
            "--port",
            "3000",
            "--no-access-log",
        ]
    )
    ready(AUTH, "http://127.0.0.1:3000/health")
    owned_run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            APP,
            "--network",
            NET,
            "--network-alias",
            "llm-proxy2-grok-bridge",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "-v",
            str(W / "profile") + ":/data/playwright-state",
            "-e",
            "BRIDGE_TOKEN=synthetic-bridge-test-value",
            "-e",
            "BRIDGE_PUBLIC_PATH=/grok-bridge",
            "-e",
            "BRIDGE_PUBLIC_ORIGIN=https://operator.example.test",
            "-e",
            "BRIDGE_AUTH_URL=http://llm-proxy2:3000/api/auth/me",
            BRIDGE,
        ]
    )
    ready(APP, "http://127.0.0.1:8443/healthz")
    proxy_args = [
        "--network",
        NET,
        "-v",
        str(W) + ":/fixture:ro",
        "-v",
        str(W / "nginx.conf") + ":/etc/nginx/nginx.conf:ro",
    ]
    out(["docker", "run", "--rm", *proxy_args, NGINX, "nginx", "-t"])
    owned_run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            PROXY,
            *proxy_args,
            "--network-alias",
            "operator.example.test",
            "--memory",
            "128m",
            NGINX,
        ]
    )
    save("browser_acceptance_running")
    client_id = owned_run(
        [
            "docker",
            "create",
            "--name",
            CLIENT,
            "--network",
            NET,
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--entrypoint",
            "python",
            "-v",
            str(W / "browser_acceptance.py") + ":/test.py:ro",
            "-v",
            str(W / "evidence") + ":/evidence",
            BRIDGE,
            "/test.py",
        ]
    )
    with (W / "browser.log").open("w") as log:
        result = subprocess.run(
            ["docker", "start", "--attach", client_id],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=240,
        )
    state["browser_exit"] = result.returncode
    if (W / "evidence/browser-result.json").exists():
        state["browser"] = json.loads((W / "evidence/browser-result.json").read_text())
    assert result.returncode == 0 and state["browser"]["passed"], (
        "Browser acceptance failed; inspect retained evidence"
    )
    save("browser_acceptance_passed_not_deployed")
except Exception as exc:
    state["error"] = str(exc)
    save("browser_acceptance_failed")
    raise
finally:
    for container_id in reversed(owned):
        if (
            subprocess.run(
                ["docker", "inspect", container_id],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            with (W / (container_id + ".log")).open("w") as log:
                subprocess.run(
                    ["docker", "logs", container_id], stdout=log, stderr=subprocess.STDOUT
                )
            subprocess.run(
                ["docker", "rm", "-f", container_id], check=True, stdout=subprocess.DEVNULL
            )
    if network_id:
        subprocess.run(
            ["docker", "network", "rm", network_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
