"""Pure synthetic recipe and maintained planner/renderer integration; no live runtime.

The concrete model below exercises the maintained resolved-model contract, not a
Docker Compose CLI resolution. Nginx text assertions check the declared request
contract; they are not nginx -t, browser authentication or provider acceptance.
"""

import builtins
import copy
import json
import re
import socket
import subprocess
from dataclasses import replace
from pathlib import Path, PurePosixPath

import network_lab_context as lab
import network_lab_fixtures as fixtures
import network_policy as policy
import pytest
import release
import yaml
from test_network_lab_context import record


def context():
    return lab.from_record(record())


def inputs(ctx=None):
    ctx = ctx or context()
    return fixtures.original_fixture_inputs(ctx, lab_binding=ctx.journal_binding())


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_initial_recipe_passes_real_candidate_and_network_renderers(scenario):
    ctx = replace(context(), scenario=scenario)
    bundle = inputs(ctx)
    original = yaml.safe_load(bundle[fixtures.COMPOSE])
    unchanged = copy.deepcopy((bundle, original))
    planned, receipt = policy.plan_networks(original, lab_context=ctx)
    candidate_data = str(ctx.generation_root / "candidate-data")
    rendered = release.render_network_compose(
        bundle[fixtures.COMPOSE],
        original,
        candidate_data,
        lab_context=ctx,
        review_receipt=receipt,
    )
    actual = yaml.safe_load(rendered)
    release.assert_network_resolved_scope(
        original, actual, candidate_data, lab_context=ctx, review_receipt=receipt
    )
    expected = copy.deepcopy(planned)
    expected["services"][policy.BRIDGE] = release.candidate_service(
        planned["services"][policy.BRIDGE], candidate_data, lab_context=ctx
    )
    assert actual == expected
    assert actual["networks"][policy.NETWORK] == ctx.network_definition()
    assert actual["services"][policy.BRIDGE]["image"] == dict(ctx.images)["candidate"]
    assert actual["services"][policy.BRIDGE]["environment"] == {
        "BRIDGE_TOKEN": fixtures.BRIDGE_TOKEN,
        "BRIDGE_PUBLIC_PATH": "/grok-bridge",
        "BRIDGE_PUBLIC_ORIGIN": "https://grok-lab.invalid",
        "BRIDGE_AUTH_URL": "http://llm-proxy2:3000/api/auth/me",
    }
    assert actual["services"][policy.BRIDGE]["volumes"] == [
        {
            "type": "bind",
            "source": candidate_data,
            "target": "/data",
            "bind": {"create_host_path": False},
        }
    ]
    for role in policy.CONSUMERS:
        baseline, changed = (
            copy.deepcopy(original["services"][role]),
            copy.deepcopy(actual["services"][role]),
        )
        assert changed.pop("networks") == planned["services"][role]["networks"]
        baseline.pop("networks")
        assert changed == baseline
    assert actual["volumes"] == original["volumes"]
    assert (bundle, original) == unchanged
    assert receipt["release_gate_satisfied"] is False
    assert receipt["live_routes_and_reachability_verified"] is False


def test_resolved_output_may_omit_unused_volume_without_removing_its_raw_declaration():
    ctx = context()
    source = inputs(ctx)[fixtures.COMPOSE]
    before = yaml.safe_load(source)
    _, receipt = policy.plan_networks(before, lab_context=ctx)
    candidate_data = str(ctx.generation_root / "candidate-data")
    candidate_source = release.render_network_compose(
        source, before, candidate_data, lab_context=ctx, review_receipt=receipt
    )
    normalized = yaml.safe_load(candidate_source)
    normalized.pop("volumes")  # Explicit simulation of Compose dropping an unused declaration.
    release.assert_network_resolved_scope(
        before, normalized, candidate_data, lab_context=ctx, review_receipt=receipt
    )
    assert b"original-data:" in source and b"original-data:" in candidate_source


