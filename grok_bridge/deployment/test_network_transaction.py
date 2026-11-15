"""Failure-path tests with a synthetic host; no Docker, providers or file writes."""

import copy
import hashlib
import json
import unittest

import network_transaction as transaction


def checksum(value):
    return hashlib.sha256(value.encode()).hexdigest()


class Crash(BaseException):
    """Model process termination after an action but before the next journal write."""


class FakeHost:
    def __init__(self):
        self.network_value = None
        self.journal = []
        self.actions = []
        self.behavior = {}
        self.save_failure = None
        self.network_reads = 0
        self.drop_on_network_read = None
        self.containers = {
            name: {
                "name": name,
                "id": checksum(name),
                "config_sha256": checksum(name + "-config"),
                "default_route_sha256": checksum(name + "-route"),
                "networks": {
                    transaction.DEFAULT: {
                        "id": checksum("original-network"),
                        "attachment_sha256": checksum(name + "-original-attachment"),
                        "aliases": [name],
                        "gw_priority": 0,
                    }
                },
            }
            for name in transaction.CONSUMERS
        }

    def save(self, state):
        if self.save_failure and self.save_failure(state):
            raise OSError("synthetic-sensitive-persistence-detail")
        self.journal.append(copy.deepcopy(state))

    def container(self, name):
        return copy.deepcopy(self.containers[name])

    def network(self, name):
        assert name == transaction.NETWORK
        self.network_reads += 1
        if self.network_reads == self.drop_on_network_read:
            self.network_value = None
        return copy.deepcopy(self.network_value)

    def create_network(self, name, txid):
        assert self.journal[-1]["network"]["phase"] == "creating"
        self.actions.append(("create", name, txid))
        if self.behavior.get("create") == "before":
            raise TimeoutError("synthetic-sensitive-create-detail")
        assert self.network_value is None
        self.network_value = {
            "name": name,
            "id": checksum("new-network"),
            "transaction_id": txid,
            "driver": "bridge",
            "internal": False,
            "members": {},
        }
        if self.behavior.get("create") == "foreign":
            self.network_value["transaction_id"] = "f" * 32
            raise TimeoutError("foreign collision")
        if self.behavior.get("create") == "after":
            raise TimeoutError("synthetic-sensitive-create-detail")
        if self.behavior.get("create") == "crash":
            raise Crash()
        if self.behavior.get("create") == "wrong-receipt":
            return checksum("unexpected-creation-receipt")
        return self.network_value["id"]

    def connect(self, network_id, container_id, alias, priority):
        assert self.journal[-1]["consumers"][alias]["phase"] == "connecting"
        assert self.network_value["id"] == network_id
        assert self.containers[alias]["id"] == container_id
        self.actions.append(("connect", network_id, container_id, alias, priority))
        behavior = self.behavior.get("connect-" + alias)
        if behavior == "before":
            raise TimeoutError("synthetic-sensitive-connect-detail")
        assert transaction.NETWORK not in self.containers[alias]["networks"]
        self.containers[alias]["networks"][transaction.NETWORK] = {
            "id": network_id,
            "attachment_sha256": checksum(alias + "-new-attachment"),
            "aliases": [alias],
            "gw_priority": priority,
        }
        self.network_value["members"][container_id] = alias
        if behavior == "after":
            raise TimeoutError("synthetic-sensitive-connect-detail")
        if behavior == "crash":
            raise Crash()
        if behavior == "route-drift":
            self.containers[alias]["default_route_sha256"] = checksum("wrong-route")

    def disconnect(self, network_id, container_id):
        name = next(name for name, data in self.containers.items() if data["id"] == container_id)
        assert self.journal[-1]["consumers"][name]["phase"] == "disconnecting"
        assert self.network_value["id"] == network_id
        self.actions.append(("disconnect", network_id, container_id))
        behavior = self.behavior.get("disconnect-" + name)
        if behavior == "before":
            raise TimeoutError("synthetic-sensitive-disconnect-detail")
        del self.containers[name]["networks"][transaction.NETWORK]
        del self.network_value["members"][container_id]
        if behavior == "after":
            raise TimeoutError("synthetic-sensitive-disconnect-detail")
        if behavior == "crash":
            raise Crash()


