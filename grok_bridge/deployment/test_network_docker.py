"""Injected-runner tests; never invokes Docker, nsenter, providers or a shell."""

import copy
import hashlib
import json
import subprocess
import unittest
from unittest.mock import patch

import network_docker as adapter
import network_policy as policy
import network_transaction as transaction


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


class FakeRunner:
    def __init__(self, resources=None, project="docker"):
        self.resources = resources or {
            name: name for name in (*policy.MEMBERS, policy.DEFAULT_NAME, policy.NETWORK_NAME)
        }
        self.project = project
        self.calls = []
        self.wire_calls = []
        self.containers = {}
        self.routes = {}
        self.network = None
        self.fail_listing = False
        self.hook = None
        self.mutation_error = None
        for index, role in enumerate(policy.MEMBERS):
            cid, pid = sha(role), 3100 + index
            endpoint = self.endpoint(role, policy.DEFAULT_NAME, 0)
            self.containers[cid] = {
                "Id": cid,
                "Name": "/" + self.resources[role],
                "Image": "sha256:" + sha("image-" + role),
                "RestartCount": 0,
                "State": {
                    "Pid": pid,
                    "Running": True,
                    "Restarting": False,
                    "StartedAt": "2026-11-14T00:00:00Z",
                },
                "Config": {"Env": ["TOKEN=synthetic-secret-never-export"], "Hostname": cid[:12]},
                "HostConfig": {
                    "NetworkMode": self.resources[policy.DEFAULT_NAME],
                    "PortBindings": {},
                },
                "Mounts": [{"Source": "/synthetic-private-path", "Destination": "/data"}],
                "NetworkSettings": {
                    "Networks": {self.resources[policy.DEFAULT_NAME]: endpoint},
                    "Ports": {},
                },
            }
            self.routes[pid] = {
                "-4": [
                    {"dst": "default", "gateway": "192.0.2.1", "dev": "eth0", "protocol": "static"}
                ],
                "-6": [],
            }

    def endpoint(self, role, network, priority):
        return {
            "NetworkID": sha(network),
            "EndpointID": sha(role + "-" + network),
            "IPAddress": "192.0.2.20",
            "Gateway": "192.0.2.1",
            "IPPrefixLen": 24,
            "Aliases": [role],
            "DNSNames": [self.resources[role], sha(role)[:12]],
            "GwPriority": priority,
            "MacAddress": "02:00:00:00:00:01",
            "IPAMConfig": None,
            "Links": None,
            "DriverOpts": None,
        }

    def result(self, argv, value=None, *, raw=None, code=0):
        data = json.dumps(value) if raw is None else raw
        return subprocess.CompletedProcess(argv, code, data.encode(), b"synthetic-secret-stderr")

    def owned_network(self, owner="a" * 32):
        self.network = {
            "Id": sha(policy.NETWORK_NAME),
            "Name": self.resources[policy.NETWORK_NAME],
            "Driver": "bridge",
            "Internal": False,
            "Scope": "local",
            "Ingress": False,
            "ConfigOnly": False,
            "Options": {},
            "Labels": {
                adapter.OWNER_LABEL: owner,
                adapter.PROJECT_LABEL: self.project,
                adapter.NETWORK_LABEL: policy.NETWORK,
            },
            "Containers": {},
        }

    def attach(self, cid, role, priority=-1):
        endpoint = self.endpoint(role, policy.NETWORK_NAME, priority)
        self.containers[cid]["NetworkSettings"]["Networks"][self.resources[policy.NETWORK_NAME]] = (
            endpoint
        )
        self.network["Containers"][cid] = {
            "Name": self.resources[role],
            "EndpointID": endpoint["EndpointID"],
            "IPv4Address": "192.0.2.20/24",
        }

    def __call__(self, argv, timeout):
        self.wire_calls.append((list(argv), timeout))
        if argv[0] == adapter.DOCKER:
            assert argv[1:3] == ["--host", adapter.DOCKER_SOCKET]
            argv = [argv[0], *argv[3:]]
        self.calls.append((list(argv), timeout))
        if self.hook:
            override = self.hook(argv)
            if override is not None:
                return override
        if argv[:4] == [adapter.DOCKER, "inspect", "--type", "container"]:
            values = []
            for reference in argv[4:]:
                matches = [
                    data
                    for cid, data in self.containers.items()
                    if reference == cid or reference == data["Name"].lstrip("/")
                ]
                if len(matches) != 1:
                    return self.result(argv, raw="", code=1)
                values.append(copy.deepcopy(matches[0]))
            return self.result(argv, values)
        if argv[:3] == [adapter.DOCKER, "network", "ls"]:
            return self.result(
                argv,
                raw=(self.network["Id"] + "\n") if self.network else "",
                code=1 if self.fail_listing else 0,
            )
        if argv[:3] == [adapter.DOCKER, "network", "inspect"]:
            if not self.network or argv[3] != self.network["Id"]:
                return self.result(argv, raw="", code=1)
            return self.result(argv, [copy.deepcopy(self.network)])
        if argv[:3] == [adapter.DOCKER, "ps", "-a"]:
            return self.result(argv, raw="\n".join(self.containers))
        if argv[0] == adapter.NSENTER:
            assert argv[1] == "--target" and argv[3:7] == ["--net", "--", adapter.IP, "-j"]
            assert argv[8:] == ["route", "show", "default"]
            return self.result(argv, copy.deepcopy(self.routes[int(argv[2])][argv[7]]))
        if argv[:3] == [adapter.DOCKER, "network", "create"]:
            assert self.network is None
            self.owned_network()
            if self.mutation_error == "create-after":
                raise TimeoutError("synthetic-secret-timeout")
            return self.result(argv, raw=self.network["Id"] + "\n")
        if argv[:3] == [adapter.DOCKER, "network", "connect"]:
            assert argv[3:6] == ["--gw-priority", "-1", "--alias"]
            role, network_id, cid = argv[6:]
            assert network_id == self.network["Id"]
            self.attach(cid, role)
            if self.mutation_error == "route-drift":
                pid = self.containers[cid]["State"]["Pid"]
                self.routes[pid]["-4"][0]["dev"] = "eth1"
            if self.mutation_error == "restart-after":
                value = self.containers[cid]
                value["RestartCount"] += 1
                value["State"]["StartedAt"] = "changed"
            if self.mutation_error == "connect-after":
                raise TimeoutError("synthetic-secret-timeout")
            return self.result(argv, raw="")
        if argv[:3] == [adapter.DOCKER, "network", "disconnect"]:
            assert len(argv) == 5 and argv[3] == self.network["Id"]
            cid = argv[4]
            del self.containers[cid]["NetworkSettings"]["Networks"][
                self.resources[policy.NETWORK_NAME]
            ]
            del self.network["Containers"][cid]
            if self.mutation_error == "disconnect-after":
                raise TimeoutError("synthetic-secret-timeout")
            return self.result(argv, raw="")
        raise AssertionError("Unexpected synthetic command")


