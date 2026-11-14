"""Journaled preparation primitive; no real host adapter or deployment entrypoint.

The injected host returns sanitized projections, never raw Docker inspect data.
The caller must hold an exclusive maintenance lock, verify engine/Compose gateway
priority support, and integrate configuration CAS and the Grok release operator.
Config fingerprints exclude expected network attachment changes; route fingerprints
cover the original default route. Original attachment fingerprints include its
network ID, endpoint identity, addressing, aliases and gateway priority.

Only consumers are attached/detached here. A recorded bridge ID is permission for
an externally verified release operation to join the network, not release approval.
Restore/remove that bridge membership through its release operator before calling
rollback. Networks are never deleted, including empty networks after rollback.
A changed default route is a refusal requiring operator reconciliation; this
primitive deliberately does not claim automatic rollback can safely repair drift.
"""

import copy
import re

import network_policy as policy

NETWORK = policy.NETWORK_NAME
DEFAULT = policy.DEFAULT_NAME
BRIDGE = policy.BRIDGE
CONSUMERS = policy.CONSUMERS
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TX = re.compile(r"[0-9a-f]{32}\Z")


class TransactionError(RuntimeError):
    """A sanitized refusal; inspect the private journal before recovery."""


def _require(condition, message):
    if not condition:
        raise TransactionError(message)


def _hex(value):
    return type(value) is str and bool(_HEX.fullmatch(value))


def _enum(value, choices):
    return type(value) is str and value in choices


def _fields(value, names):
    _require(type(value) is dict and set(value) == set(names), "Invalid sanitized projection")


def _container(value, name):
    _fields(value, ("name", "id", "config_sha256", "default_route_sha256", "networks"))
    _require(value["name"] == name and _hex(value["id"]), "Container identity is invalid")
    _require(
        _hex(value["config_sha256"]) and _hex(value["default_route_sha256"]),
        "Container fingerprint is invalid",
    )
    _require(type(value["networks"]) is dict, "Invalid membership projection")
    for network, attachment in value["networks"].items():
        _require(_enum(network, {DEFAULT, NETWORK}), "Unreviewed container membership")
        _fields(attachment, ("id", "attachment_sha256", "aliases", "gw_priority"))
        _require(
            _hex(attachment["id"]) and _hex(attachment["attachment_sha256"]),
            "Invalid attachment identity",
        )
        _require(
            type(attachment["aliases"]) is list
            and all(type(x) is str for x in attachment["aliases"]),
            "Invalid attachment aliases",
        )
        _require(type(attachment["gw_priority"]) is int, "Invalid gateway priority")
    return value


def _network(value, txid):
    _fields(value, ("name", "id", "transaction_id", "driver", "internal", "members"))
    _require(value["name"] == NETWORK and _hex(value["id"]), "Network identity is invalid")
    _require(value["transaction_id"] == txid, "Network ownership is not established")
    _require(
        value["driver"] == "bridge" and value["internal"] is False, "Network configuration differs"
    )
    _require(type(value["members"]) is dict, "Invalid network members")
    _require(
        all(
            _hex(cid) and _enum(name, {*CONSUMERS, BRIDGE})
            for cid, name in value["members"].items()
        ),
        "Unapproved network member",
    )
    return value


