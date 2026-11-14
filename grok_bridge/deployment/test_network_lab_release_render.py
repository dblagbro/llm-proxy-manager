"""Shared Compose preparation on synthetic inputs; no host or admission actions."""

import copy
from dataclasses import replace
from pathlib import PurePosixPath

import network_lab_context as lab
import network_policy as policy
import pytest
import release as r
import yaml
from test_network_lab_context import record
from test_release import network_compose_fixture

REFUSALS = (r.Refused, policy.NetworkPolicyError, r.network_compose.ComposeNetworkError)


def context():
    return lab.from_record(record())


def fixture(ctx=None):
    ctx = ctx or context()
    names, pins = ctx.resource_map(), dict(ctx.images)
    model = {
        "name": ctx.project,
        "services": {
            "unrelated": {
                "image": "unrelated:retained",
                "environment": {"UNCHANGED": "synthetic"},
                "networks": {"default": None},
            },
            "nginx": {
                "image": pins["nginx"],
                "container_name": names["nginx"],
                "networks": {"default": None},
            },
            "llm-proxy2": {
                "image": pins["nginx"],
                "container_name": names["llm-proxy2"],
                "networks": {"default": None},
            },
            r.SERVICE: {
                "image": pins["original"],
                "container_name": names[r.SERVICE],
                "environment": {
                    "BRIDGE_TOKEN": "synthetic-only",
                    "BRIDGE_PUBLIC_PATH": "/grok-bridge",
                    "KEEP": "untouched",
                },
                "volumes": [
                    {"type": "volume", "source": "grok-data", "target": "/data"},
                    {
                        "type": "bind",
                        "source": "/synthetic/read-only",
                        "target": "/keep",
                        "read_only": True,
                    },
                ],
                "restart": "no",
                "networks": {"default": None},
            },
        },
        "volumes": {"grok-data": {"name": ctx.project + "-original-data"}},
        "networks": {"default": {"name": names[policy.DEFAULT_NAME]}},
    }
    _, receipt = policy.plan_networks(model, lab_context=ctx)
    return ctx, model, receipt


def raw(model):
    return (
        ("# retained header\n" + yaml.safe_dump(model, sort_keys=False))
        .encode()
        .replace(b"image: unrelated:retained\n", b"image: unrelated:retained # another owner\n")
    )


def path(ctx):
    return str(ctx.generation_root / "candidate-data")


def render(ctx, before, receipt, *, source=None, data=None):
    return r.render_network_compose(
        raw(before) if source is None else source,
        before,
        path(ctx) if data is None else data,
        lab_context=ctx,
        review_receipt=receipt,
    )


def verify(ctx, before, after, receipt, *, data=None):
    return r.assert_network_resolved_scope(
        before,
        after,
        path(ctx) if data is None else data,
        lab_context=ctx,
        review_receipt=receipt,
    )


@pytest.mark.parametrize("scenario", sorted(lab.SCENARIOS))
def test_combined_shared_path_applies_exact_delta_without_mutating_inputs(scenario):
    ctx, before, receipt = fixture(replace(context(), scenario=scenario))
    snapshots = copy.deepcopy((before, receipt, r.ENV_ADDITIONS, policy.NETWORK_DEFINITION))
    source = raw(before)
    rendered = render(ctx, before, receipt, source=source)
    after = yaml.safe_load(rendered)
    verify(ctx, before, after, receipt)
    candidate = after["services"][r.SERVICE]
    assert candidate["image"] == dict(ctx.images)["candidate"]
    assert candidate["environment"] == {
        **before["services"][r.SERVICE]["environment"],
        "BRIDGE_PUBLIC_ORIGIN": "https://grok-lab.invalid",
        "BRIDGE_AUTH_URL": "http://llm-proxy2:3000/api/auth/me",
    }
    assert candidate["volumes"][0] == {
        "type": "bind",
        "source": path(ctx),
        "target": "/data",
        "bind": {"create_host_path": False},
    }
    assert candidate["volumes"][1:] == before["services"][r.SERVICE]["volumes"][1:]
    assert after["volumes"] == before["volumes"]
    assert after["services"]["unrelated"] == before["services"]["unrelated"]
    assert after["networks"][policy.NETWORK] == ctx.network_definition()
    for role in policy.MEMBERS:
        expected = {policy.NETWORK} if role == r.SERVICE else {"default", policy.NETWORK}
        assert set(after["services"][role]["networks"]) == expected
        assert after["services"][role]["networks"][policy.NETWORK]["aliases"] == [role]
    assert b"# retained header\n" in rendered
    assert b"image: unrelated:retained # another owner\n" in rendered
    assert b"https://www.voipguru.org" not in rendered
    assert (before, receipt, r.ENV_ADDITIONS, policy.NETWORK_DEFINITION) == snapshots
    assert raw(before) == source
    assert receipt["release_gate_satisfied"] is False
    assert receipt["live_routes_and_reachability_verified"] is False


