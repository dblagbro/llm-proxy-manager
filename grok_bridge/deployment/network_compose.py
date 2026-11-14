"""Pure, bounded edits to private canonical Compose source; never a deployment.

Call after the release operator's image/environment/data renderer. Only the three
network fields and the new top-level network entry may change. Unrelated text is
preserved verbatim. Comments inside a replaced field retain their text as comment
lines; comments outside those field spans remain byte-for-byte unchanged.

Inputs/returned bytes may contain secrets: keep them private and never log them.
Resolved Compose validation against policy.plan_networks remains mandatory: raw
YAML parsing does not establish interpolation, physical topology or live state.
"""

import copy
import json

import network_policy as policy
import yaml


class ComposeNetworkError(ValueError):
    """Sanitized refusal of ambiguous source or an unreviewed transformation."""


def _require(condition, message):
    if not condition:
        raise ComposeNetworkError(message)


class _UniqueLoader(yaml.SafeLoader):
    """Reject duplicate/merge/non-string keys throughout the private document."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        _require(key_node.tag != "tag:yaml.org,2002:merge", "YAML merge keys require review")
        key = loader.construct_object(key_node, deep=deep)
        _require(type(key) is str and key not in result, "Ambiguous YAML mapping key")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _json(value):
    try:
        policy._json_value(value)
        return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))
    except (policy.NetworkPolicyError, TypeError, ValueError):
        raise ComposeNetworkError("Unsupported semantic value") from None


def _parse(text):
    try:
        for token in yaml.scan(text):
            _require(
                not isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken)),
                "YAML anchors or aliases require separate review",
            )
        documents = list(yaml.load_all(text, Loader=_UniqueLoader))
        nodes = list(yaml.compose_all(text, Loader=_UniqueLoader))
        _require(
            len(documents) == 1 and type(documents[0]) is dict,
            "Expected one Compose mapping document",
        )
        _json(documents[0])
        return documents[0], nodes[0]
    except yaml.YAMLError:
        raise ComposeNetworkError("Invalid or unsupported YAML source") from None


def _entries(node):
    _require(
        isinstance(node, yaml.MappingNode) and not node.flow_style,
        "Expected canonical block mapping",
    )
    return {key.value: (key, value) for key, value in node.value}


def _key_indent(text, key, spaces):
    start = text.rfind("\n", 0, key.start_mark.index) + 1
    _require(text[start : key.start_mark.index] == " " * spaces, "Unexpected canonical indentation")
    return start


def _content_end(node):
    if isinstance(node, yaml.ScalarNode) or node.flow_style:
        return node.end_mark.index
    children = (
        [child for pair in node.value for child in pair]
        if isinstance(node, yaml.MappingNode)
        else node.value
    )
    return max((_content_end(child) for child in children), default=node.end_mark.index)


def _after_content(text, node):
    end = _content_end(node)
    if end > 0 and text[end - 1] == "\n":
        return end
    newline = text.find("\n", end)
    return len(text) if newline < 0 else newline + 1


def _field_comments(text):
    """Retain comments without treating quoted # characters as comments."""
    result = []
    for line in text.splitlines(keepends=True):
        quote = None
        escaped = False
        index = 0
        while index < len(line):
            char = line[index]
            if quote == '"':
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    quote = None
            elif quote == "'":
                if char == "'" and index + 1 < len(line) and line[index + 1] == "'":
                    index += 1
                elif char == "'":
                    quote = None
            elif char in "\"'":
                quote = char
            elif char == "#" and (index == 0 or line[index - 1].isspace()):
                indent = line[: len(line) - len(line.lstrip(" "))]
                result.append(indent + line[index:].rstrip("\n") + "\n")
                break
            index += 1
    return "".join(result)


def _logical_networks(service):
    _require(
        type(service) is dict and "network_mode" not in service,
        "Unsupported target service topology",
    )
    if "networks" not in service:
        return {policy.DEFAULT: None}
    value = service["networks"]
    if type(value) is list:
        _require(
            all(type(name) is str for name in value) and len(value) == len(set(value)),
            "Ambiguous network list",
        )
        return dict.fromkeys(value)
    _require(type(value) is dict, "Unsupported network field")
    return {
        name: None if options == {} else copy.deepcopy(options) for name, options in value.items()
    }