class NetworkTransactionTests(unittest.TestCase):
    def begin(self, host=None):
        host = host or FakeHost()
        return host, transaction.NetworkTransaction.begin(host, host.save, "a" * 32)

    def resume(self, host):
        return transaction.NetworkTransaction(host, host.save, host.journal[-1])

    def test_success_intents_precede_actions_and_rollback_retains_network(self):
        host, engine = self.begin()
        before = copy.deepcopy(host.containers)
        attached = engine.attach_consumers()
        self.assertEqual(attached["phase"], "attached")
        self.assertEqual(len(host.actions), 3)
        self.assertEqual(host.actions[0][0], "create")
        self.assertEqual([a[3] for a in host.actions[1:]], list(transaction.CONSUMERS))
        for name in transaction.CONSUMERS:
            for key in ("id", "config_sha256", "default_route_sha256"):
                self.assertEqual(host.containers[name][key], before[name][key])
            self.assertEqual(
                host.containers[name]["networks"][transaction.DEFAULT],
                before[name]["networks"][transaction.DEFAULT],
            )
        self.assertEqual(engine.rollback()["phase"], "rolled_back")
        self.assertEqual(host.containers, before)
        self.assertEqual(host.network_value["members"], {})
        self.assertEqual(host.network_value["id"], checksum("new-network"))
        self.assertTrue(all(a[0] in {"create", "connect", "disconnect"} for a in host.actions))
        self.assertEqual(len(host.actions), 5)

    def test_completed_steps_do_not_duplicate_on_resume(self):
        host, engine = self.begin()
        engine.attach_consumers()
        self.resume(host).attach_consumers()
        self.assertEqual(len(host.actions), 3)
        engine = self.resume(host)
        engine.rollback()
        self.resume(host).rollback()
        self.assertEqual(len(host.actions), 5)

    def test_create_timeout_after_effect_reconciles_ownership(self):
        host = FakeHost()
        host.behavior["create"] = "after"
        host, engine = self.begin(host)
        state = engine.attach_consumers()
        self.assertEqual(state["network"]["proof"], "creation_intent_reconciled")
        self.assertEqual(len([a for a in host.actions if a[0] == "create"]), 1)

    def test_create_crash_reconciles_without_duplicate_creation(self):
        host = FakeHost()
        host.behavior["create"] = "crash"
        host, engine = self.begin(host)
        with self.assertRaises(Crash):
            engine.attach_consumers()
        self.assertEqual(host.journal[-1]["network"]["phase"], "creating")
        self.resume(host).attach_consumers()
        self.assertEqual(len([a for a in host.actions if a[0] == "create"]), 1)

    def test_create_failure_before_effect_can_rollback_without_creating(self):
        host = FakeHost()
        host.behavior["create"] = "before"
        host, engine = self.begin(host)
        with self.assertRaisesRegex(transaction.TransactionError, "not observed"):
            engine.attach_consumers()
        self.resume(host).rollback()
        self.assertIsNone(host.network_value)
        self.assertEqual(len(host.actions), 1)

    def test_rollback_never_creates_if_network_disappears_between_inspections(self):
        host = FakeHost()
        host.behavior["create"] = "crash"
        host, engine = self.begin(host)
        with self.assertRaises(Crash):
            engine.attach_consumers()
        host.drop_on_network_read = host.network_reads + 2
        with self.assertRaisesRegex(transaction.TransactionError, "prohibited during rollback"):
            self.resume(host).rollback()
        self.assertEqual(len(host.actions), 1)

    def test_connect_timeouts_after_effect_are_reconciled_once(self):
        host = FakeHost()
        for name in transaction.CONSUMERS:
            host.behavior["connect-" + name] = "after"
        host, engine = self.begin(host)
        state = engine.attach_consumers()
        self.assertTrue(all(r["owned"] for r in state["consumers"].values()))
        self.assertEqual(len([a for a in host.actions if a[0] == "connect"]), 2)

    def test_connect_crash_reconciles_for_resume_or_rollback(self):
        for operation in ("attach_consumers", "rollback"):
            with self.subTest(operation=operation):
                host = FakeHost()
                host.behavior["connect-nginx"] = "crash"
                host, engine = self.begin(host)
                with self.assertRaises(Crash):
                    engine.attach_consumers()
                self.assertEqual(host.journal[-1]["consumers"]["nginx"]["phase"], "connecting")
                getattr(self.resume(host), operation)()
                self.assertEqual(
                    len([a for a in host.actions if a[0] == "connect" and a[3] == "nginx"]), 1
                )
                if operation == "rollback":
                    self.assertEqual(host.network_value["members"], {})

    def test_connect_failure_before_effect_requires_reconciliation_and_can_rollback(self):
        host = FakeHost()
        host.behavior["connect-llm-proxy2"] = "before"
        host, engine = self.begin(host)
        with self.assertRaisesRegex(transaction.TransactionError, "not observed"):
            engine.attach_consumers()
        self.resume(host).rollback()
        self.assertEqual(host.network_value["members"], {})
        disconnected = [a[2] for a in host.actions if a[0] == "disconnect"]
        self.assertEqual(disconnected, [checksum("nginx")])

    def test_disconnect_timeout_or_crash_after_effect_does_not_repeat(self):
        for behavior in ("after", "crash"):
            with self.subTest(behavior=behavior):
                host, engine = self.begin()
                engine.attach_consumers()
                host.behavior["disconnect-llm-proxy2"] = behavior
                if behavior == "crash":
                    with self.assertRaises(Crash):
                        engine.rollback()
                    self.resume(host).rollback()
                else:
                    engine.rollback()
                self.assertEqual(len([a for a in host.actions if a[0] == "disconnect"]), 2)
                self.assertEqual(host.network_value["members"], {})

    def test_disconnect_before_effect_does_not_claim_success(self):
        host, engine = self.begin()
        engine.attach_consumers()
        host.behavior["disconnect-llm-proxy2"] = "before"
        with self.assertRaisesRegex(transaction.TransactionError, "not observed"):
            engine.rollback()
        self.assertEqual(host.journal[-1]["consumers"]["llm-proxy2"]["phase"], "disconnecting")
        self.assertEqual(len(host.network_value["members"]), 2)

    def test_journal_failure_prevents_the_next_mutation(self):
        for phase in ("creating", "connecting", "disconnecting"):
            with self.subTest(phase=phase):
                host, engine = self.begin()
                if phase == "disconnecting":
                    engine.attach_consumers()
                before = len(host.actions)
                host.save_failure = lambda s, phase=phase: (
                    s["network"]["phase"] == phase
                    or any(r["phase"] == phase for r in s["consumers"].values())
                )
                with self.assertRaisesRegex(transaction.TransactionError, "persistence failed"):
                    (engine.rollback if phase == "disconnecting" else engine.attach_consumers)()
                expected = before + (1 if phase == "connecting" else 0)
                self.assertEqual(len(host.actions), expected)

    def test_consumer_replacement_or_drift_stops_before_any_detach(self):
        changes = (
            lambda h: h.containers["nginx"].update(id=checksum("replacement")),
            lambda h: h.containers["nginx"].update(config_sha256=checksum("config-drift")),
            lambda h: h.containers["nginx"].update(default_route_sha256=checksum("route-drift")),
            lambda h: h.containers["nginx"]["networks"][transaction.DEFAULT].update(
                attachment_sha256=checksum("membership-drift")
            ),
            lambda h: h.containers["nginx"]["networks"][transaction.NETWORK].update(
                attachment_sha256=checksum("reconnected-elsewhere")
            ),
        )
        for change in changes:
            with self.subTest(change=change):
                host, engine = self.begin()
                engine.attach_consumers()
                change(host)
                with self.assertRaises(transaction.TransactionError):
                    engine.rollback()
                self.assertFalse(any(a[0] == "disconnect" for a in host.actions))

    def test_save_failure_after_effect_resumes_from_last_durable_intent(self):
        for effect in ("create", "connect", "disconnect"):
            with self.subTest(effect=effect):
                host, engine = self.begin()
                if effect == "disconnect":
                    engine.attach_consumers()
                if effect == "create":
                    host.save_failure = lambda state: state["network"]["phase"] == "owned"
                elif effect == "connect":
                    host.save_failure = lambda state: (
                        state["consumers"]["nginx"]["phase"] == "attached"
                    )
                else:
                    host.save_failure = lambda state: (
                        state["consumers"]["llm-proxy2"]["phase"] == "detached"
                    )
                with self.assertRaisesRegex(transaction.TransactionError, "persistence failed"):
                    (engine.rollback if effect == "disconnect" else engine.attach_consumers)()
                durable = host.journal[-1]
                if effect == "create":
                    self.assertEqual(durable["network"]["phase"], "creating")
                    self.assertIsNotNone(host.network_value)
                elif effect == "connect":
                    self.assertEqual(durable["consumers"]["nginx"]["phase"], "connecting")
                    self.assertIn(transaction.NETWORK, host.containers["nginx"]["networks"])
                else:
                    self.assertEqual(durable["consumers"]["llm-proxy2"]["phase"], "disconnecting")
                    self.assertNotIn(transaction.NETWORK, host.containers["llm-proxy2"]["networks"])
                host.save_failure = None
                resumed = self.resume(host)
                (resumed.rollback if effect == "disconnect" else resumed.attach_consumers)()
                self.assertEqual(len([a for a in host.actions if a[0] == "create"]), 1)
                self.assertEqual(len([a for a in host.actions if a[0] == "connect"]), 2)
                if effect == "disconnect":
                    self.assertEqual(len([a for a in host.actions if a[0] == "disconnect"]), 2)

    def test_creation_return_must_match_inspected_immutable_id(self):
        host = FakeHost()
        host.behavior["create"] = "wrong-receipt"
        host, engine = self.begin(host)
        with self.assertRaisesRegex(transaction.TransactionError, "receipt differs"):
            engine.attach_consumers()
        self.assertEqual(host.journal[-1]["network"]["phase"], "creating")
        self.assertFalse(any(a[0] == "connect" for a in host.actions))

    def test_unjournaled_attachment_is_never_adopted_or_disconnected(self):
        host = FakeHost()
        host.behavior["create"] = "crash"
        host, engine = self.begin(host)
        with self.assertRaises(Crash):
            engine.attach_consumers()
        name = "nginx"
        host.containers[name]["networks"][transaction.NETWORK] = {
            "id": host.network_value["id"],
            "attachment_sha256": checksum("unowned-attachment"),
            "aliases": [name],
            "gw_priority": -1,
        }
        host.network_value["members"][host.containers[name]["id"]] = name
        for operation in ("attach_consumers", "rollback"):
            with (
                self.subTest(operation=operation),
                self.assertRaisesRegex(transaction.TransactionError, "unexpected members"),
            ):
                getattr(self.resume(host), operation)()
        self.assertFalse(any(a[0] in {"connect", "disconnect"} for a in host.actions))

    def test_route_drift_after_connect_is_not_reported_as_accepted(self):
        host = FakeHost()
        host.behavior["connect-nginx"] = "route-drift"
        host, engine = self.begin(host)
        with self.assertRaisesRegex(transaction.TransactionError, "default route drift"):
            engine.attach_consumers()
        self.assertEqual(host.journal[-1]["consumers"]["nginx"]["phase"], "connecting")
        with self.assertRaises(transaction.TransactionError):
            self.resume(host).rollback()
        self.assertFalse(any(a[0] == "disconnect" for a in host.actions))

    def test_existing_network_and_foreign_creation_are_never_adopted(self):
        host, engine = self.begin()
        engine.attach_consumers()
        count = len(host.actions)
        with self.assertRaisesRegex(transaction.TransactionError, "already exists"):
            transaction.NetworkTransaction.begin(host, host.save, "b" * 32)
        self.assertEqual(len(host.actions), count)
        host = FakeHost()
        host.behavior["create"] = "foreign"
        host, engine = self.begin(host)
        with self.assertRaisesRegex(transaction.TransactionError, "ownership"):
            engine.attach_consumers()
        self.assertFalse(any(a[0] == "connect" for a in host.actions))

    def test_unrelated_members_and_network_replacement_refuse_rollback(self):
        changes = (
            lambda h: h.network_value["members"].update({checksum("outsider"): "other"}),
            lambda h: h.network_value["members"].update(
                {checksum("replacement-gateway"): "llm-proxy2"}
            ),
            lambda h: h.network_value.update(id=checksum("replacement-network")),
            lambda h: h.network_value.update(transaction_id="b" * 32),
        )
        for change in changes:
            with self.subTest(change=change):
                host, engine = self.begin()
                engine.attach_consumers()
                change(host)
                with self.assertRaises(transaction.TransactionError):
                    engine.rollback()
                self.assertFalse(any(a[0] == "disconnect" for a in host.actions))

    def test_bridge_identity_is_explicit_and_never_operated_on(self):
        host, engine = self.begin()
        engine.attach_consumers()
        bridge_id = checksum("externally-verified-bridge")
        host.network_value["members"][bridge_id] = transaction.BRIDGE
        with self.assertRaisesRegex(transaction.TransactionError, "exact IDs"):
            engine.attach_consumers()
        engine.record_bridge_id(bridge_id)
        engine.attach_consumers()
        with self.assertRaisesRegex(transaction.TransactionError, "release operator"):
            engine.rollback()
        del host.network_value["members"][bridge_id]  # Simulate external release rollback.
        engine.rollback()
        self.assertFalse(any(bridge_id in action for action in host.actions))

    def test_journal_state_is_detached_and_contains_only_sanitized_fields(self):
        host, engine = self.begin()
        engine.attach_consumers()
        exported = engine.state
        encoded = json.dumps(exported)
        self.assertNotIn("aliases", encoded)
        self.assertNotIn("Config", encoded)
        self.assertNotIn("environment", encoded)
        self.assertNotIn("synthetic-sensitive", encoded)
        exported["consumers"]["nginx"]["id"] = checksum("tampered")
        self.assertEqual(engine.state["consumers"]["nginx"]["id"], checksum("nginx"))
        malformed = engine.state
        malformed["environment"] = "synthetic-sensitive-value"
        with self.assertRaises(transaction.TransactionError) as caught:
            transaction.NetworkTransaction(host, host.save, malformed)
        self.assertNotIn("synthetic-sensitive", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