def test_direct_service_renderer_and_scope_use_same_lab_policy():
    ctx, before, _ = fixture()
    source = raw(before)
    rendered = r.render_compose(source, path(ctx), lab_context=ctx)
    a, b = r.service_span(source.decode())
    c, d = r.service_span(rendered.decode())
    assert source[:a] == rendered[:c] and source[b:] == rendered[d:]
    after = yaml.safe_load(rendered)
    r.assert_resolved_scope(before, after, path(ctx), lab_context=ctx)
    assert after["services"][r.SERVICE] == r.candidate_service(
        before["services"][r.SERVICE], path(ctx), lab_context=ctx
    )
    after["services"][r.SERVICE]["environment"]["KEEP"] = "changed"
    with pytest.raises(r.Refused, match="scope"):
        r.assert_resolved_scope(before, after, path(ctx), lab_context=ctx)


@pytest.mark.parametrize("api", ["render", "verify"])
@pytest.mark.parametrize(
    "bad", [None, {}, False, "receipt", "missing-digest", "extra-field", "approval-true"]
)
def test_caller_receipt_required_in_full_before_transform(api, bad, monkeypatch):
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    supplied = copy.deepcopy(receipt)
    if bad == "missing-digest":
        supplied.pop("context_sha256")
    elif bad == "extra-field":
        supplied["unreviewed"] = True
    elif bad == "approval-true":
        supplied["release_gate_satisfied"] = True
    else:
        supplied = bad
    entered = []
    monkeypatch.setattr(
        r.network_compose, "render_networks", lambda *args, **kwargs: entered.append(True)
    )
    with pytest.raises(REFUSALS):
        if api == "render":
            render(ctx, before, supplied)
        else:
            verify(ctx, before, after, supplied)
    assert entered == []


@pytest.mark.parametrize("field", ["vm_uuid", "vm_name", "engine_id", "source_commit", "images"])
def test_full_context_drift_refuses_even_with_same_physical_namespace(field):
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    changes = {
        "vm_uuid": "12345678-1234-1234-1234-123456789abd",
        "vm_name": "codex-grok-lab-other",
        "engine_id": "other-engine",
        "source_commit": "d" * 40,
        "images": tuple(
            (role, "sha256:" + "4" * 64 if role == "candidate" else pin) for role, pin in ctx.images
        ),
    }
    changed = replace(ctx, **{field: changes[field]})
    assert (
        changed.generation_root == ctx.generation_root
        and changed.resource_map() == ctx.resource_map()
    )
    with pytest.raises(REFUSALS):
        render(changed, before, receipt)
    with pytest.raises(REFUSALS):
        verify(changed, before, after, receipt)


@pytest.mark.parametrize(
    "data",
    [
        "relative",
        "root",
        "outside",
        "nested",
        "traversal",
        "double-slash",
        "trailing-slash",
        "dot",
        "path-object",
    ],
)
def test_lab_data_path_is_exact_without_normalization(data):
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    wrong = {
        "relative": "candidate-data",
        "root": str(ctx.generation_root),
        "outside": "/mnt/s/backups/candidate-data",
        "nested": path(ctx) + "/nested",
        "traversal": str(ctx.generation_root) + "/other/../candidate-data",
        "double-slash": str(ctx.generation_root) + "//candidate-data",
        "trailing-slash": path(ctx) + "/",
        "dot": str(ctx.generation_root) + "/./candidate-data",
        "path-object": PurePosixPath(path(ctx)),
    }[data]
    with pytest.raises(r.Refused, match="exact lab candidate-data"):
        render(ctx, before, receipt, data=wrong)
    with pytest.raises(r.Refused, match="exact lab candidate-data"):
        verify(ctx, before, after, receipt, data=wrong)
    with pytest.raises(r.Refused, match="exact lab candidate-data"):
        r.candidate_service(before["services"][r.SERVICE], wrong, lab_context=ctx)


