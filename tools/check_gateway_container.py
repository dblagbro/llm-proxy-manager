"""Check non-root startup and volume reuse using only a locally available gateway image."""

import argparse
import json
import secrets
import subprocess
import time
from contextlib import ExitStack

PROBE = """
import json, os, sqlite3, sys
from pathlib import Path
from urllib.request import urlopen
health = json.load(urlopen("http://127.0.0.1:3000/health", timeout=2))
assert health["status"] in {"healthy", "degraded"} and health["totalProviders"] == 0
with sqlite3.connect("file:/app/data/llmproxy.db?mode=ro", uri=True) as db:
    admin_id = db.execute("SELECT id FROM users WHERE username = 'admin'").fetchone()[0]
marker = Path("/app/logs/portable-volume-check")
if sys.argv[1] == "initial":
    marker.write_text(admin_id)
else:
    assert marker.read_text() == admin_id
print(json.dumps([os.getuid(), os.getgid(), health["version"], admin_id]))
"""


def docker(*args, check=True):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if check and result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed: {result.stderr[-300:]}")
    return result


def probe(container, phase):
    deadline = time.monotonic() + 90
    while True:
        result = docker("exec", container, "python", "-c", PROBE, phase, check=False)
        if result.returncode == 0:
            return json.loads(result.stdout)
        running = docker("inspect", "-f", "{{.State.Running}}", container).stdout.strip()
        if running != "true" or time.monotonic() >= deadline:
            raise RuntimeError(f"{phase} startup or volume verification failed")
        time.sleep(0.5)


def check_image(image, uid, gid):
    image_id = docker("image", "inspect", "-f", "{{.Id}}", image).stdout.strip()
    secret = secrets.token_hex(32)
    volume_names = set()

    with ExitStack() as cleanup:

        def create(*mounts):
            container = docker(
                "create",
                "--pull=never",
                "--network",
                "none",
                "--restart=no",
                "--cpus",
                "2",
                "--memory",
                "2g",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "-e",
                f"SECRET_KEY={secret}",
                *mounts,
                image_id,
            ).stdout.strip()
            cleanup.callback(docker, "rm", "-f", "-v", container)
            info = json.loads(docker("inspect", container).stdout)[0]
            assert info["Image"] == image_id
            assert info["HostConfig"]["NetworkMode"] == "none"
            assert {m["Destination"] for m in info["Mounts"]} == {"/app/data", "/app/logs"}
            assert len(info["Mounts"]) == 2 and all(
                m["Type"] == "volume" and m["RW"] for m in info["Mounts"]
            )
            return container, {m["Name"] for m in info["Mounts"]}

        first, volume_names = create("-v", "/app/data", "-v", "/app/logs")
        docker("start", first)
        initial = probe(first, "initial")
        assert initial[:2] == [uid, gid], "image runtime ownership differs from expected UID/GID"
        docker("stop", "-t", "15", first)

        # Keep the original stopped until the replacement releases its anonymous volumes.
        replacement, reused = create("--volumes-from", first)
        assert reused == volume_names
        docker("start", replacement)
        assert probe(replacement, "replacement") == initial

    remaining = set(docker("volume", "ls", "-q").stdout.splitlines())
    assert not remaining.intersection(volume_names), "fixture volume cleanup incomplete"
    print(
        f"Image {image_id} passed startup and replacement at {uid}:{gid}. "
        "Volume persistence and cleanup passed."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--uid", type=int, default=640)
    parser.add_argument("--gid", type=int, default=640)
    args = parser.parse_args()
    check_image(args.image, args.uid, args.gid)
