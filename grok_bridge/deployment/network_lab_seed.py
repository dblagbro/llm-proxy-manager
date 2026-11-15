"""Pure fixed seed-helper recipes, not a launcher, journal or admission decision.

The proposed /usr/bin/python3 must still be verified in the original image pin;
the maintained Dockerfile does not prove that image's interpreter compatibility.
Empty Env/Volumes declarations do not erase inherited image configuration. Actual
image volumes/environment, resolved mounts and helper configuration require fresh
admission checks. NoCopy suppresses copy-up, not same-name reuse or implicit volume
creation. Ordinary local volumes have no immutable Docker object ID.

Retain both helpers after execution. The working helper remains a consumer of the
original volume even when stopped, conflicting with current exclusive_volume.
A future lab-only exact-ID allowance needs separate review; production exclusivity
is unchanged. Auxiliary v1 tracks neither helper lifecycle nor the working volume.
Its events must not silently acquire those meanings.

The future controller must persist each create/start intent before its one-shot
request, retain ambiguous outcomes, and preserve independently established control
and live backing-directory/mount continuity. Engine ID alone does not prove daemon
continuity: boot, socket/peer and daemon-process identity matter too. Script exit 0
and its report are not controller acceptance, freshness, provenance or permission.
A complete-looking report may escape before a later stdout/flush failure; its
bytes cannot be retracted and are unacknowledged without the exact helper's
terminal success plus independent seed/continuity reinspection. Filesystem calls
have bounded content but no OS-level wall-clock deadline in this script.
No transport, CLI, persistence, image execution or filesystem operation occurs in
seed_helper_recipe. The embedded script writes only its fixed synthetic volume
leaf when separately admitted; it never changes permissions or removes evidence.
"""

import base64
import copy
import hashlib
import json

import network_lab_auxiliary as auxiliary
import network_lab_capacity as capacity
import network_lab_fixtures as fixtures

SCHEMA = "grok-network-lab-seed-helper-recipe-v1"
PYTHON = "/usr/bin/python3"
MAX_SEED_BYTES = 4096
MAX_SCRIPT_BYTES = 24576
_SCRIPT = r'''"""Fixed synthetic seed only; reports are not controller acceptance."""
import base64
import hashlib
import json
import os
import stat
import sys

PAYLOAD = json.loads(__PAYLOAD__)
SUCCESS = ('{"schema":"grok-lab-seed-result-v1","status":"script_completed",'
           '"execution_admission":false,"compatibility_verified":false}\n')
FAILURE = ('{"schema":"grok-lab-seed-result-v1","status":"refused","code":"seed_refused",'
           '"execution_admission":false,"compatibility_verified":false}\n')


class Refused(RuntimeError):
    pass


def require(condition):
    if not condition:
        raise Refused("Synthetic seed refused")


def payload():
    require(type(PAYLOAD) is dict and set(PAYLOAD) == {"name", "data", "size", "sha256"})
    require(type(PAYLOAD["name"]) is str and PAYLOAD["name"] in
            {"fixture-generation.txt", "reference-generation.txt"})
    require(type(PAYLOAD["size"]) is int and 0 < PAYLOAD["size"] <= 4096)
    require(type(PAYLOAD["data"]) is str and len(PAYLOAD["data"]) <= 5464)
    require(type(PAYLOAD["sha256"]) is str and len(PAYLOAD["sha256"]) == 64)
    data = base64.b64decode(PAYLOAD["data"], validate=True)
    require(len(data) == PAYLOAD["size"] and hashlib.sha256(data).hexdigest() == PAYLOAD["sha256"])
    return PAYLOAD["name"], data


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)


def leaf_identity(info):
    return (*identity(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def directory(info):
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022)


def private(info):
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_gid == 0
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1)


def members(fd):
    names = set()
    with os.scandir(fd) as entries:
        for entry in entries:
            require(len(names) < 1)
            names.add(entry.name)
    return names


def seed():
    name, data = payload()
    require(sys.platform.startswith("linux") and os.geteuid() == 0 and os.getegid() == 0)
    descriptors = []
    error = None
    try:
        root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(root)
        root_info = os.fstat(root)
        directory(root_info)
        before = os.stat("data", dir_fd=root, follow_symlinks=False)
        directory(before)
        volume = os.open("data", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=root)
        descriptors.append(volume)
        directory(os.fstat(volume))
        require(identity(os.fstat(volume)) == identity(before))
        require(not os.fstatvfs(volume).f_flag & os.ST_RDONLY)

        def guard(expected, writer=None, retained=None):
            require(identity(os.fstat(root)) == identity(root_info))
            require(identity(os.stat("/", follow_symlinks=False)) == identity(root_info))
            require(identity(os.fstat(volume)) == identity(before))
            require(identity(os.stat("data", dir_fd=root, follow_symlinks=False))
                    == identity(before))
            require(members(volume) == expected)
            if writer is not None:
                current = os.fstat(writer)
                private(current)
                path = os.stat(name, dir_fd=volume, follow_symlinks=False)
                private(path)
                require(identity(current) == retained and identity(path) == retained)

        guard(set())
        writer = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                         os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=volume)
        descriptors.append(writer)
        current = os.fstat(writer)
        private(current)
        retained = identity(current)
        offset = 0
        while offset < len(data):
            guard({name}, writer, retained)
            written = os.write(writer, data[offset:])
            require(type(written) is int and 0 < written <= len(data) - offset)
            offset += written
        guard({name}, writer, retained)
        os.fsync(writer)
        frozen = leaf_identity(os.fstat(writer))
        require(os.fstat(writer).st_size == len(data))

        def readback():
            guard({name}, writer, retained)
            require(leaf_identity(os.fstat(writer)) == frozen)
            before_read = os.stat(name, dir_fd=volume, follow_symlinks=False)
            private(before_read)
            require(leaf_identity(before_read) == frozen)
            reader = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             dir_fd=volume)
            descriptors.append(reader)
            require(leaf_identity(os.fstat(reader)) == frozen)
            chunks, count = [], 0
            while True:
                chunk = os.read(reader, len(data) + 1 - count)
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
                require(count <= len(data))
            require(b"".join(chunks) == data and leaf_identity(os.fstat(reader)) == frozen)
            require(leaf_identity(os.stat(name, dir_fd=volume, follow_symlinks=False)) == frozen)
            guard({name}, writer, retained)

        readback()
        os.fsync(volume)
        readback()
        guard({name}, writer, retained)
        require(leaf_identity(os.fstat(writer)) == frozen)
    except BaseException as failure:
        error = failure
    finally:
        # Close-only, attempt-all cleanup. Never delete or repair partial output.
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except BaseException as failure:
                if error is None:
                    error = failure
    if error is not None:
        raise Refused("Synthetic seed refused") from None


def main():
    try:
        require(sys.argv == ["-c"])
        seed()
        require(sys.stdout.write(SUCCESS) == len(SUCCESS))
        sys.stdout.flush()
        return 0
    except BaseException:
        try:
            sys.stderr.write(FAILURE)
            sys.stderr.flush()
        except BaseException:
            pass
        return 78


if __name__ == "__main__":
    sys.exit(main())
'''


