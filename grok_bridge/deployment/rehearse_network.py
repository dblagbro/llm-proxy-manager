"""Explicit isolated Docker acceptance; retain new stopped fixtures for review.

No production services, data, ports, images or networks are mutated. This only
tests consumer attachment and rollback, not browser isolation or Grok deployment.
Requires an already-local image with /bin/sleep and no declared volumes. Never
pulls images or removes containers, networks, volumes or retained evidence.
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import network_docker as adapter
import network_policy as policy
from network_transaction import NetworkTransaction

LABEL = "com.tmr.security.network-fixture"


def run(argv):
    result = adapter.subprocess_runner([adapter.DOCKER, "--host", adapter.DOCKER_SOCKET, *argv], 45)
    if result.returncode:
        raise RuntimeError("Fixture command failed; inspect privately")
    return result.stdout.decode().strip()


def inspected(reference, kind="container"):
    values = json.loads(run(["inspect", "--type", kind, reference]))
    if len(values) != 1:
        raise RuntimeError("Ambiguous fixture identity")
    return values[0]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inventory():
    ids = run(["ps", "-aq"]).split()
    if not ids:
        return {}
    values = json.loads(run(["inspect", "--type", "container", *ids]))
    return {
        value["Id"]: digest(
            {
                "running": value["State"]["Running"],
                **{
                    key: value[key]
                    for key in (
                        "Name",
                        "Image",
                        "Config",
                        "HostConfig",
                        "Mounts",
                        "NetworkSettings",
                    )
                },
            }
        )
        for value in values
    }


def save_json(path, value):
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(value, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"codex-grok-nettest-[a-z0-9][a-z0-9-]{0,48}", args.project):
        parser.error("A unique codex-grok-nettest-* project is required")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", args.image):
        parser.error("An exact local image ID is required")
    output = Path(args.output).resolve()
    if output.exists() or Path("/var/backups") not in output.parents:
        parser.error("A new private /var/backups generation is required")
    os.umask(0o077)
    output.mkdir(parents=False)
    receipt = {
        "phase": "preflight",
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "project": args.project,
        "image": args.image,
        "checks": [],
        "resources": {},
        "production_deployed": False,
        "browser_acceptance": False,
        "deleted_resources": [],
    }
    txid = os.urandom(16).hex()
    resources = {
        policy.DEFAULT_NAME: args.project + "-default",
        policy.NETWORK_NAME: args.project + "-security",
        **{role: args.project + "-" + role for role in policy.MEMBERS},
    }
    baseline = inventory()
    created = {}
    success = False
    try:
        image = inspected(args.image, "image")
        if image["Id"] != args.image or image["Config"].get("Volumes"):
            raise RuntimeError("Image has undeclared fixture storage")
        help_text = run(["network", "connect", "--help"])
        if "--gw-priority" not in help_text:
            raise RuntimeError("Docker lacks reviewed gateway priority support")
        names = {"/" + value for value in resources.values()}
        existing = json.loads(run(["inspect", "--type", "container", *baseline]))
        if any(value["Name"] in names for value in existing):
            raise RuntimeError("Fixture container name already exists")
        for network in (policy.DEFAULT_NAME, policy.NETWORK_NAME):
            found = run(
                ["network", "ls", "--filter", "name=^" + re.escape(resources[network]) + "$", "-q"]
            )
            if found:
                raise RuntimeError("Fixture network name already exists")
        default_id = run(
            [
                "network",
                "create",
                "--driver",
                "bridge",
                "--label",
                LABEL + "=" + txid,
                resources[policy.DEFAULT_NAME],
            ]
        )
        receipt["resources"]["default_network"] = default_id
        for role in policy.CONSUMERS:
            cid = run(
                [
                    "run",
                    "-d",
                    "--pull=never",
                    "--name",
                    resources[role],
                    "--label",
                    LABEL + "=" + txid,
                    "--network",
                    resources[policy.DEFAULT_NAME],
                    "--network-alias",
                    role,
                    "--restart=no",
                    "--read-only",
                    "--cap-drop=ALL",
                    "--security-opt=no-new-privileges:true",
                    "--user=65534:65534",
                    "--pids-limit=16",
                    "--memory=32m",
                    "--cpus=0.1",
                    "--entrypoint=/bin/sleep",
                    args.image,
                    "3600",
                ]
            )
            created[role] = cid
            value = inspected(cid)
            if value["Mounts"] or value["HostConfig"]["PortBindings"]:
                raise RuntimeError("Fixture unexpectedly exposes storage or ports")
            if (
                value["NetworkSettings"]["Networks"][resources[policy.DEFAULT_NAME]]["NetworkID"]
                != default_id
            ):
                raise RuntimeError("Fixture default network identity changed")
        receipt["resources"]["consumers"] = created.copy()
        receipt["checks"].append("fixture_exact_image_no_mounts_or_published_ports")
        faults = {"connect": False, "disconnect": False}

        def runner(argv, timeout):
            result = adapter.subprocess_runner(argv, timeout)
            if result.returncode == 0 and argv[3:5] in (
                ["network", "connect"],
                ["network", "disconnect"],
            ):
                verb = argv[4]
                if not faults[verb]:
                    faults[verb] = True
                    raise TimeoutError("Injected after completed isolated operation")
            return result

        def host():
            return adapter.DockerHost(
                runner,
                enable_mutations=True,
                transaction_id=txid,
                allowed_consumers=created,
                resource_map=resources,
                compose_project=args.project,
            )

        journal = output / "journal.sanitized.json"
        initial = {role: host().container(role) for role in policy.CONSUMERS}
        NetworkTransaction.begin(host(), lambda state: save_json(journal, state), txid)
        tx = NetworkTransaction(
            host(), lambda state: save_json(journal, state), json.loads(journal.read_text())
        )
        tx.attach_consumers()
        if tx.state["phase"] != "attached" or not faults["connect"]:
            raise RuntimeError("Attach reconciliation did not complete")
        receipt["checks"].extend(
            ["fresh_controller_attach", "real_connect_post_effect_timeout_reconciled"]
        )
        owned = host().network(policy.NETWORK_NAME)
        if owned["members"] != {cid: role for role, cid in created.items()}:
            raise RuntimeError("Unexpected fixture membership")
        receipt["resources"]["dedicated_network"] = owned["id"]
        receipt["checks"].append("only_exact_consumer_members")
        tx = NetworkTransaction(
            host(), lambda state: save_json(journal, state), json.loads(journal.read_text())
        )
        tx.rollback()
        if tx.state["phase"] != "rolled_back" or not faults["disconnect"]:
            raise RuntimeError("Rollback reconciliation did not complete")
        for role in policy.CONSUMERS:
            if initial[role] != host().container(role):
                raise RuntimeError("Consumer identity/configuration/route/endpoint not restored")
        if host().network(policy.NETWORK_NAME)["members"]:
            raise RuntimeError("Owned network is not empty after rollback")
        receipt["checks"].extend(
            [
                "fresh_controller_rollback",
                "real_disconnect_post_effect_timeout_reconciled",
                "consumer_ids_config_routes_original_endpoints_preserved",
                "empty_owned_network_retained",
            ]
        )
        success = True
    except Exception as error:
        receipt["failure_class"] = type(error).__name__
    finally:
        # Reconcile a run that may have succeeded before a transport error.
        for role in policy.CONSUMERS:
            if role in created:
                continue
            try:
                value = inspected(resources[role])
                if value["Config"]["Labels"].get(LABEL) == txid:
                    created[role] = value["Id"]
            except Exception:
                continue
        receipt["resources"]["consumers"] = created.copy()
        stopped = []
        for role, cid in created.items():
            try:
                value = inspected(cid)
                if (
                    value["Name"] != "/" + resources[role]
                    or value["Config"]["Labels"].get(LABEL) != txid
                ):
                    raise RuntimeError("Fixture identity changed before stop")
                if value["State"]["Running"]:
                    run(["stop", "--time", "2", cid])
                if inspected(cid)["State"]["Running"]:
                    raise RuntimeError("Fixture did not stop")
                stopped.append(cid)
            except Exception:
                success = False
        receipt["stopped_fixture_ids"] = stopped
        after = inventory()
        unchanged = {
            cid: value for cid, value in after.items() if cid not in created.values()
        } == baseline
        receipt["preexisting_containers_unchanged"] = unchanged
        receipt["preexisting_container_count"] = len(baseline)
        success = success and unchanged and len(stopped) == len(created)
        receipt["phase"] = (
            "isolated_adapter_rollback_passed" if success else "fixture_retained_for_review"
        )
        receipt["completed_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        receipt["retention"] = (
            "Retain new stopped fixtures/networks pending named operator cleanup review"
        )
        save_json(output / "receipt.sanitized.json", receipt)
        print(json.dumps(receipt))
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
