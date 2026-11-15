"""Synthetic canonical-source checks; no host operations or private inputs."""

import copy
import unittest

import network_compose as renderer
import network_policy as policy
import yaml

RAW = """# Canonical header — résumé.
name: docker
services:
  nginx: # preserve service comment
    image: nginx:synthetic
    ports:
      - '443:443'
    # Preserve this unrelated Nginx comment.
  llm-proxy2:
    image: gateway:synthetic
    networks: [default] # Gateway network comment.
    environment:
      TOKEN: synthetic-private-value
  llm-proxy2-grok-bridge:
    image: sha256:prepared-candidate
    environment:
      BRIDGE_TOKEN: synthetic-private-bridge-value
      BRIDGE_AUTH_URL: http://llm-proxy2:3000/api/auth/me
    volumes:
      - type: bind
        source: /private/prepared-generation
        target: /data
    networks:
      - default # Grok network comment.
  unrelated:
    image: retained:unchanged
    command: |
      echo 'networks: untouched literal'
      # A script comment remains part of the scalar.
networks:
  default:
  postgres-network:
    driver: bridge
  esnet:
    name: docker_esnet
# Preserve this top-level comment.
volumes:
  retained:
    external: true
""".encode()


def specification(original=None):
    original = copy.deepcopy(original or {name: {"default": None} for name in policy.MEMBERS})
    model = {
        "services": {name: {"networks": original[name]} for name in policy.MEMBERS},
        "networks": {"default": {"name": policy.DEFAULT_NAME, "ipam": {}}},
    }
    planned, _ = policy.plan_networks(model)
    return {
        "original_networks": original,
        "desired_networks": {
            name: planned["services"][name]["networks"] for name in policy.MEMBERS
        },
        "dedicated_definition": copy.deepcopy(policy.NETWORK_DEFINITION),
    }