def test_nginx_marker_boundaries_preserve_the_rest_of_the_synthetic_config():
    bundle = inputs()
    original = bundle[fixtures.NGINX]
    text = original.decode()
    assert text.count(release.MARKER) == 1
    start = text.index("        " + release.MARKER)
    end = text.index("        # ── from projects-locations.d/fixture-health.conf ──")
    inline = text[start:end].splitlines()[1:]
    assert (
        "\n".join(line[12:] for line in inline if line).encode() + b"\n"
        == bundle[fixtures.FRAGMENT]
    )
    proposed = (
        b"location = /grok-bridge/vnc/websockify {\n"
        b"    set $grok_bridge_host llm-proxy2-grok-bridge;\n"
        b"    proxy_pass http://$grok_bridge_host:8443/vnc/websockify;\n"
        b"}\n"
    )
    rendered = release.render_nginx(original, proposed)
    changed = rendered.decode()
    new_end = changed.index("        # ── from projects-locations.d/fixture-health.conf ──")
    assert changed[:start] == text[:start] and changed[new_end:] == text[end:]
    assert b":6080" in original and b":6080" not in rendered
    assert b"resolver 127.0.0.11 ipv6=off;" in rendered
    assert b"server_name grok-lab.invalid;" in rendered
    assert b"listen 8080;" in rendered
    assert bundle[fixtures.NGINX] == original


def test_original_positive_paths_reach_bridge_api_and_novnc_instead_of_a_synthetic_success():
    fragment = inputs()[fixtures.FRAGMENT].decode()
    assert "return 200" not in fragment
    websocket = re.search(r"location = /grok-bridge/vnc/websockify \{([^}]*)\}", fragment).group(1)
    static = re.search(r"location /grok-bridge/vnc/ \{([^}]*)\}", fragment).group(1)
    api = re.search(r"location /grok-bridge/ \{([^}]*)\}", fragment).group(1)
    for route in (websocket, static, api):
        assert "set $fixture_grok_host llm-proxy2-grok-bridge;" in route
        assert "proxy_set_header Host $host;" in route
        assert "proxy_set_header Cookie $http_cookie;" in route
    assert "rewrite ^/grok-bridge/vnc/websockify$ /websockify break;" in websocket
    assert "rewrite ^/grok-bridge/vnc/(.*)$ /$1 break;" in static
    assert "rewrite ^/grok-bridge(/.*)$ $1 break;" in api
    assert "proxy_pass http://$fixture_grok_host:6080;" in websocket
    assert "proxy_pass http://$fixture_grok_host:6080;" in static
    assert "proxy_pass http://$fixture_grok_host:8443;" in api
    assert "proxy_http_version 1.1;" in websocket
    assert "proxy_set_header Upgrade $http_upgrade;" in websocket
    assert 'proxy_set_header Connection "upgrade";' in websocket
    assert "proxy_set_header Origin $http_origin;" in websocket
    assert "proxy_set_header X-Bridge-Token $http_x_bridge_token;" in api
    assert "proxy_set_header Origin $http_origin;" in api
    assert fixtures.BRIDGE_TOKEN not in fragment  # No injected credential can mask caller failure.
    assert b"return 200 'synthetic-nginx'" in inputs()[fixtures.NGINX]


def test_three_service_identities_pins_aliases_and_original_storage_are_exact():
    ctx = context()
    model = yaml.safe_load(inputs(ctx)[fixtures.COMPOSE])
    assert model["name"] == ctx.project
    assert set(model["services"]) == set(policy.MEMBERS)
    assert model["networks"] == {
        "default": {
            "name": ctx.resource_map()[policy.DEFAULT_NAME],
            "driver": "bridge",
            "internal": False,
        }
    }
    for role, service in model["services"].items():
        assert service["container_name"] == ctx.resource_map()[role]
        assert (
            service["image"] == dict(ctx.images)["original" if role == policy.BRIDGE else "nginx"]
        )
        assert service["networks"] == {"default": {"aliases": [role]}}
        assert service["restart"] == "no" and service["pull_policy"] == "never"
        assert not {
            "ports",
            "expose",
            "build",
            "network_mode",
            "privileged",
            "devices",
            "cap_add",
            "env_file",
            "secrets",
            "external_links",
            "links",
            "pid",
            "ipc",
            "userns_mode",
        } & set(service)
    assert model["volumes"] == {
        "original-data": {"name": ctx.project + "-original-data", "driver": "local"}
    }
    assert model["services"][policy.BRIDGE]["volumes"] == [
        {"type": "volume", "source": "original-data", "target": "/data"}
    ]
    assert dict(ctx.images)["candidate"] not in inputs(ctx)[fixtures.COMPOSE].decode()


