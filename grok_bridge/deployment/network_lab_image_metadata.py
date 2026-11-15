"""Read-only metadata for three pinned images; no material or runtime approval.

Two bounded samples compare exact pin/platform/Config projections. Mutable tags,
repository digests and last-tag metadata are excluded. Counts and hashes report
declarations only: even nonempty Volumes/OnBuild/Healthcheck are observations, not
compatibility decisions. ENV values, commands, labels and private engine details
never leave the collector. ArgsEscaped is retained without a Linux safety ruling;
Docker's ImageConfig documents it for Windows.

The existing fixed local transport supplies per-request limits; a shared deadline
covers at most ten HTTP requests, not arbitrary filesystem syscalls. Matching
samples and an advisory session lock cannot exclude transient changes or reserve
the daemon. Resources may exist: there is no emptiness, ownership or adoption
claim. Image filesystem contents, executable compatibility, supply-chain trust,
hypervisor isolation and operator authority remain unverified. No CLI or mutation
entry point exists, and no auxiliary or production journal is written.
"""

import copy
import json
import os
import re
import sys
import time

import network_lab_capacity as capacity
import network_lab_storage as storage

SCHEMA = "grok-network-lab-image-metadata-v1"
SCOPE = "two_pinned_image_configuration_samples_only"
TOTAL_SECONDS = 30
MAX_CONFIG_BYTES = 65536
MAX_ITEMS = 256
_STRINGS = {"User", "WorkingDir", "StopSignal"}
_ARRAYS = {"Env", "Entrypoint", "Cmd", "OnBuild", "Shell"}
_MAPS = {"Volumes", "ExposedPorts", "Labels"}
_CONFIG_FIELDS = _STRINGS | _ARRAYS | _MAPS | {"ArgsEscaped", "Healthcheck"}
_COUNTS = {
    "environment_entry_count",
    "declared_volume_count",
    "onbuild_count",
    "entrypoint_count",
    "command_count",
}


class LabImageMetadataError(RuntimeError):
    """Sanitized refusal without raw daemon content or caller identities."""


def _require(condition, message):
    if not condition:
        raise LabImageMetadataError(message)


def _hex(value):
    return type(value) is str and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _platform():
    _require(
        sys.platform.startswith("linux") and os.geteuid() == 0,
        "Image metadata observation requires root on Linux",
    )
    _require(os.uname().machine == "x86_64", "Image metadata observation requires Linux x86_64")


def _copy_config(value):
    """Detach bounded JSON primitives before field validation; never invoke custom hooks."""
    budget = [4096, MAX_CONFIG_BYTES]

    def visit(item, depth=0):
        budget[0] -= 1
        _require(depth <= 8 and budget[0] >= 0, "Image configuration structure exceeds bound")
        kind = type(item)
        if item is None or kind is bool:
            return item
        if kind is int:
            _require(0 <= item < 2**63, "Image configuration integer exceeds bound")
            return item
        if kind is str:
            _require(len(item) <= 4096, "Image configuration string exceeds bound")
            length = len(item.encode("utf-8"))
            budget[1] -= length
            _require(
                length <= 4096 and budget[1] >= 0 and "\0" not in item,
                "Image configuration text exceeds bound",
            )
            return item
        if kind is list:
            _require(len(item) <= MAX_ITEMS, "Image configuration array exceeds bound")
            return [visit(child, depth + 1) for child in item]
        if kind is dict:
            _require(
                len(item) <= MAX_ITEMS and all(type(key) is str for key in item),
                "Invalid image configuration mapping",
            )
            return {visit(key, depth + 1): visit(child, depth + 1) for key, child in item.items()}
        raise LabImageMetadataError("Invalid image configuration type")

    result = visit(value)
    _require(
        len(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()) <= MAX_CONFIG_BYTES,
        "Image configuration serialization exceeds bound",
    )
    return result


def _strings(value):
    return value is None or (type(value) is list and all(type(item) is str for item in value))


