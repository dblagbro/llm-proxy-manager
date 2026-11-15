"""Synthetic topology and refusal tests; no Docker, network, or private data."""

import copy
import json
import unittest

import network_policy as policy


def fixture():
    service = {
        "image": "synthetic-image",
        "networks": {"default": None},
        "environment": {"TOKEN": "synthetic-never-export-this-value"},
        "volumes": [{"type": "volume", "source": "synthetic-data", "target": "/data"}],
    }
    return {
        "name": "docker",
        "services": {
            **{name: copy.deepcopy(service) for name in policy.MEMBERS},
            "unrelated": {"image": "retained", "networks": {"default": None}},
            "database": {"image": "retained-db", "networks": {"postgres-network": None}},
        },
        "networks": {
            "default": {"name": "docker_default", "ipam": {}},
            "postgres-network": {"name": "docker_postgres-network", "ipam": {}},
        },
        "volumes": {"synthetic-data": {"name": "synthetic-data"}},
        "x-preserve": {"arbitrary": ["unchanged"]},
    }


class NetworkPolicyTests(unittest.TestCase):
    def test_least_privilege_and_callback_alias(self):
        before = fixture()
        candidate, receipt = policy.plan_networks(before)
        self.assertEqual(set(candidate["services"][policy.BRIDGE]["networks"]), {policy.NETWORK})
        self.assertEqual(
            candidate["services"][policy.BRIDGE]["networks"][policy.NETWORK],
            {"aliases": [policy.BRIDGE]},
        )
        for name in policy.CONSUMERS:
            membership = candidate["services"][name]["networks"]
            self.assertEqual(set(membership), {"default", policy.NETWORK})
            self.assertEqual(membership[policy.NETWORK], {"aliases": [name], "gw_priority": -1})
        self.assertEqual(candidate["networks"][policy.NETWORK]["internal"], False)
        self.assertEqual(receipt["network"]["members"], list(policy.MEMBERS))
        self.assertFalse(receipt["production_modified"])
        self.assertFalse(receipt["release_gate_satisfied"])

    def test_input_and_untouched_fields_are_preserved(self):
        before = fixture()
        snapshot = copy.deepcopy(before)
        candidate, _ = policy.plan_networks(before)
        self.assertEqual(before, snapshot)
        for name, service in before["services"].items():
            if name in policy.MEMBERS:
                for key, value in service.items():
                    if key != "networks":
                        self.assertEqual(candidate["services"][name][key], value)
            else:
                self.assertEqual(candidate["services"][name], service)
        for key, value in before.items():
            if key not in {"services", "networks"}:
                self.assertEqual(candidate[key], value)
        for name, value in before["networks"].items():
            self.assertEqual(candidate["networks"][name], value)
        candidate["services"]["llm-proxy2"]["environment"]["TOKEN"] = "changed"
        candidate["x-preserve"]["arbitrary"].append("changed")
        self.assertEqual(before, snapshot)

    def test_existing_consumer_attach_metadata_is_preserved(self):
        before = fixture()
        options = {"aliases": ["gateway-private"], "priority": 7, "gw_priority": 3}
        before["services"]["llm-proxy2"]["networks"]["default"] = options
        candidate, _ = policy.plan_networks(before)
        self.assertEqual(candidate["services"]["llm-proxy2"]["networks"]["default"], options)
        self.assertEqual(options["gw_priority"], 3)
        self.assertNotIn(
            "priority", candidate["services"]["llm-proxy2"]["networks"][policy.NETWORK]
        )

    def test_expected_runtime_mapping_is_private_copy_and_strict(self):
        candidate, _ = policy.plan_networks(fixture())
        snapshot = copy.deepcopy(candidate)
        runtime = policy.expected_runtime_networks(candidate)
        self.assertEqual(set(runtime), set(policy.MEMBERS))
        self.assertEqual(set(runtime[policy.BRIDGE]), {policy.NETWORK_NAME})
        self.assertEqual(set(runtime["llm-proxy2"]), {policy.DEFAULT_NAME, policy.NETWORK_NAME})
        runtime["llm-proxy2"][policy.NETWORK_NAME]["aliases"].append("changed")
        self.assertEqual(candidate, snapshot)
        candidate["services"]["unrelated"]["networks"][policy.NETWORK] = None
        with self.assertRaisesRegex(policy.NetworkPolicyError, "allowlist"):
            policy.expected_runtime_networks(candidate)

    def test_exact_scope_rejects_non_network_changes(self):
        before = fixture()
        candidate, _ = policy.plan_networks(before)
        policy.verify_network_scope(before, candidate)
        mutations = [
            lambda x: x["services"][policy.BRIDGE].update(image="other"),
            lambda x: x["services"]["nginx"]["environment"].update(TOKEN="other"),
            lambda x: x["services"]["unrelated"].update(image="other"),
            lambda x: x["volumes"].clear(),
            lambda x: x["networks"]["postgres-network"].update(internal=True),
            lambda x: x["services"]["nginx"]["networks"].update(default={"priority": 9}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                changed = copy.deepcopy(candidate)
                mutate(changed)
                with self.assertRaisesRegex(policy.NetworkPolicyError, "scope"):
                    policy.verify_network_scope(before, changed)

    def test_missing_targets_and_unresolved_networks_refuse(self):
        for name in policy.MEMBERS:
            before = fixture()
            del before["services"][name]
            with self.subTest(name=name), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)
        for value in (
            None,
            [],
            ["default"],
            {},
            {"postgres-network": None},
            {"default": None, "postgres-network": None},
        ):
            before = fixture()
            before["services"][policy.BRIDGE]["networks"] = value
            with self.subTest(value=value), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_network_identity_and_collision_refusals(self):
        mutations = [
            lambda x: x["networks"]["default"].update(name="unexpected"),
            lambda x: x["networks"].update(grok_security=copy.deepcopy(policy.NETWORK_DEFINITION)),
            lambda x: x["networks"]["postgres-network"].update(name=policy.NETWORK_NAME),
            lambda x: x["networks"]["postgres-network"].update(name=policy.DEFAULT_NAME),
            lambda x: x["networks"]["default"].update(internal=True),
            lambda x: x["networks"]["default"].update(driver="host"),
            lambda x: x["networks"]["default"].update(
                ipam={"config": [{"subnet": "192.0.2.0/24"}]}
            ),
            lambda x: x["services"]["unrelated"]["networks"].update(unknown=None),
            lambda x: x["services"]["unrelated"].pop("networks"),
        ]
        for mutate in mutations:
            before = fixture()
            mutate(before)
            with self.subTest(mutation=mutate), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_unsafe_target_settings_refuse(self):
        settings = [
            {"network_mode": "host"},
            {"network_mode": "service:other"},
            {"links": ["other"]},
            {"external_links": ["other"]},
            {"ports": [{"target": 5900, "published": "5900"}]},
            {"ports": None},
            {"container_name": "unexpected"},
        ]
        for setting in settings:
            before = fixture()
            before["services"][policy.BRIDGE].update(setting)
            with self.subTest(setting=setting), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_static_addresses_and_advanced_attachment_options_refuse(self):
        for options in (
            {"ipv4_address": "192.0.2.3"},
            {"ipv6_address": "2001:db8::1"},
            {"mac_address": "02:00:00:00:00:01"},
            {"driver_opts": {}},
            {"link_local_ips": ["192.0.2.4"]},
            {"interface_name": "eth1"},
        ):
            before = fixture()
            before["services"]["nginx"]["networks"]["default"] = options
            with self.subTest(options=options), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_priority_refusals(self):
        for key in ("priority", "gw_priority"):
            for value in (-1, True, "0", 0.0, None):
                before = fixture()
                before["services"]["nginx"]["networks"]["default"] = {key: value}
                with (
                    self.subTest(key=key, value=value),
                    self.assertRaises(policy.NetworkPolicyError),
                ):
                    policy.plan_networks(before)
        candidate, _ = policy.plan_networks(fixture())
        for value in (0, 1, -2, True):
            changed = copy.deepcopy(candidate)
            changed["services"]["nginx"]["networks"][policy.NETWORK]["gw_priority"] = value
            with self.subTest(value=value), self.assertRaises(policy.NetworkPolicyError):
                policy.expected_runtime_networks(changed)
        candidate["services"][policy.BRIDGE]["networks"][policy.NETWORK]["gw_priority"] = False
        with self.assertRaises(policy.NetworkPolicyError):
            policy.expected_runtime_networks(candidate)

    def test_exact_scope_distinguishes_json_boolean_and_numeric_values(self):
        before = fixture()
        before["x-preserve"] = {"count": 0, "other": 1}
        for key, replacement in (("count", False), ("other", 1.0)):
            candidate, _ = policy.plan_networks(before)
            candidate["x-preserve"][key] = replacement
            with self.subTest(key=key), self.assertRaisesRegex(policy.NetworkPolicyError, "scope"):
                policy.verify_network_scope(before, candidate)

    def test_alias_conflicts_and_invalid_values_refuse(self):
        for aliases in (["llm-proxy2"], ["nginx"], [policy.BRIDGE], ["LLM-PROXY2"]):
            before = fixture()
            before["services"]["unrelated"]["networks"]["default"] = {"aliases": aliases}
            with self.subTest(aliases=aliases), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)
        for key in ("container_name", "hostname"):
            before = fixture()
            before["services"]["unrelated"][key] = "llm-proxy2"
            with self.subTest(key=key), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)
        for aliases in ("llm-proxy2", [False], ["same", "same"], ["same", "SAME"], ["bad alias"]):
            before = fixture()
            before["services"]["nginx"]["networks"]["default"] = {"aliases": aliases}
            with self.subTest(aliases=aliases), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_receipts_and_errors_do_not_export_input_values(self):
        marker = "synthetic-sensitive-input-do-not-export"
        before = fixture()
        before["services"]["nginx"]["environment"]["PRIVATE"] = marker
        before["x-preserve"] = marker
        _, receipt = policy.plan_networks(before)
        encoded = json.dumps(receipt)
        self.assertNotIn(marker, encoded)
        self.assertNotIn("TOKEN", encoded)
        self.assertNotIn("environment", encoded)
        before["services"]["nginx"]["networks"]["default"] = {marker: marker}
        with self.assertRaises(policy.NetworkPolicyError) as caught:
            policy.plan_networks(before)
        self.assertNotIn(marker, str(caught.exception))

    def test_input_types_cannot_invoke_custom_copy_hooks(self):
        class Unexpected:
            def __deepcopy__(self, memo):
                raise AssertionError("Must never invoke input object hooks")

        for value in (Unexpected(), float("nan"), float("inf"), ("not", "json")):
            before = fixture()
            before["x-preserve"] = value
            with self.subTest(kind=type(value)), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)

    def test_namespace_sharing_is_refused_even_without_membership(self):
        for mode in (
            "service:" + policy.BRIDGE,
            "service:nginx",
            "service:unrelated",
            "container:synthetic-target-id",
            "container:llm-proxy2",
            "bridge",
            None,
            {},
        ):
            before = fixture()
            before["services"]["unrelated"] = {"image": "retained", "network_mode": mode}
            with self.subTest(mode=mode), self.assertRaises(policy.NetworkPolicyError):
                policy.plan_networks(before)
            candidate, _ = policy.plan_networks(fixture())
            candidate["services"]["unrelated"] = copy.deepcopy(before["services"]["unrelated"])
            with self.subTest(candidate_mode=mode), self.assertRaises(policy.NetworkPolicyError):
                policy.expected_runtime_networks(candidate)

    def test_unrelated_host_and_none_modes_are_preserved_without_memberships(self):
        for mode in ("host", "none"):
            for declared_networks in (False, True):
                before = fixture()
                service = {"image": "retained", "network_mode": mode}
                if declared_networks:
                    service["networks"] = {}
                before["services"]["unrelated"] = service
                with self.subTest(mode=mode, declared=declared_networks):
                    candidate, _ = policy.plan_networks(before)
                    self.assertEqual(candidate["services"]["unrelated"], service)
                    policy.verify_network_scope(before, candidate)
                    policy.expected_runtime_networks(candidate)

    def test_malformed_unrelated_memberships_raise_sanitized_policy_error(self):
        for mode in ("host", "none"):
            for networks in (None, [], "default", False, {"default": None}):
                before = fixture()
                before["services"]["unrelated"] = {
                    "image": "retained",
                    "network_mode": mode,
                    "networks": networks,
                }
                with (
                    self.subTest(mode=mode, networks=networks),
                    self.assertRaises(policy.NetworkPolicyError),
                ):
                    policy.plan_networks(before)
                candidate, _ = policy.plan_networks(fixture())
                candidate["services"]["unrelated"] = copy.deepcopy(before["services"]["unrelated"])
                with (
                    self.subTest(candidate_mode=mode, networks=networks),
                    self.assertRaises(policy.NetworkPolicyError),
                ):
                    policy.expected_runtime_networks(candidate)

    def test_extra_bridge_alias_is_not_silently_dropped(self):
        before = fixture()
        before["services"][policy.BRIDGE]["networks"]["default"] = {
            "aliases": ["private-client-alias"]
        }
        snapshot = copy.deepcopy(before)
        with self.assertRaisesRegex(policy.NetworkPolicyError, "Additional bridge aliases"):
            policy.plan_networks(before)
        self.assertEqual(before, snapshot)
        for aliases in ([], [policy.BRIDGE]):
            before["services"][policy.BRIDGE]["networks"]["default"] = {"aliases": aliases}
            candidate, _ = policy.plan_networks(before)
            self.assertEqual(
                candidate["services"][policy.BRIDGE]["networks"][policy.NETWORK]["aliases"],
                [policy.BRIDGE],
            )


if __name__ == "__main__":
    unittest.main()
