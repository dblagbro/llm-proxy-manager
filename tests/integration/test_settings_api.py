"""Settings API tests — read, write, validation."""
import pytest

from tests.conftest import BASE_URL

EXPECTED_KEYS = {
    "cot_enabled", "cot_max_iterations", "cot_quality_threshold",
    "cot_min_tokens_skip", "cot_critique_max_tokens",
    "cot_verify_enabled", "cot_verify_auto_detect", "cot_verify_max_tokens",
    "circuit_breaker_threshold", "circuit_breaker_timeout_sec",
    "circuit_breaker_success_needed", "circuit_breaker_halfopen_sec",
    "hold_down_sec", "native_thinking_budget_tokens", "native_reasoning_effort",
    "smtp_enabled",
}


class TestSettingsRead:
    def test_get_returns_all_expected_keys(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/settings")
        assert r.status_code == 200
        data = r.json()
        for key in EXPECTED_KEYS:
            assert key in data, f"Missing setting key: {key}"

    def test_cot_enabled_is_bool(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/settings")
        assert isinstance(r.json()["cot_enabled"], bool)

    def test_numeric_settings_are_numbers(self, admin_session):
        data = admin_session.get(f"{BASE_URL}/api/settings").json()
        for key in ("cot_max_iterations", "circuit_breaker_threshold", "hold_down_sec",
                    "native_thinking_budget_tokens", "smtp_port"):
            assert isinstance(data[key], (int, float)), f"{key} is not numeric"


class TestSettingsWrite:
    def test_partial_update_applies(self, admin_session, settings_snapshot):
        """Change one setting, verify it, restore."""
        original = settings_snapshot["cot_max_iterations"]
        new_val = 2 if original != 2 else 3
        r = admin_session.put(f"{BASE_URL}/api/settings", json={"cot_max_iterations": new_val})
        assert r.status_code == 200
        assert "cot_max_iterations" in r.json().get("saved", [])
        # Read back
        r2 = admin_session.get(f"{BASE_URL}/api/settings")
        assert r2.json()["cot_max_iterations"] == new_val
        # Restore
        admin_session.put(f"{BASE_URL}/api/settings", json={"cot_max_iterations": original})

    def test_unknown_key_rejected(self, admin_session):
        r = admin_session.put(f"{BASE_URL}/api/settings", json={"nonexistent_setting_xyz": 1})
        assert r.status_code == 400

    def test_cluster_diff_without_cluster(self, admin_session):
        r = admin_session.get(f"{BASE_URL}/api/settings/cluster-diff")
        assert r.status_code == 200
        data = r.json()
        # Cluster is not enabled on this node — should return disabled status
        assert data.get("cluster_enabled") is False or "peers" in data
