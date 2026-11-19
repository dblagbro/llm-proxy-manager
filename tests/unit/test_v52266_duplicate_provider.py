"""v5.22.66: frontend-only Duplicate action on provider cards."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "frontend" / "src"
FORM = (ROOT / "components/providers/ProviderForm.tsx").read_text()
PAGE = (ROOT / "pages/ProvidersPage.tsx").read_text()


def test_duplicate_helper_strips_name_and_secrets():
    assert "export function providerToDuplicateForm" in FORM
    body = FORM.split("export function providerToDuplicateForm")[1].split("interface Props")[0]
    assert "`${p.name} (copy)`" in body
    assert "api_key: ''" in body
    assert "SECRET_EXTRA_KEY" in body
    # reuses providerToForm rather than duplicating field mapping
    assert "providerToForm(p)" in body


def test_secret_pattern_covers_known_extra_config_secrets():
    import re

    pat = re.search(r"SECRET_EXTRA_KEY = /(.+)/i", FORM).group(1)
    rx = re.compile(pat, re.I)
    for k in (
        "cookie_header",
        "bridge_token",
        "x_statsig_id",
        "x_userid",
        "conversation_id",
        "api_key",
        "refresh_token",
    ):
        assert rx.search(k), k
    for k in ("bridge_url", "disable_long_context", "user_agent"):
        assert not rx.search(k), k


def test_card_has_duplicate_button_opening_create_form():
    assert "openDuplicate(p)" in PAGE
    assert "Duplicate" in PAGE
    fn = PAGE.split("function openDuplicate")[1].split("function closeModal")[0]
    assert "setEditing(null)" in fn and "providerToDuplicateForm(p)" in fn


def test_oauth_note_shown():
    assert "needs its own sign-in" in PAGE
    for t in ("claude-oauth", "ChatGPT-oauth-plan", "cursor-oauth", "grok-web", "cohere"):
        assert f"'{t}'" in FORM.split("OAUTH_PROVIDER_TYPES: ProviderType[] = [")[1].split("]")[0]