class LabSeedRecipeError(ValueError):
    """Sanitized recipe refusal without caller identities or material contents."""


def _require(condition, message):
    if not condition:
        raise LabSeedRecipeError(message)


def seed_helper_recipe(context, *, lab_binding, role):
    """Return a fixed one-shot recipe only; compatibility and admission remain false."""
    try:
        _require(type(role) is str and role in {"working", "reference"}, "Unknown seed role")
        ctx = capacity._context(context)
        ctx.verify_journal_binding(lab_binding)
        binding = copy.deepcopy(lab_binding)
        if role == "working":
            bundle = fixtures.original_fixture_inputs(ctx, lab_binding=binding)
            data = bundle[fixtures.SEED]
            filename, volume = "fixture-generation.txt", ctx.project + "-original-data"
        else:
            bundle = auxiliary.auxiliary_fixture_inputs(ctx, lab_binding=binding)
            data = bundle[auxiliary.REFERENCE_SEED]
            filename, volume = "reference-generation.txt", ctx.project + "-reference-data"
        _require(type(data) is bytes and 0 < len(data) <= MAX_SEED_BYTES, "Invalid seed byte bound")
        checksum = hashlib.sha256(data).hexdigest()
        manifest = {"path": filename, "type": "regular", "size": len(data), "sha256": checksum}
        payload = json.dumps(
            {
                "name": filename,
                "data": base64.b64encode(data).decode("ascii"),
                "size": len(data),
                "sha256": checksum,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        script = _SCRIPT.replace("__PAYLOAD__", repr(payload), 1)
        _require(len(script.encode()) <= MAX_SCRIPT_BYTES, "Seed script exceeds bound")
        result = {
            "schema": SCHEMA,
            "role": role,
            "context_sha256": ctx.digest(),
            "helper_name": ctx.project + "-" + role + "-seed",
            "create_body": {
                "Image": dict(ctx.images)["original"],
                "User": "0:0",
                "WorkingDir": "/",
                "Entrypoint": [PYTHON, "-I", "-B", "-S", "-c", script],
                "Cmd": [],
                "Env": [],
                "Volumes": {},
                "Healthcheck": {"Test": ["NONE"]},
                "Tty": False,
                "OpenStdin": False,
                "StdinOnce": False,
                "Labels": {
                    "io.codex.lab.context-sha256": ctx.digest(),
                    "io.codex.lab.fixture-role": role + "-seed-helper",
                },
                "HostConfig": {
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Source": volume,
                            "Target": "/data",
                            "ReadOnly": False,
                            "VolumeOptions": {"NoCopy": True},
                        }
                    ],
                    "ReadonlyRootfs": True,
                    "CapDrop": ["ALL"],
                    "CapAdd": [],
                    "SecurityOpt": ["no-new-privileges:true"],
                    "NetworkMode": "none",
                    "RestartPolicy": {"Name": "no", "MaximumRetryCount": 0},
                    "AutoRemove": False,
                    "Privileged": False,
                    "PublishAllPorts": False,
                    "PortBindings": {},
                    "Binds": [],
                    "PidMode": "",
                    "IpcMode": "private",
                    "UTSMode": "",
                    "UsernsMode": "",
                    "CgroupnsMode": "private",
                    "PidsLimit": 32,
                    "Memory": 64 * 1024 * 1024,
                    "NanoCpus": 500_000_000,
                    "LogConfig": {
                        "Type": "json-file",
                        "Config": {"max-size": "1m", "max-file": "1"},
                    },
                },
            },
            "seed_manifest": [manifest],
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
            "execution_admission": False,
            "compatibility_verified": False,
        }
        ctx.verify_journal_binding(lab_binding)
        _require(
            capacity._context(context) == ctx and capacity._exact(lab_binding, binding),
            "Seed recipe context or binding changed",
        )
        return result
    except LabSeedRecipeError:
        raise
    except Exception:  # noqa: BLE001 -- do not disclose malformed caller/material diagnostics.
        raise LabSeedRecipeError(
            "Seed recipe requires exact context and retained binding"
        ) from None