def _config(value):
    config = _copy_config(value)
    _require(
        type(config) is dict and set(config) <= _CONFIG_FIELDS,
        "Unsupported image configuration fields",
    )
    for field in _STRINGS:
        _require(
            config.get(field) is None or type(config[field]) is str,
            "Invalid image configuration string field",
        )
    for field in _ARRAYS:
        _require(_strings(config.get(field)), "Invalid image configuration string array")
    environment = config.get("Env") or []
    names = set()
    for entry in environment:
        name, separator, _value = entry.partition("=")
        _require(
            separator == "="
            and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name))
            and name not in names,
            "Malformed or duplicate image environment key",
        )
        names.add(name)
    for field in _MAPS:
        values = config.get(field)
        _require(values is None or type(values) is dict, "Invalid image declaration mapping")
        if values is not None:
            _require(all(key for key in values), "Empty image declaration key")
            expected = (
                (lambda item: type(item) is str)
                if field == "Labels"
                else (lambda item: type(item) is dict and not item)
            )
            _require(
                all(expected(item) for item in values.values()), "Malformed image declaration value"
            )
    escaped = config.get("ArgsEscaped")
    _require(escaped is None or type(escaped) is bool, "Invalid ArgsEscaped metadata type")
    health = config.get("Healthcheck")
    health_numbers = {"Interval", "Timeout", "Retries", "StartPeriod", "StartInterval"}
    _require(
        health is None or (type(health) is dict and set(health) <= health_numbers | {"Test"}),
        "Invalid image healthcheck fields",
    )
    if health is not None:
        _require(_strings(health.get("Test")), "Invalid image healthcheck command array")
        _require(
            all(
                type(health[key]) is int and 0 <= health[key] < 2**63
                for key in health_numbers & set(health)
            ),
            "Invalid image healthcheck number",
        )
    return config


def _image(data, pin):
    _require(
        type(data) is dict
        and data.get("Id") == pin
        and data.get("Os") == "linux"
        and data.get("Architecture") == "amd64"
        and data.get("Variant") in (None, ""),
        "Pinned image identity or platform differs",
    )
    _require("Config" in data, "Pinned image configuration is absent")
    return {
        "id": pin,
        "os": data["Os"],
        "architecture": data["Architecture"],
        "variant": data.get("Variant"),
        "config": _config(data["Config"]),
    }


def _info(data, peer, ctx):
    identity = storage._engine_identity(data, peer)
    _require(identity["engine_id"] == ctx.engine_id, "Observed image metadata engine differs")
    _require(
        data.get("OSType") == "linux" and data.get("Architecture") in {"amd64", "x86_64"},
        "Image metadata engine platform is unsupported",
    )
    return {**identity, "os": data["OSType"], "architecture": data["Architecture"]}


def _sample(get, ctx):
    first = _info(*get("/info"), ctx)
    images = {role: _image(get("/images/" + pin + "/json")[0], pin) for role, pin in ctx.images}
    _require(
        capacity._exact(first, _info(*get("/info"), ctx)), "Image metadata engine binding changed"
    )
    return {"engine": first, "images": images}


def _summary(image):
    config = image["config"]
    return {
        "config_sha256": capacity._digest(config),
        "environment_entry_count": len(config.get("Env") or []),
        "declared_volume_count": len(config.get("Volumes") or {}),
        "healthcheck_present": config.get("Healthcheck") not in (None, {}),
        "onbuild_count": len(config.get("OnBuild") or []),
        "entrypoint_count": len(config.get("Entrypoint") or []),
        "command_count": len(config.get("Cmd") or []),
        "args_escaped": config.get("ArgsEscaped"),
    }