def test_bind_paths_are_exact_generation_inputs_and_never_implicitly_created():
    ctx = context()
    bundle = inputs(ctx)
    model = yaml.safe_load(bundle[fixtures.COMPOSE])
    destinations = {}
    for role in policy.CONSUMERS:
        for mount in model["services"][role]["volumes"]:
            source = PurePosixPath(mount["source"])
            assert mount["type"] == "bind"
            assert ctx.generation_root in source.parents
            assert mount["bind"] == {"create_host_path": False}
            relative = source.relative_to(ctx.generation_root).as_posix()
            if mount["target"] == "/etc/nginx/nginx.conf":
                assert mount["read_only"] is True
                assert relative in bundle
                destinations[role] = relative
            else:
                assert mount["target"] == "/var/log/nginx" and mount["read_only"] is False
                assert relative == f"runtime/{role}-logs" and relative not in bundle
    assert destinations == {"nginx": fixtures.NGINX, "llm-proxy2": fixtures.GATEWAY}


def test_gateway_declares_distinct_admin_user_and_default_deny_cookie_contract():
    gateway = inputs()[fixtures.GATEWAY].decode()
    mapping = re.search(r"map \$cookie_codex_lab_session \$fixture_role \{([^}]*)\}", gateway)
    assert mapping is not None
    assert mapping.group(1).splitlines() == [
        "",
        "        default denied;",
        f'        "{fixtures.ADMIN_COOKIE}" admin;',
        f'        "{fixtures.USER_COOKIE}" user;',
        "    ",
    ]
    route = re.search(r"location = /api/auth/me \{(.*?)\n        \}", gateway, re.S)
    assert route is not None
    lines = [line.strip() for line in route.group(1).splitlines() if line.strip()]
    assert lines == [
        "if ($fixture_role = denied) { return 401; }",
        'return 200 \'{"role":"$fixture_role","fixture":"synthetic"}\';',
    ]
    assert "listen 3000;" in gateway
    assert "default_type application/json;" in gateway
    assert "location / { return 404; }" in gateway
    assert "proxy_pass" not in gateway and "http_authorization" not in gateway
    assert (
        "X-Bridge-Token" not in gateway
    )  # Browser-role fixture accepts only its fixed lab cookie.
    assert fixtures.ADMIN_COOKIE != fixtures.USER_COOKIE != fixtures.BRIDGE_TOKEN


def test_fixed_synthetic_inputs_have_no_caller_environment_private_material_or_production_paths(
    monkeypatch,
):
    monkeypatch.setenv("BRIDGE_TOKEN", "private-host-token-must-not-be-used")
    monkeypatch.setenv("HTTP_PROXY", "http://private-proxy.invalid")
    bundle = inputs()
    serialized = b"\n".join(bundle.values()).decode()
    for forbidden in (
        "private-host-token",
        "private-proxy",
        "/mnt/s",
        "/home/dblagbro",
        "/var/backups",
        "/run/docker.sock",
        "/var/run/docker.sock",
        "/etc/letsencrypt",
        "voipguru.org",
        "grok.com",
        "BEGIN PRIVATE KEY",
        "BEGIN CERTIFICATE",
        "${",
    ):
        assert forbidden not in serialized
    bridge = yaml.safe_load(bundle[fixtures.COMPOSE])["services"][policy.BRIDGE]
    assert bridge["environment"] == {
        "BRIDGE_TOKEN": fixtures.BRIDGE_TOKEN,
        "BRIDGE_PUBLIC_PATH": "/grok-bridge",
    }
    assert bridge["healthcheck"]["test"][:3] == ["CMD", "python", "-c"]
    assert "http://127.0.0.1:8443/healthz" in bridge["healthcheck"]["test"][3]


def test_mapping_is_bounded_relative_deterministic_and_detached():
    ctx = context()
    binding = ctx.journal_binding()
    retained = copy.deepcopy(binding)
    first = fixtures.original_fixture_inputs(ctx, lab_binding=binding)
    second = fixtures.original_fixture_inputs(ctx, lab_binding=binding)
    assert first == second and first is not second
    assert set(first) == {
        fixtures.COMPOSE,
        fixtures.NGINX,
        fixtures.GATEWAY,
        fixtures.FRAGMENT,
        fixtures.SEED,
    }
    assert sum(map(len, first.values())) < 16 * 1024
    for name, value in first.items():
        path = PurePosixPath(name)
        assert not path.is_absolute() and ".." not in path.parts and str(path) == name
        assert type(value) is bytes and value.endswith(b"\n") and b"\r" not in value
    assert (
        first[fixtures.SEED]
        == ("synthetic-original-generation\ncontext_sha256=" + ctx.digest() + "\n").encode()
    )
    first[fixtures.COMPOSE] = b"changed-caller-copy"
    first.clear()
    assert inputs(ctx) == second and binding == retained