def enabled(runner, **kwargs):
    return adapter.DockerHost(
        runner,
        enable_mutations=True,
        transaction_id="a" * 32,
        allowed_consumers={role: sha(role) for role in policy.CONSUMERS},
        **kwargs,
    )


class DockerAdapterTests(unittest.TestCase):
    def test_container_projection_is_sanitized_and_routes_are_host_read_only(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        projection = host.container("nginx")
        transaction._container(projection, "nginx")
        encoded = json.dumps(projection)
        for secret in (
            "synthetic-secret",
            "/synthetic-private-path",
            "192.0.2.1",
            "Config",
            "TOKEN",
        ):
            self.assertNotIn(secret, encoded)
        self.assertEqual(len(runner.calls), 4)
        self.assertEqual(
            [args[7] for args, _ in runner.calls if args[0] == adapter.NSENTER], ["-4", "-6"]
        )
        self.assertEqual(runner.calls[-1][0][-1], sha("nginx"))
        self.assertTrue(all(1 <= timeout <= 60 for _, timeout in runner.calls))
        self.assertFalse(any("exec" in args or "shell" in args for args, _ in runner.calls))

    def test_private_config_changes_affect_only_fingerprint(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        before = host.container("nginx")
        runner.containers[sha("nginx")]["Config"]["Env"] = ["TOKEN=another-synthetic-private-value"]
        after = host.container("nginx")
        self.assertNotEqual(before["config_sha256"], after["config_sha256"])
        self.assertEqual(before["default_route_sha256"], after["default_route_sha256"])
        self.assertNotIn("another-synthetic", json.dumps(after))

    def test_all_docker_calls_explicitly_select_the_local_socket(self):
        runner = FakeRunner()
        host = enabled(runner)
        network_id = host.create_network(policy.NETWORK_NAME, "a" * 32)
        host.connect(network_id, sha("nginx"), "nginx", -1)
        host.disconnect(network_id, sha("nginx"))
        docker_calls = [args for args, _ in runner.wire_calls if args[0] == adapter.DOCKER]
        self.assertTrue(docker_calls)
        self.assertTrue(
            all(args[1:3] == ["--host", "unix:///var/run/docker.sock"] for args in docker_calls)
        )

    def test_network_list_then_exact_inspect_mismatch_or_disappearance_refuses(self):
        for failure in ("wrong-id", "disappeared", "duplicate-list"):
            runner = FakeRunner()
            runner.owned_network()

            def hook(args, runner=runner, failure=failure):
                if failure == "duplicate-list" and args[1:3] == ["network", "ls"]:
                    return runner.result(args, raw=sha("first") + "\n" + sha("second"))
                if args[1:3] == ["network", "inspect"]:
                    if failure == "disappeared":
                        return runner.result(args, raw="synthetic-secret-error", code=1)
                    value = copy.deepcopy(runner.network)
                    value["Id"] = sha("wrong-id")
                    return runner.result(args, [value])
                return None

            runner.hook = hook
            with self.subTest(failure=failure), self.assertRaises(adapter.DockerBoundaryError):
                enabled(runner).network(policy.NETWORK_NAME)

    def test_restart_or_default_route_change_during_connect_is_not_success(self):
        for failure in ("route-drift", "restart-after"):
            runner = FakeRunner()
            runner.owned_network()
            runner.mutation_error = failure
            with self.subTest(failure=failure), self.assertRaises(adapter.DockerBoundaryError):
                enabled(runner).connect(sha(policy.NETWORK_NAME), sha("nginx"), "nginx", -1)
            self.assertIn(sha("nginx"), runner.network["Containers"])
            self.assertFalse(
                any(args[1:3] == ["network", "disconnect"] for args, _ in runner.calls)
            )

    def test_original_complete_endpoint_metadata_is_fingerprinted(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        before = host.container("nginx")
        endpoint = runner.containers[sha("nginx")]["NetworkSettings"]["Networks"][
            policy.DEFAULT_NAME
        ]
        endpoint["Aliases"].append("private-legacy-alias")
        after = host.container("nginx")
        self.assertNotEqual(
            before["networks"][policy.DEFAULT_NAME]["attachment_sha256"],
            after["networks"][policy.DEFAULT_NAME]["attachment_sha256"],
        )
        self.assertNotIn("private-legacy-alias", json.dumps(after))

    def test_ipv4_and_ipv6_routes_affect_digest_but_expiry_does_not(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        pid = runner.containers[sha("nginx")]["State"]["Pid"]
        initial = host.container("nginx")["default_route_sha256"]
        runner.routes[pid]["-6"] = [
            {"dst": "default", "gateway": "2001:db8::1", "dev": "eth0", "expires": 100}
        ]
        ipv6 = host.container("nginx")["default_route_sha256"]
        self.assertNotEqual(initial, ipv6)
        runner.routes[pid]["-6"][0]["expires"] = 90
        self.assertEqual(ipv6, host.container("nginx")["default_route_sha256"])
        runner.routes[pid]["-4"][0]["dev"] = "eth1"
        self.assertNotEqual(ipv6, host.container("nginx")["default_route_sha256"])

    def test_pid_start_config_and_network_drift_during_routes_refuse(self):
        changes = (
            lambda value: value["State"].update(Pid=9999),
            lambda value: value["State"].update(StartedAt="changed"),
            lambda value: value.update(RestartCount=1),
            lambda value: value["Config"].update(Env=["synthetic-secret-drift"]),
            lambda value: value["NetworkSettings"]["Networks"][policy.DEFAULT_NAME].update(
                EndpointID=sha("different")
            ),
        )
        for change in changes:
            runner = FakeRunner()

            def hook(args, change=change, runner=runner):
                if args[0] == adapter.NSENTER and args[7] == "-6":
                    change(runner.containers[sha("nginx")])

            runner.hook = hook
            with self.subTest(change=change), self.assertRaises(adapter.DockerBoundaryError):
                adapter.DockerHost(runner).container("nginx")

    def test_successful_empty_listing_is_distinct_from_daemon_error(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        self.assertIsNone(host.network(policy.NETWORK_NAME))
        self.assertIn("name=^docker_grok_security$", runner.calls[0][0])
        runner.fail_listing = True
        with self.assertRaises(adapter.DockerBoundaryError) as caught:
            host.network(policy.NETWORK_NAME)
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_read_only_refuses_all_mutations_before_any_command(self):
        runner = FakeRunner()
        host = adapter.DockerHost(runner)
        calls = (
            lambda: host.create_network(policy.NETWORK_NAME, "a" * 32),
            lambda: host.connect(sha(policy.NETWORK_NAME), sha("nginx"), "nginx", -1),
            lambda: host.disconnect(sha(policy.NETWORK_NAME), sha("nginx")),
        )
        for call in calls:
            with self.assertRaisesRegex(adapter.DockerBoundaryError, "read-only"):
                call()
        self.assertEqual(runner.calls, [])

    def test_mutation_binding_and_resource_map_refusals(self):
        for kwargs in (
            {"enable_mutations": True},
            {"enable_mutations": True, "transaction_id": "not-a-transaction"},
            {"allowed_consumers": {"nginx": sha("nginx")}},
            {"allowed_consumers": {"nginx": sha("same"), "llm-proxy2": sha("same")}},
            {"compose_project": "unexpected-production-project"},
            {"resource_map": {}, "compose_project": "codex-grok-nettest-unit"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(adapter.DockerBoundaryError):
                adapter.DockerHost(FakeRunner(), **kwargs)

    def test_create_connect_disconnect_use_exact_ids_and_owned_labels(self):
        runner = FakeRunner()
        host = enabled(runner)
        network_id = host.create_network(policy.NETWORK_NAME, "a" * 32)
        creation = next(args for args, _ in runner.calls if args[1:3] == ["network", "create"])
        self.assertIn(adapter.OWNER_LABEL + "=" + "a" * 32, creation)
        self.assertIn(adapter.PROJECT_LABEL + "=docker", creation)
        self.assertIn(adapter.NETWORK_LABEL + "=" + policy.NETWORK, creation)
        before = host.container("nginx")
        host.connect(network_id, sha("nginx"), "nginx", -1)
        after = host.container("nginx")
        self.assertEqual(before["config_sha256"], after["config_sha256"])
        self.assertEqual(before["default_route_sha256"], after["default_route_sha256"])
        connection = next(args for args, _ in runner.calls if args[1:3] == ["network", "connect"])
        self.assertEqual(
            connection,
            [
                adapter.DOCKER,
                "network",
                "connect",
                "--gw-priority",
                "-1",
                "--alias",
                "nginx",
                network_id,
                sha("nginx"),
            ],
        )
        host.disconnect(network_id, sha("nginx"))
        disconnection = next(
            args for args, _ in runner.calls if args[1:3] == ["network", "disconnect"]
        )
        self.assertEqual(
            disconnection, [adapter.DOCKER, "network", "disconnect", network_id, sha("nginx")]
        )
        self.assertEqual(runner.network["Containers"], {})
        self.assertFalse(
            any("--force" in args or "rm" in args or "stop" in args for args, _ in runner.calls)
        )

    def test_foreign_network_labels_driver_and_identity_refuse(self):
        changes = (
            lambda value: value["Labels"].update({adapter.OWNER_LABEL: "b" * 32}),
            lambda value: value["Labels"].update({adapter.PROJECT_LABEL: "other"}),
            lambda value: value["Labels"].update({adapter.NETWORK_LABEL: "other"}),
            lambda value: value.update(Name="other"),
            lambda value: value.update(Internal=True),
            lambda value: value.update(Driver="overlay"),
            lambda value: value.update(Options={"unreviewed": "setting"}),
            lambda value: value.update(EnableIPv6=True),
            lambda value: value.update(EnableIPv4=False),
        )
        for change in changes:
            runner = FakeRunner()
            runner.owned_network()
            change(runner.network)
            with self.subTest(change=change), self.assertRaises(adapter.DockerBoundaryError):
                enabled(runner).network(policy.NETWORK_NAME)

    def test_unknown_member_alias_and_endpoint_mismatch_refuse(self):
        for change in (
            lambda r: r.network["Containers"].update({sha("unknown"): {"Name": "unknown"}}),
            lambda r: r.network["Containers"][sha("nginx")].update(EndpointID=sha("wrong")),
            lambda r: r.containers[sha("nginx")]["NetworkSettings"]["Networks"][
                policy.NETWORK_NAME
            ].update(Aliases=["unapproved-private-alias"]),
            lambda r: r.containers[sha("nginx")]["NetworkSettings"]["Networks"][
                policy.NETWORK_NAME
            ].update(GwPriority=0),
        ):
            runner = FakeRunner()
            runner.owned_network()
            runner.attach(sha("nginx"), "nginx")
            change(runner)
            with self.subTest(change=change), self.assertRaises(adapter.DockerBoundaryError):
                enabled(runner).network(policy.NETWORK_NAME)

    def test_namespace_sharer_blocks_mutation(self):
        runner = FakeRunner()
        runner.owned_network()
        outsider = copy.deepcopy(runner.containers[sha("nginx")])
        outsider.update(Id=sha("outsider"), Name="/unrelated-outsider")
        outsider["HostConfig"]["NetworkMode"] = "container:" + sha("nginx")
        runner.containers[outsider["Id"]] = outsider
        with self.assertRaisesRegex(adapter.DockerBoundaryError, "shares an approved namespace"):
            enabled(runner).connect(sha(policy.NETWORK_NAME), sha("nginx"), "nginx", -1)
        self.assertFalse(any(args[1:3] == ["network", "connect"] for args, _ in runner.calls))

    def test_bridge_ports_and_namespace_modes_refuse(self):
        for change in (
            lambda v: v["HostConfig"].update(NetworkMode="host"),
            lambda v: v["HostConfig"].update(NetworkMode="container:" + sha("nginx")),
            lambda v: v["HostConfig"].update(PortBindings={"5900/tcp": [{"HostPort": "5900"}]}),
            lambda v: v["NetworkSettings"].update(Ports={"5900/tcp": [{"HostPort": "5900"}]}),
        ):
            runner = FakeRunner()
            change(runner.containers[sha(policy.BRIDGE)])
            with self.subTest(change=change), self.assertRaises(adapter.DockerBoundaryError):
                adapter.DockerHost(runner).container(policy.BRIDGE)

    def test_forbidden_mutation_targets_and_options_refuse(self):
        runner = FakeRunner()
        host = enabled(runner)
        operations = (
            lambda: host.connect(sha(policy.NETWORK_NAME), sha(policy.BRIDGE), policy.BRIDGE, -1),
            lambda: host.disconnect(sha(policy.NETWORK_NAME), sha(policy.BRIDGE)),
            lambda: host.connect(sha(policy.NETWORK_NAME), sha("nginx"), "llm-proxy2", -1),
            lambda: host.connect(sha(policy.NETWORK_NAME), sha("nginx"), "nginx", 0),
            lambda: host.create_network("other", "a" * 32),
            lambda: host.create_network(policy.NETWORK_NAME, "b" * 32),
        )
        for operation in operations:
            with self.assertRaises(adapter.DockerBoundaryError):
                operation()
        self.assertEqual(runner.calls, [])

    def test_replaced_consumer_and_wrong_network_id_never_disconnect(self):
        runner = FakeRunner()
        runner.owned_network()
        runner.attach(sha("nginx"), "nginx")
        host = enabled(runner)
        with self.assertRaises(adapter.DockerBoundaryError):
            host.disconnect(sha("wrong-network"), sha("nginx"))
        runner.containers[sha("nginx")]["Id"] = sha("replacement")
        with self.assertRaises(adapter.DockerBoundaryError):
            host.disconnect(sha(policy.NETWORK_NAME), sha("nginx"))
        self.assertFalse(any(args[1:3] == ["network", "disconnect"] for args, _ in runner.calls))

    def test_fixture_map_is_all_or_nothing_and_normalizes_roles(self):
        prefix = "codex-grok-nettest-unit"
        resources = {
            "nginx": prefix + "-nginx",
            "llm-proxy2": prefix + "-gateway",
            policy.BRIDGE: prefix + "-bridge",
            policy.DEFAULT_NAME: prefix + "-default",
            policy.NETWORK_NAME: prefix + "-private",
        }
        runner = FakeRunner(resources, prefix)
        host = enabled(runner, resource_map=resources, compose_project=prefix)
        self.assertEqual(host.container("nginx")["name"], "nginx")
        network_id = host.create_network(policy.NETWORK_NAME, "a" * 32)
        host.connect(network_id, sha("llm-proxy2"), "llm-proxy2", -1)
        self.assertEqual(
            host.network(policy.NETWORK_NAME)["members"], {sha("llm-proxy2"): "llm-proxy2"}
        )
        resources["nginx"] = "nginx"
        with self.assertRaises(adapter.DockerBoundaryError):
            enabled(runner, resource_map=resources, compose_project=prefix)

    def test_adapter_and_transaction_reconcile_ambiguous_effects(self):
        for failure in ("create-after", "connect-after", "disconnect-after"):
            with self.subTest(failure=failure):
                runner = FakeRunner()
                runner.mutation_error = failure
                host = enabled(runner)
                journal = []
                engine = transaction.NetworkTransaction.begin(
                    host,
                    lambda state, journal=journal: journal.append(copy.deepcopy(state)),
                    "a" * 32,
                )
                engine.attach_consumers()
                engine.rollback()
                self.assertEqual(engine.state["phase"], "rolled_back")
                self.assertEqual(runner.network["Containers"], {})

    def test_errors_and_invalid_json_do_not_expose_private_output(self):
        def failed(argv, timeout):
            raise RuntimeError("synthetic-secret-raw-exception")

        with self.assertRaises(adapter.DockerBoundaryError) as caught:
            adapter.DockerHost(failed).container("nginx")
        self.assertNotIn("synthetic-secret", str(caught.exception))

        def malformed(argv, timeout):
            return subprocess.CompletedProcess(argv, 0, b"synthetic-secret-invalid-json", b"")

        with self.assertRaises(adapter.DockerBoundaryError) as caught:
            adapter.DockerHost(malformed).container("nginx")
        self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_default_runner_uses_capture_timeout_and_no_shell(self):
        with patch.object(adapter.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
            adapter.subprocess_runner([adapter.DOCKER, "version"], 7)
            kwargs = run.call_args.kwargs
            self.assertIs(kwargs["shell"], False)
            self.assertIs(kwargs["check"], False)
            self.assertEqual(kwargs["timeout"], 7)
            self.assertIs(kwargs["capture_output"], True)


if __name__ == "__main__":
    unittest.main()