def _desired(original_networks, desired_networks, dedicated_definition):
    _json(original_networks)
    _json(desired_networks)
    _json(dedicated_definition)
    _require(
        type(original_networks) is dict and set(original_networks) == set(policy.MEMBERS),
        "Original network review is incomplete",
    )
    synthetic = {
        "services": {
            name: {"networks": copy.deepcopy(original_networks[name])} for name in policy.MEMBERS
        },
        "networks": {policy.DEFAULT: {"name": policy.DEFAULT_NAME, "ipam": {}}},
    }
    try:
        planned, _ = policy.plan_networks(synthetic)
    except policy.NetworkPolicyError:
        raise ComposeNetworkError("Original network policy requires review") from None
    expected = {name: planned["services"][name]["networks"] for name in policy.MEMBERS}
    _require(
        _json(desired_networks) == _json(expected),
        "Desired service networks differ from fixed policy",
    )
    _require(
        _json(dedicated_definition) == _json(policy.NETWORK_DEFINITION),
        "Dedicated definition differs from fixed policy",
    )
    return copy.deepcopy(expected)


def _dump(value, indent):
    return "".join(
        " " * indent + line + "\n"
        for line in yaml.safe_dump(
            value, sort_keys=False, allow_unicode=True, default_flow_style=False
        ).splitlines()
    )


def render_networks(raw, *, original_networks, desired_networks, dedicated_definition):
    """Return private transformed bytes after exact-scope semantic verification.

    Original mappings are reviewed logical mappings (implicit/list default becomes
    {default: None}); desired mappings/definition must match the fixed policy.
    This only supports LF, UTF-8, two-space canonical service blocks. Anchors,
    aliases, merge keys, duplicate keys and multiple documents always refuse.
    """
    _require(
        type(raw) is bytes and 0 < len(raw) <= 8 * 1024 * 1024,
        "Source size or type requires review",
    )
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise ComposeNetworkError("Source must be UTF-8") from None
    _require(
        "\r" not in text and not text.startswith("\ufeff"),
        "Unsupported source encoding or line endings",
    )
    desired = _desired(original_networks, desired_networks, dedicated_definition)
    before, document = _parse(text)
    root = _entries(document)
    _require(
        "services" in root and type(before.get("services")) is dict,
        "Missing canonical services mapping",
    )
    services_key, services_node = root["services"]
    _key_indent(text, services_key, 0)
    services = _entries(services_node)
    _require(set(policy.MEMBERS) <= set(services), "Required target service is absent")
    edits = []
    for name in policy.MEMBERS:
        service_key, service_node = services[name]
        _key_indent(text, service_key, 2)
        fields = _entries(service_node)
        _require(bool(fields), "Empty target service requires review")
        for field_key, _ in fields.values():
            _key_indent(text, field_key, 4)
        _require(
            _json(_logical_networks(before["services"][name])) == _json(original_networks[name]),
            "Raw network field changed since review",
        )
        replacement = _dump({"networks": desired[name]}, 4)
        if "networks" in fields:
            key, value = fields["networks"]
            start, end = _key_indent(text, key, 4), _after_content(text, value)
            replacement = _field_comments(text[start:end]) + replacement
        else:
            start = end = _after_content(text, service_node)
            if start > 0 and text[start - 1] != "\n":
                replacement = "\n" + replacement
        edits.append((start, end, replacement))
    expected = copy.deepcopy(before)
    for name in policy.MEMBERS:
        expected["services"][name]["networks"] = desired[name]
    if "networks" in root:
        key, value = root["networks"]
        _key_indent(text, key, 0)
        network_nodes = _entries(value)
        _require(
            type(before.get("networks")) is dict and policy.NETWORK not in network_nodes,
            "Dedicated network declaration collides",
        )
        for network_key, _ in network_nodes.values():
            _key_indent(text, network_key, 2)
        position = _after_content(text, value)
        addition = _dump({policy.NETWORK: dedicated_definition}, 2)
    else:
        position = _after_content(text, document)
        addition = _dump({"networks": {policy.NETWORK: dedicated_definition}}, 0)
        expected["networks"] = {}
    expected["networks"][policy.NETWORK] = copy.deepcopy(dedicated_definition)
    if position > 0 and text[position - 1] != "\n":
        addition = "\n" + addition
    edits.append((position, position, addition))
    # Coincident insertions at EOF are legitimate (last service + new root key).
    # Apply top-level insertion first so the service field ends up before it.
    output = text
    last_start = len(text) + 1
    ordered = sorted(
        enumerate(edits), key=lambda item: (item[1][0], item[1][1], item[0]), reverse=True
    )
    for _, (start, end, replacement) in ordered:
        _require(
            0 <= start <= end <= len(text) and end <= last_start,
            "Overlapping source edits require review",
        )
        output = output[:start] + replacement + output[end:]
        last_start = start
    after, _ = _parse(output)
    _require(
        _json(after) == _json(expected), "Rendered Compose changed outside reviewed network scope"
    )
    return output.encode("utf-8")