def test_generator_performs_no_file_process_or_network_operations(monkeypatch):
    def unexpected(*_, **__):
        raise AssertionError("Unexpected host operation")

    with monkeypatch.context() as isolated:
        isolated.setattr(builtins, "open", unexpected)
        isolated.setattr(Path, "open", unexpected)
        isolated.setattr(Path, "resolve", unexpected)
        isolated.setattr(subprocess, "run", unexpected)
        isolated.setattr(subprocess, "Popen", unexpected)
        isolated.setattr(socket, "socket", unexpected)
        produced = inputs()
    assert produced[fixtures.SEED].startswith(b"synthetic-original-generation\ncontext_sha256=")


@pytest.mark.parametrize(
    "field,value",
    [
        ("scenario", "configuration-conflict"),
        ("context_id", "c" * 32),
        ("vm_uuid", "11111111-2222-3333-4444-555555555555"),
        ("engine_id", "other-engine"),
        ("source_commit", "d" * 40),
    ],
)
def test_seed_attribution_changes_with_validated_generation_or_full_binding(field, value):
    original = context()
    changed = replace(original, **{field: value})
    before, after = inputs(original), inputs(changed)
    assert before[fixtures.SEED] != after[fixtures.SEED]
    assert changed.digest().encode() in after[fixtures.SEED]
    assert original.digest().encode() not in after[fixtures.SEED]


@pytest.mark.parametrize(
    "field,value",
    [
        ("vm_uuid", "11111111-2222-3333-4444-555555555555"),
        ("vm_name", "codex-grok-lab-other"),
        ("engine_id", "another-engine"),
        ("source_commit", "f" * 40),
        (
            "images",
            (
                ("original", "sha256:" + "4" * 64),
                ("candidate", "sha256:" + "2" * 64),
                ("nginx", "sha256:" + "3" * 64),
            ),
        ),
    ],
)
def test_full_context_mismatch_refuses_even_when_physical_namespace_is_the_same(
    field, value, monkeypatch
):
    original = context()
    changed = replace(original, **{field: value})
    assert changed.generation_root == original.generation_root
    monkeypatch.setattr(
        fixtures.yaml,
        "safe_dump",
        lambda *_args, **_kwargs: pytest.fail("Rendered mismatched input"),
    )
    with pytest.raises(fixtures.LabFixtureError):
        fixtures.original_fixture_inputs(changed, lab_binding=original.journal_binding())


@pytest.mark.parametrize("binding", [None, {}, True, "private-invalid-binding"])
def test_missing_or_invalid_binding_refuses_with_sanitized_error(binding):
    with pytest.raises(fixtures.LabFixtureError) as caught:
        fixtures.original_fixture_inputs(context(), lab_binding=binding)
    assert "private-invalid-binding" not in str(caught.value)


@pytest.mark.parametrize("value", [None, {}, True, record()])
def test_exact_validated_context_type_is_required(value):
    with pytest.raises(fixtures.LabFixtureError):
        fixtures.original_fixture_inputs(value, lab_binding=context().journal_binding())


def test_caller_binding_mutation_during_serialization_refuses(monkeypatch):
    ctx = context()
    binding = ctx.journal_binding()
    original_dump = fixtures.yaml.safe_dump

    def change(*args, **kwargs):
        binding["context"]["source_commit"] = "c" * 40
        return original_dump(*args, **kwargs)

    monkeypatch.setattr(fixtures.yaml, "safe_dump", change)
    with pytest.raises(fixtures.LabFixtureError):
        fixtures.original_fixture_inputs(ctx, lab_binding=binding)


def test_public_api_offers_no_source_environment_path_or_admission_overrides():
    ctx = context()
    for key, value in {
        "environment": {},
        "root": "/mnt/s",
        "token": "private",
        "approved": True,
    }.items():
        with pytest.raises(TypeError):
            fixtures.original_fixture_inputs(ctx, lab_binding=ctx.journal_binding(), **{key: value})
    bundle = inputs(ctx)
    assert not {"state.private.json", "state.lab.private.json", "release-evidence.json"} & set(
        bundle
    )
    assert "execution_admission" not in json.dumps(yaml.safe_load(bundle[fixtures.COMPOSE]))