class NetworkTransaction:
    """Inject host and save callbacks; inspect `state` for a detached journal copy.

    Host methods: container(name), network(name), create_network(name, txid),
    connect(network_id, container_id, alias, gw_priority), and
    disconnect(network_id, container_id). create_network returns its immutable ID.
    Inspections return the exact projection schemas checked above; missing network
    returns None. Methods may raise on ambiguous transport errors. Save must durably
    persist its supplied sanitized JSON before returning, without exposing secrets.
    """

    def __init__(self, host, save, state):
        self.host, self.save = host, save
        self._validate_state(state)
        self._state = copy.deepcopy(state)

    @property
    def state(self):
        return copy.deepcopy(self._state)

    @staticmethod
    def _validate_state(state):
        _fields(state, ("schema", "transaction_id", "phase", "network", "consumers", "bridge_id"))
        _require(
            type(state["schema"]) is int and state["schema"] == 1, "Unsupported journal schema"
        )
        _require(
            type(state["transaction_id"]) is str and bool(_TX.fullmatch(state["transaction_id"])),
            "Invalid transaction identity",
        )
        _require(
            _enum(state["phase"], {"prepared", "attached", "rolling_back", "rolled_back"}),
            "Invalid transaction phase",
        )
        _require(state["bridge_id"] is None or _hex(state["bridge_id"]), "Invalid bridge identity")
        net = state["network"]
        _fields(net, ("phase", "id", "proof"))
        _require(_enum(net["phase"], {"planned", "creating", "owned"}), "Invalid network phase")
        _require(net["id"] is None or _hex(net["id"]), "Invalid journal network identity")
        _require(
            net["proof"] is None
            or _enum(net["proof"], {"creation_return_verified", "creation_intent_reconciled"}),
            "Invalid network proof",
        )
        _require(
            (net["id"] is not None and net["proof"] is not None)
            if net["phase"] == "owned"
            else (net["id"] is None and net["proof"] is None),
            "Incomplete network ownership proof",
        )
        _fields(state["consumers"], CONSUMERS)
        for record in state["consumers"].values():
            _fields(
                record,
                (
                    "id",
                    "config_sha256",
                    "default_route_sha256",
                    "default_network_id",
                    "default_attachment_sha256",
                    "phase",
                    "owned",
                    "attachment_sha256",
                ),
            )
            _require(
                all(
                    _hex(record[key])
                    for key in (
                        "id",
                        "config_sha256",
                        "default_route_sha256",
                        "default_network_id",
                        "default_attachment_sha256",
                    )
                ),
                "Invalid journal fingerprint",
            )
            _require(
                _enum(
                    record["phase"],
                    {"pending", "connecting", "attached", "disconnecting", "detached"},
                ),
                "Invalid attachment phase",
            )
            _require(type(record["owned"]) is bool, "Invalid attachment ownership")
            _require(
                record["attachment_sha256"] is None or _hex(record["attachment_sha256"]),
                "Invalid attachment proof",
            )
            _require(
                record["owned"] == (record["attachment_sha256"] is not None),
                "Incomplete attachment proof",
            )
            _require(
                record["phase"] not in {"attached", "disconnecting"} or record["owned"],
                "Missing attachment ownership",
            )
            _require(
                record["phase"] != "pending" or not record["owned"],
                "Pending attachment cannot be owned",
            )
        _require(
            len({record["id"] for record in state["consumers"].values()}) == len(CONSUMERS),
            "Duplicate journal consumer identity",
        )

    def _checkpoint(self, change):
        next_state = self.state
        change(next_state)
        self._validate_state(next_state)
        try:
            self.save(copy.deepcopy(next_state))
        except Exception:  # noqa: BLE001 -- injected persistence errors may contain private data.
            raise TransactionError(
                "Journal persistence failed; no next action authorized"
            ) from None
        self._state = next_state

    def _read(self, method, *args):
        try:
            return getattr(self.host, method)(*args)
        except Exception:  # noqa: BLE001 -- injected adapter errors must not leak raw inspection data.
            raise TransactionError("Host inspection failed; reconcile before continuing") from None

    def _attempt(self, method, *args):
        try:
            return getattr(self.host, method)(*args)
        except Exception:  # noqa: BLE001 -- reconcile observations; never log private adapter errors.
            return None

    @classmethod
    def begin(cls, host, save, transaction_id):
        _require(
            type(transaction_id) is str and bool(_TX.fullmatch(transaction_id)),
            "Invalid transaction identity",
        )
        initial = {
            "schema": 1,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "network": {"phase": "planned", "id": None, "proof": None},
            "consumers": {},
            "bridge_id": None,
        }
        try:
            _require(host.network(NETWORK) is None, "Dedicated network already exists")
            for name in CONSUMERS:
                current = _container(host.container(name), name)
                _require(set(current["networks"]) == {DEFAULT}, "Initial consumer topology differs")
                attachment = current["networks"][DEFAULT]
                _require(attachment["gw_priority"] >= 0, "Original default route requires review")
                initial["consumers"][name] = {
                    "id": current["id"],
                    "config_sha256": current["config_sha256"],
                    "default_route_sha256": current["default_route_sha256"],
                    "default_network_id": attachment["id"],
                    "default_attachment_sha256": attachment["attachment_sha256"],
                    "phase": "pending",
                    "owned": False,
                    "attachment_sha256": None,
                }
        except TransactionError:
            raise
        except Exception:  # noqa: BLE001 -- never export private initial inspection errors.
            raise TransactionError("Initial host inspection failed") from None
        _require(
            len({r["id"] for r in initial["consumers"].values()}) == len(CONSUMERS),
            "Duplicate consumer identity",
        )
        _require(
            len({r["default_network_id"] for r in initial["consumers"].values()}) == 1,
            "Original network identities differ",
        )
        result = cls(host, save, initial)
        result._checkpoint(lambda _: None)
        return result

    def _audit(self, network, allow_missing=False):
        _require(
            network is not None or self._state["network"]["phase"] != "owned",
            "Owned network disappeared",
        )
        if network is not None:
            _network(network, self._state["transaction_id"])
            _require(network["id"] == self._state["network"]["id"], "Owned network was replaced")
        observed = {}
        expected_members = {}
        for name, record in self._state["consumers"].items():
            current = _container(self._read("container", name), name)
            _require(current["id"] == record["id"], "Consumer was replaced; preserve replacement")
            for key in ("config_sha256", "default_route_sha256"):
                _require(
                    current[key] == record[key], "Consumer configuration or default route drift"
                )
            default = current["networks"].get(DEFAULT)
            _require(
                default is not None
                and default["id"] == record["default_network_id"]
                and default["attachment_sha256"] == record["default_attachment_sha256"],
                "Original membership drift",
            )
            extra = current["networks"].get(NETWORK)
            if extra is not None:
                _require(
                    network is not None and extra["id"] == network["id"],
                    "Dedicated membership identity differs",
                )
                _require(
                    record["phase"] in {"connecting", "attached", "disconnecting"},
                    "Unjournaled consumer attachment",
                )
                _require(
                    extra["aliases"] == [name] and extra["gw_priority"] == -1,
                    "Dedicated attachment configuration differs",
                )
                if record["owned"]:
                    _require(
                        extra["attachment_sha256"] == record["attachment_sha256"],
                        "Owned attachment was replaced",
                    )
                expected_members[record["id"]] = name
            elif record["phase"] == "attached" and not allow_missing:
                raise TransactionError("Owned consumer attachment disappeared")
            observed[name] = extra
        if network is not None:
            bridge_id = self._state["bridge_id"]
            if bridge_id is not None and network["members"].get(bridge_id) == BRIDGE:
                expected_members[bridge_id] = BRIDGE
            _require(
                network["members"] == expected_members,
                "Network membership does not match approved exact IDs",
            )
        return observed

    def _ensure_network(self, allow_create=True):
        phase = self._state["network"]["phase"]
        network = self._read("network", NETWORK)
        if phase == "owned":
            _require(network is not None, "Owned network disappeared")
            self._audit(network)
            return
        if phase == "planned":
            _require(network is None, "Dedicated network collision")
            self._audit(None)
            self._checkpoint(lambda state: state["network"].update(phase="creating"))
        created_id = None
        if network is None:
            _require(allow_create, "Network creation is prohibited during rollback")
            created_id = self._attempt("create_network", NETWORK, self._state["transaction_id"])
            network = self._read("network", NETWORK)
        _require(network is not None, "Network creation not observed; reconcile before retry")
        _network(network, self._state["transaction_id"])
        _require(
            network["id"]
            not in {r["default_network_id"] for r in self._state["consumers"].values()},
            "Dedicated network identity collides with original",
        )
        _require(not network["members"], "New network has unexpected members")
        if created_id is not None:
            _require(
                _hex(created_id) and created_id == network["id"], "Network creation receipt differs"
            )
        proof = (
            "creation_return_verified" if created_id is not None else "creation_intent_reconciled"
        )
        self._checkpoint(
            lambda state: state["network"].update(phase="owned", id=network["id"], proof=proof)
        )
        self._audit(network)

    def attach_consumers(self):
        _require(self._state["phase"] in {"prepared", "attached"}, "Transaction is not attachable")
        self._ensure_network()
        for name in CONSUMERS:
            observed = self._audit(self._read("network", NETWORK))
            record = self._state["consumers"][name]
            if record["phase"] == "attached":
                continue
            _require(
                record["phase"] in {"pending", "connecting"}, "Attachment phase is not resumable"
            )
            if record["phase"] == "pending":
                self._checkpoint(
                    lambda state, name=name: state["consumers"][name].update(phase="connecting")
                )
            if observed[name] is None:
                self._attempt("connect", self._state["network"]["id"], record["id"], name, -1)
            observed = self._audit(self._read("network", NETWORK))
            _require(observed[name] is not None, "Connection not observed; reconcile before retry")
            self._checkpoint(
                lambda state, name=name, proof=observed[name]["attachment_sha256"]: state[
                    "consumers"
                ][name].update(
                    phase="attached",
                    owned=True,
                    attachment_sha256=proof,
                )
            )
        self._checkpoint(lambda state: state.update(phase="attached"))
        return self.state

    def record_bridge_id(self, container_id):
        """Journal an externally verified exact ID; never operate on the bridge."""
        _require(
            self._state["phase"] == "attached" and _hex(container_id),
            "Bridge identity cannot be recorded",
        )
        _require(
            self._state["bridge_id"] in (None, container_id), "Bridge identity already recorded"
        )
        _require(
            container_id not in {r["id"] for r in self._state["consumers"].values()},
            "Bridge identity collides with consumer",
        )
        self._checkpoint(lambda state: state.update(bridge_id=container_id))
        return self.state

    def rollback(self):
        network = self._read("network", NETWORK)
        if self._state["network"]["phase"] != "owned":
            if network is None:
                self._audit(None, allow_missing=True)
                self._checkpoint(lambda state: state.update(phase="rolled_back"))
                return self.state
            _require(self._state["network"]["phase"] == "creating", "Unowned network collision")
            self._ensure_network(allow_create=False)
            network = self._read("network", NETWORK)
        _require(network is not None, "Owned network disappeared")
        self._audit(network, allow_missing=True)
        _require(
            BRIDGE not in network["members"].values(),
            "Restore bridge through release operator before network rollback",
        )
        self._checkpoint(lambda state: state.update(phase="rolling_back"))
        for name in reversed(CONSUMERS):
            observed = self._audit(self._read("network", NETWORK), allow_missing=True)
            record = self._state["consumers"][name]
            if observed[name] is not None:
                _require(
                    record["phase"] in {"connecting", "attached", "disconnecting"},
                    "Attachment has no transaction intent",
                )
                if not record["owned"]:
                    self._checkpoint(
                        lambda state, name=name, proof=observed[name]["attachment_sha256"]: state[
                            "consumers"
                        ][name].update(owned=True, attachment_sha256=proof)
                    )
                self._checkpoint(
                    lambda state, name=name: state["consumers"][name].update(phase="disconnecting")
                )
                self._attempt("disconnect", self._state["network"]["id"], record["id"])
                observed = self._audit(self._read("network", NETWORK), allow_missing=True)
                _require(
                    observed[name] is None, "Disconnection not observed; reconcile before retry"
                )
            self._checkpoint(
                lambda state, name=name: state["consumers"][name].update(phase="detached")
            )
        self._checkpoint(lambda state: state.update(phase="rolled_back"))
        return self.state