class ComposeRendererTests(unittest.TestCase):
    def test_canonical_implicit_nginx_and_list_consumers(self):
        before = yaml.safe_load(RAW)
        spec = specification()
        result = renderer.render_networks(RAW, **spec)
        after = yaml.safe_load(result)
        expected = copy.deepcopy(before)
        for name in policy.MEMBERS:
            expected["services"][name]["networks"] = spec["desired_networks"][name]
        expected["networks"][policy.NETWORK] = spec["dedicated_definition"]
        self.assertEqual(after, expected)
        self.assertEqual(after["services"]["nginx"]["networks"][policy.NETWORK]["gw_priority"], -1)
        self.assertEqual(set(after["services"][policy.BRIDGE]["networks"]), {policy.NETWORK})

    def test_image_environment_data_and_unrelated_text_stay_exact(self):
        result = renderer.render_networks(RAW, **specification())
        untouched = (
            (
                b"    image: sha256:prepared-candidate\n    environment:\n"
                b"      BRIDGE_TOKEN: synthetic-private-bridge-value\n"
                b"      BRIDGE_AUTH_URL: http://llm-proxy2:3000/api/auth/me\n"
                b"    volumes:\n      - type: bind\n        source: /private/prepared-generation\n"
                b"        target: /data\n"
            ),
            (
                b"  unrelated:\n    image: retained:unchanged\n    command: |\n"
                b"      echo 'networks: untouched literal'\n"
                b"      # A script comment remains part of the scalar.\n"
            ),
            (
                b"  default:\n  postgres-network:\n    driver: bridge\n"
                b"  esnet:\n    name: docker_esnet\n"
            ),
            b"# Preserve this top-level comment.\nvolumes:\n  retained:\n    external: true\n",
        )
        for fragment in untouched:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, result)
        self.assertTrue(
            result.startswith("# Canonical header — résumé.\nname: docker\nservices:\n".encode())
        )

    def test_comments_survive_without_duplicate_or_lost_text(self):
        result = renderer.render_networks(RAW, **specification()).decode()
        for comment in (
            "# preserve service comment",
            "# Preserve this unrelated Nginx comment.",
            "# Gateway network comment.",
            "# Grok network comment.",
            "# Preserve this top-level comment.",
        ):
            self.assertEqual(result.count(comment), 1)
        self.assertIn("    # Gateway network comment.\n    networks:\n", result)

    def test_explicit_default_mapping_and_empty_options_normalize(self):
        for field in (
            b"networks: {default: null}",
            b"networks: {default: {}}",
            b"networks:\n      default:",
        ):
            raw = RAW.replace(b"networks: [default]", field)
            with self.subTest(field=field):
                result = renderer.render_networks(raw, **specification())
                self.assertEqual(
                    yaml.safe_load(result)["services"]["llm-proxy2"]["networks"]["default"], None
                )

    def test_existing_default_options_preserved_and_review_not_mutated(self):
        options = {"aliases": ["gateway-private"], "priority": 7, "gw_priority": 3}
        original = {name: {"default": None} for name in policy.MEMBERS}
        original["llm-proxy2"]["default"] = options
        spec = specification(original)
        snapshot = copy.deepcopy(spec)
        raw = RAW.replace(
            b"    networks: [default] # Gateway network comment.\n",
            b"    networks: # Gateway network comment.\n      default:\n"
            b"        # Retain this option comment.\n        aliases: [gateway-private]\n"
            b"        priority: 7\n        gw_priority: 3\n",
        )
        result = renderer.render_networks(raw, **spec)
        self.assertEqual(
            yaml.safe_load(result)["services"]["llm-proxy2"]["networks"]["default"], options
        )
        self.assertIn(b"# Retain this option comment.", result)
        self.assertEqual(spec, snapshot)

    def test_missing_top_networks_and_last_service_insertion_at_eof(self):
        raw = (
            b"services:\n  nginx:\n    image: nginx\n  llm-proxy2:\n    image: gateway\n"
            b"  llm-proxy2-grok-bridge:\n    image: candidate"
        )
        result = renderer.render_networks(raw, **specification())
        model = yaml.safe_load(result)
        self.assertEqual(model["networks"][policy.NETWORK], policy.NETWORK_DEFINITION)
        self.assertEqual(model["services"][policy.BRIDGE]["image"], "candidate")
        self.assertEqual(set(model["services"][policy.BRIDGE]["networks"]), {policy.NETWORK})

    def test_explicit_document_end_and_trailing_comments_preserved(self):
        raw = (
            b"---\nservices:\n  nginx:\n    image: nginx\n  llm-proxy2:\n    image: gateway\n"
            b"  llm-proxy2-grok-bridge:\n    image: candidate\n# End note.\n...\n"
        )
        result = renderer.render_networks(raw, **specification())
        self.assertTrue(result.startswith(b"---\n"))
        self.assertTrue(result.endswith(b"# End note.\n...\n"))
        self.assertEqual(len(list(yaml.safe_load_all(result))), 1)

    def test_network_last_field_before_next_service_boundary(self):
        raw = RAW.replace(
            b"    networks:\n      - default # Grok network comment.\n",
            b"    networks:\n      default:\n        aliases: [llm-proxy2-grok-bridge]\n"
            b"    # Boundary comment belongs here.\n",
        )
        original = {name: {"default": None} for name in policy.MEMBERS}
        original[policy.BRIDGE]["default"] = {"aliases": [policy.BRIDGE]}
        result = renderer.render_networks(raw, **specification(original))
        self.assertIn(b"    # Boundary comment belongs here.\n  unrelated:\n", result)
        self.assertEqual(
            yaml.safe_load(result)["services"]["unrelated"],
            yaml.safe_load(raw)["services"]["unrelated"],
        )

    def test_last_service_literal_scalar_preserved_when_adding_field(self):
        raw = (
            b"services:\n  nginx:\n    image: nginx\n  llm-proxy2:\n    image: gateway\n"
            b"  llm-proxy2-grok-bridge:\n    image: candidate\n    command: |+\n"
            b"      echo unchanged\n      # Literal comment\n\n"
        )
        before = yaml.safe_load(raw)["services"][policy.BRIDGE]["command"]
        result = renderer.render_networks(raw, **specification())
        self.assertEqual(yaml.safe_load(result)["services"][policy.BRIDGE]["command"], before)

    def test_duplicate_keys_refuse_even_in_unrelated_sections(self):
        mutations = (
            RAW.replace(
                b"    image: nginx:synthetic", b"    image: first\n    image: nginx:synthetic"
            ),
            RAW.replace(b"  esnet:\n", b"  esnet: {}\n  esnet:\n"),
            RAW.replace(
                b"      TOKEN: synthetic-private-value",
                b"      TOKEN: other\n      TOKEN: synthetic-private-value",
            ),
            RAW + b"volumes:\n  duplicate: {}\n",
        )
        for raw in mutations:
            with self.subTest(raw=raw), self.assertRaises(renderer.ComposeNetworkError):
                renderer.render_networks(raw, **specification())

    def test_anchors_aliases_merges_multiple_docs_refuse(self):
        for raw in (
            RAW.replace(b"    image: nginx:synthetic", b"    image: &image nginx:synthetic"),
            RAW.replace(b"    image: gateway:synthetic", b"    image: *image"),
            RAW.replace(b"    image: nginx:synthetic", b"    <<: {image: nginx:synthetic}"),
            RAW + b"---\nservices: {}\n",
        ):
            with self.subTest(raw=raw), self.assertRaises(renderer.ComposeNetworkError):
                renderer.render_networks(raw, **specification())

    def test_flow_service_unexpected_indent_and_ambiguous_field_refuse(self):
        for raw in (
            RAW.replace(
                b"  nginx: # preserve service comment\n    image: nginx:synthetic\n"
                b"    ports:\n      - '443:443'\n",
                b"  nginx: {image: nginx, networks: [default]}\n",
            ),
            RAW.replace(
                b"  nginx: # preserve service comment", b"    nginx: # preserve service comment"
            ),
            RAW.replace(b"networks: [default]", b"networks: null"),
            RAW.replace(b"networks: [default]", b"networks: [default, default]"),
            RAW.replace(b"networks: [default]", b"network_mode: host"),
            RAW.replace(b"\n", b"\r\n"),
        ):
            with self.subTest(raw=raw), self.assertRaises(renderer.ComposeNetworkError):
                renderer.render_networks(raw, **specification())

    def test_network_collision_and_raw_review_drift_refuse(self):
        for raw in (
            RAW.replace(b"networks:\n  default:", b"networks:\n  grok_security: {}\n  default:"),
            RAW.replace(b"networks: [default]", b"networks: [postgres-network]"),
            RAW.replace(b"networks: [default]", b"networks: {default: {priority: 9}}"),
        ):
            with self.subTest(raw=raw), self.assertRaises(renderer.ComposeNetworkError):
                renderer.render_networks(raw, **specification())

    def test_arbitrary_desired_mapping_or_definition_cannot_expand_scope(self):
        changes = (
            lambda spec: spec["desired_networks"].update(other={"default": None}),
            lambda spec: spec["desired_networks"][policy.BRIDGE].update(default=None),
            lambda spec: spec["desired_networks"]["nginx"][policy.NETWORK].update(gw_priority=0),
            lambda spec: spec["desired_networks"]["llm-proxy2"][policy.NETWORK].update(
                aliases=["other"]
            ),
            lambda spec: spec["dedicated_definition"].update(internal=True),
            lambda spec: spec["dedicated_definition"].update(internal=0),
            lambda spec: spec["dedicated_definition"].update(driver="host"),
            lambda spec: spec["original_networks"].pop("nginx"),
        )
        for change in changes:
            spec = specification()
            change(spec)
            with self.subTest(change=change), self.assertRaises(renderer.ComposeNetworkError):
                renderer.render_networks(RAW, **spec)

    def test_secret_values_are_absent_from_parser_and_policy_errors(self):
        marker = "synthetic-private-refusal-marker"
        for raw in (
            RAW.replace(b"networks: [default]", ("networks: [" + marker).encode()),
            RAW.replace(b"networks: [default]", ("networks: [" + marker + "]").encode()),
        ):
            with self.assertRaises(renderer.ComposeNetworkError) as caught:
                renderer.render_networks(raw, **specification())
            self.assertNotIn(marker, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
