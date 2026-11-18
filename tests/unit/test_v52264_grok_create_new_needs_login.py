"""v5.22.64: Grok "Create new" must not blame a dead browser when signed out.

Signed out, the bridge's create-new drives grok.com until nginx returns 504, and the
UI told the operator to restart the bridge. The handler now short-circuits when the
bridge status says logged_in=false and maps a 504 to a sign-in hint.
"""

from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "frontend/src/components/providers/GrokWebProviderFields.tsx"
).read_text()


def test_create_new_checks_login_before_calling_bridge():
    guard = SRC.index("!status.logged_in")
    call = SRC.index("/api/conversation/new")
    assert guard < call


def test_504_maps_to_sign_in_hint():
    assert "r.status === 504" in SRC
    assert "not signed in" in SRC