@pytest.mark.parametrize("side", ["raw", "resolved"])
@pytest.mark.parametrize("role", [r.SERVICE, "nginx"])
@pytest.mark.parametrize("image", ["mutable:latest", "candidate", "different-pin"])
def test_raw_and_resolved_original_pins_checked_before_any_transform(
    side, role, image, monkeypatch
):
    ctx, before, receipt = fixture()
    source_model, resolved = copy.deepcopy(before), copy.deepcopy(before)
    target = source_model if side == "raw" else resolved
    target["services"][role]["image"] = {
        "mutable:latest": "mutable:latest",
        "candidate": dict(ctx.images)["candidate"],
        "different-pin": "sha256:" + "5" * 64,
    }[image]
    entered = []
    monkeypatch.setattr(
        r.network_compose, "render_networks", lambda *args, **kwargs: entered.append(True)
    )
    with pytest.raises(REFUSALS):
        render(ctx, resolved, receipt, source=raw(source_model))
    assert entered == []


@pytest.mark.parametrize("side", ["raw", "resolved"])
@pytest.mark.parametrize("field", ["project", *policy.MEMBERS, "default"])
def test_raw_and_resolved_physical_identity_disagreement_refuses(side, field):
    ctx, before, receipt = fixture()
    source_model, resolved = copy.deepcopy(before), copy.deepcopy(before)
    target = source_model if side == "raw" else resolved
    if field == "project":
        target["name"] = "docker"
    elif field == "default":
        target["networks"]["default"]["name"] = "docker_default"
    else:
        target["services"][field]["container_name"] = field
    with pytest.raises(REFUSALS):
        render(ctx, resolved, receipt, source=raw(source_model))


@pytest.mark.parametrize(
    "change",
    ["image", "data", "environment", "unrelated", "network", "volume", "project", "physical-name"],
)
def test_resolved_scope_refuses_changes_outside_exact_reviewed_delta(change):
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    service = after["services"][r.SERVICE]
    if change == "image":
        service["image"] = r.CANDIDATE
    elif change == "data":
        service["volumes"][0]["source"] = "/production"
    elif change == "environment":
        service["environment"]["BRIDGE_PUBLIC_ORIGIN"] = r.ENV_ADDITIONS["BRIDGE_PUBLIC_ORIGIN"]
    elif change == "unrelated":
        after["services"]["unrelated"]["image"] = "changed"
    elif change == "network":
        service["networks"]["default"] = None
    elif change == "volume":
        after["volumes"]["grok-data"]["name"] = "changed"
    elif change == "project":
        after["name"] = "docker"
    else:
        service["container_name"] = r.SERVICE
    with pytest.raises(REFUSALS):
        verify(ctx, before, after, receipt)


def test_only_previously_reviewed_compose_defaults_are_normalized():
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    after["networks"][policy.NETWORK].pop("internal")
    after["networks"][policy.NETWORK]["ipam"] = {}
    after["services"][r.SERVICE]["volumes"][0]["read_only"] = False
    after.pop("volumes")
    verify(ctx, before, after, receipt)
    after["networks"][policy.NETWORK]["internal"] = 0
    with pytest.raises(r.Refused):
        verify(ctx, before, after, receipt)


@pytest.mark.parametrize(
    "kind", ["duplicate-image", "duplicate-service", "anchor", "multiple-documents"]
)
def test_ambiguous_yaml_refuses_before_service_rewrite(kind):
    ctx, before, receipt = fixture()
    source = raw(before)
    old_image = ("    image: " + dict(ctx.images)["original"] + "\n").encode()
    if kind == "duplicate-image":
        source = source.replace(old_image, old_image + old_image)
    elif kind == "duplicate-service":
        source = source.replace(b"volumes:\n", ("  " + r.SERVICE + ": {}\nvolumes:\n").encode(), 1)
    elif kind == "anchor":
        source = source.replace(b"restart: 'no'", b"restart: &choice 'no'")
    else:
        source += b"---\nservices: {}\n"
    with pytest.raises(REFUSALS):
        render(ctx, before, receipt, source=source)
    with pytest.raises(REFUSALS):
        r.render_compose(source, path(ctx), lab_context=ctx)


@pytest.mark.parametrize(
    "source",
    [
        b"",
        "not-bytes",
        b"\xff",
        b"\xef\xbb\xbfservices: {}",
        b"services:\r\n",
        b"x" * (8 * 1024 * 1024 + 1),
    ],
)
def test_lab_source_bounds_and_encoding_refuse_before_parser(source, monkeypatch):
    ctx, before, receipt = fixture()
    parsed = []
    monkeypatch.setattr(r.network_compose, "_parse", lambda *args: parsed.append(True))
    with pytest.raises(r.Refused):
        render(ctx, before, receipt, source=source)
    assert parsed == []


