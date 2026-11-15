"""The historical Docker harness must refuse new network lifecycle operators."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import rehearse_release as harness


class LegacyHarnessGuardTests(unittest.TestCase):
    def source(self, folder, text):
        source = Path(folder) / "source"
        deployment = source / "grok_bridge/deployment"
        deployment.mkdir(parents=True)
        (deployment / "release.py").write_text(text, encoding="utf-8")
        return source

    def test_new_operator_refuses_before_host_or_output_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(folder, "NETWORK_RELEASE_SCHEMA = 1\n")
            destination = Path(folder) / "never-created"
            with (
                mock.patch.object(harness, "command") as command,
                self.assertRaisesRegex(RuntimeError, "network-aware"),
            ):
                harness.run(destination, source)
            command.assert_not_called()
            self.assertFalse(destination.exists())

    def test_unknown_network_schema_also_refuses(self):
        for value in ("0", "2", "True", "None"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                source = self.source(folder, "NETWORK_RELEASE_SCHEMA = " + value + "\n")
                with self.assertRaisesRegex(RuntimeError, "Legacy two-service"):
                    harness.require_supported_operator(source)

    def test_guard_does_not_execute_operator_module(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(
                folder, "raise RuntimeError('must not execute')\nNETWORK_RELEASE_SCHEMA = 1\n"
            )
            with self.assertRaisesRegex(RuntimeError, "network-aware"):
                harness.require_supported_operator(source)

    def test_historical_operator_is_only_accepted_by_static_guard(self):
        with tempfile.TemporaryDirectory() as folder:
            source = self.source(folder, "ORIGINAL = 'synthetic'\n")
            harness.require_supported_operator(source)


if __name__ == "__main__":
    unittest.main()