def _validated_result(value, context):
    """Private consistency check shared with the held-session wrapper; no authority."""
    _require(
        capacity._fields(
            value,
            {
                "schema",
                "scope",
                "context_sha256",
                "engine_socket_peer_sha256",
                "metadata_before_sha256",
                "metadata_after_sha256",
                "images",
                "execution_admission",
                "compatibility_verified",
                "material_safety_verified",
            },
        ),
        "Unexpected image metadata receipt fields",
    )
    _require(
        value["schema"] == SCHEMA
        and value["scope"] == SCOPE
        and value["context_sha256"] == context.digest()
        and all(
            _hex(value[key])
            for key in (
                "engine_socket_peer_sha256",
                "metadata_before_sha256",
                "metadata_after_sha256",
            )
        )
        and value["metadata_before_sha256"] == value["metadata_after_sha256"]
        and all(
            value[key] is False
            for key in ("execution_admission", "compatibility_verified", "material_safety_verified")
        ),
        "Unexpected image metadata receipt binding or claim",
    )
    images = value["images"]
    _require(
        capacity._fields(images, (role for role, _ in context.images)),
        "Image metadata receipt roles differ",
    )
    for item in images.values():
        _require(
            capacity._fields(
                item, _COUNTS | {"config_sha256", "healthcheck_present", "args_escaped"}
            ),
            "Unexpected image metadata summary fields",
        )
        _require(
            _hex(item["config_sha256"])
            and all(type(item[key]) is int and 0 <= item[key] <= MAX_ITEMS for key in _COUNTS)
            and type(item["healthcheck_present"]) is bool
            and (item["args_escaped"] is None or type(item["args_escaped"]) is bool),
            "Invalid image metadata summary",
        )
    return copy.deepcopy(value)


def collect(context, *, lab_binding):
    """Observe three exact descriptor pins twice; no image/listing/ownership approval."""
    try:
        ctx = capacity._context(context)
        ctx.verify_journal_binding(lab_binding)
        binding = copy.deepcopy(lab_binding)
        digest = ctx.digest()

        def guard():
            _require(
                capacity._context(context).digest() == digest and ctx.digest() == digest,
                "Image metadata context changed",
            )
            ctx.verify_journal_binding(lab_binding)
            _require(
                capacity._exact(binding, lab_binding), "Image metadata retained binding changed"
            )

        guard()
        _platform()
        deadline = time.monotonic() + TOTAL_SECONDS
        leaf = storage._Leaf(storage.SOCKET, "socket")
        try:
            socket_binding = copy.deepcopy(
                {"identity": leaf.identity, "parents": leaf.parent.bindings}
            )
            peer_binding = None
            requests = 0

            def get(route):
                nonlocal peer_binding, requests
                guard()
                storage._remaining(deadline)
                requests += 1
                _require(requests <= 10, "Image metadata request count exceeds bound")
                data, peer = storage._engine_get(leaf, route, deadline=deadline)
                _require(
                    peer_binding is None or peer == peer_binding, "Image metadata peer changed"
                )
                peer_binding = peer
                _require(
                    capacity._exact(
                        socket_binding,
                        {
                            "identity": leaf.identity,
                            "parents": leaf.parent.bindings,
                        },
                    ),
                    "Image metadata socket binding changed",
                )
                guard()
                storage._remaining(deadline)
                return data, peer

            first = _sample(get, ctx)
            second = _sample(get, ctx)
            before, after = capacity._digest(first), capacity._digest(second)
            _require(before == after, "Pinned image metadata changed between samples")
            leaf.verify()
            guard()
            storage._remaining(deadline)
            result = {
                "schema": SCHEMA,
                "scope": SCOPE,
                "context_sha256": digest,
                "engine_socket_peer_sha256": capacity._digest(
                    {"engine": first["engine"], "socket": socket_binding}
                ),
                "metadata_before_sha256": before,
                "metadata_after_sha256": after,
                "images": {role: _summary(image) for role, image in first["images"].items()},
                "execution_admission": False,
                "compatibility_verified": False,
                "material_safety_verified": False,
            }
            return _validated_result(result, ctx)
        finally:
            leaf.close()
    except LabImageMetadataError:
        raise
    except Exception:  # noqa: BLE001 -- raw daemon/configuration/caller details remain private.
        raise LabImageMetadataError("Pinned image metadata observation refused") from None