@pytest.mark.parametrize("bad", [{}, False, "lab", object()])
def test_invalid_context_refuses_for_both_wrappers(bad):
    ctx, before, receipt = fixture()
    after = yaml.safe_load(render(ctx, before, receipt))
    with pytest.raises(policy.NetworkPolicyError):
        r.render_network_compose(
            raw(before), before, path(ctx), lab_context=bad, review_receipt=receipt
        )
    with pytest.raises(policy.NetworkPolicyError):
        r.assert_network_resolved_scope(
            before, after, path(ctx), lab_context=bad, review_receipt=receipt
        )


def test_context_subclass_does_not_execute_untrusted_methods():
    class Foreign(lab.LabContext):
        def to_record(self):
            raise AssertionError("Subclass method must not execute")

    ctx, before, receipt = fixture()
    foreign = Foreign(**{field: getattr(ctx, field) for field in ctx.__dataclass_fields__})
    with pytest.raises(policy.NetworkPolicyError):
        r.render_network_compose(
            raw(before), before, path(ctx), lab_context=foreign, review_receipt=receipt
        )


def test_production_default_output_is_unchanged_by_intervening_lab_calls():
    prod_raw, prod_before = network_compose_fixture()
    prior = r.render_network_compose(prod_raw, prod_before, "/private/candidate-data")
    ctx, before, receipt = fixture()
    render(ctx, before, receipt)
    assert r.render_network_compose(prod_raw, prod_before, "/private/candidate-data") == prior
    assert (
        r.render_network_compose(
            prod_raw, prod_before, "/private/candidate-data", lab_context=None, review_receipt=None
        )
        == prior
    )
    assert yaml.safe_load(prior)["services"][r.SERVICE]["image"] == r.CANDIDATE
    assert (
        yaml.safe_load(prior)["services"][r.SERVICE]["environment"]["BRIDGE_PUBLIC_ORIGIN"]
        == r.ENV_ADDITIONS["BRIDGE_PUBLIC_ORIGIN"]
    )


def test_production_refuses_lab_receipts_names_and_paths():
    ctx, before, receipt = fixture()
    prod_raw, prod_before = network_compose_fixture()
    for operation in (
        lambda: r.render_network_compose(
            prod_raw, prod_before, "/private/candidate-data", review_receipt=receipt
        ),
        lambda: r.assert_network_resolved_scope(
            prod_before, prod_before, "/private/candidate-data", review_receipt=receipt
        ),
        lambda: r.candidate_service(before["services"][r.SERVICE], "/private/candidate-data"),
        lambda: r.candidate_service(prod_before["services"][r.SERVICE], path(ctx)),
        lambda: r.render_compose(raw(before), "/private/candidate-data"),
        lambda: r.render_network_compose(raw(before), before, "/private/candidate-data"),
    ):
        with pytest.raises(REFUSALS):
            operation()


@pytest.mark.parametrize(
    "syntax,source",
    [
        (syntax, source)
        for syntax in ("short", "long")
        for source in ("/production/data", "./data", "../data", "nested/data", "${DATA}", "")
    ]
    + [("long", source) for source in (None, False, 123)],
)
def test_lab_original_data_is_concrete_named_volume_only(syntax, source):
    ctx, before, receipt = fixture()
    mount = (
        f"{source}:/data"
        if syntax == "short"
        else {"type": "volume", "source": source, "target": "/data"}
    )
    before["services"][r.SERVICE]["volumes"][0] = mount
    snapshot = copy.deepcopy(before)
    with pytest.raises(r.Refused, match="named-volume"):
        r.candidate_service(before["services"][r.SERVICE], path(ctx), lab_context=ctx)
    with pytest.raises(r.Refused, match="named-volume"):
        render(ctx, before, receipt)
    assert before == snapshot


@pytest.mark.parametrize("syntax", ["short", "long"])
def test_concrete_named_volume_forms_work_and_stale_raw_source_refuses(syntax):
    ctx, before, receipt = fixture()
    if syntax == "short":
        before["services"][r.SERVICE]["volumes"][0] = "grok-data:/data:rw"
    verify(ctx, before, yaml.safe_load(render(ctx, before, receipt)), receipt)
    assert (
        r.candidate_service(before["services"][r.SERVICE], path(ctx), lab_context=ctx)["volumes"][
            0
        ]["type"]
        == "bind"
    )
    source_model = copy.deepcopy(before)
    source_model["services"][r.SERVICE]["volumes"][0] = "different-data:/data"
    with pytest.raises(r.Refused, match="Raw and resolved"):
        render(ctx, before, receipt, source=raw(source_model))
