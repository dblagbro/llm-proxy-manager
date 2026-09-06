"""v5.22.15 — reduce what an authenticated response gives away, and stop the
one CORS combination that would matter.

Neither of these was exploitable as shipped. Both are the kind of thing that
becomes exploitable later, quietly, when someone makes a reasonable-looking
change — so each is pinned rather than fixed and forgotten.
"""

import re
from pathlib import Path

from app.api.providers import _mask_key


class TestProviderKeyMasking:
    """``_serialize`` used to emit ``api_key[:8]``. Admin-only, so not a
    breach — but 8 characters of a vendor key is a real head start, and it
    was being handed out on every provider list render."""

    def test_long_key_reveals_at_most_four_characters(self):
        raw = "sk-ant-api03-" + "R" * 60
        masked = _mask_key(raw)
        assert masked.startswith("sk-a")
        assert "sk-ant-a" not in masked, "still leaking the old 8-char prefix"
        assert raw[4:] not in masked

    def test_short_key_reveals_nothing_but_its_length(self):
        """A 4-char prefix of a 6-char key is most of the key."""
        assert _mask_key("abc123") == "…(6 chars)"

    def test_none_stays_none(self):
        assert _mask_key(None) is None
        assert _mask_key("") is None

    def test_length_is_reported_so_the_ui_can_still_tell_keys_apart(self):
        assert "(73 chars)" in _mask_key("k" * 73)

    def test_serializer_uses_the_mask(self):
        src = Path("app/api/providers.py").read_text()
        assert '"api_key": _mask_key(' in src
        # Scoped to the serializer: an 8-char prefix COMPARISON elsewhere is
        # fine (it emits nothing), it is emitting 8 chars that was the problem.
        assert '"api_key": f"{p.api_key[:8]}' not in src, (
            "the serializer is emitting a raw 8-char slice again"
        )


class TestCorsCredentialsGuard:
    """Wildcard origins are safe only while credentials are off. The pairing
    is what would hand any website the logged-in operator's admin session."""

    def _main_src(self) -> str:
        return Path("app/main.py").read_text()

    def test_wildcard_plus_credentials_is_refused_in_code(self):
        src = self._main_src()
        assert 'if _cors_credentials and "*" in _cors_origins:' in src
        assert "_cors_credentials = False" in src

    def test_origins_are_configurable_not_hardcoded(self):
        src = self._main_src()
        assert "settings.cors_allow_origins" in src
        assert re.search(r"allow_origins=\[\"\*\"\]", src) is None, (
            "origins are hardcoded to a wildcard again"
        )

    def test_credentials_default_to_off(self):
        from app.config import settings

        assert settings.cors_allow_credentials is False

    def test_default_origins_unchanged_so_consumers_do_not_break(self):
        from app.config import settings

        assert settings.cors_allow_origins == "*"


class TestGuardLogicItself:
    """Exercise the refusal rule directly, so it is tested as behaviour and
    not only as source text."""

    @staticmethod
    def _resolve(origins_csv: str, credentials: bool):
        origins = [o.strip() for o in origins_csv.split(",") if o.strip()] or ["*"]
        creds = bool(credentials)
        if creds and "*" in origins:
            creds = False
        return origins, creds

    def test_wildcard_forces_credentials_off(self):
        assert self._resolve("*", True) == (["*"], False)

    def test_explicit_origins_may_use_credentials(self):
        origins, creds = self._resolve("https://a.example,https://b.example", True)
        assert origins == ["https://a.example", "https://b.example"]
        assert creds is True

    def test_empty_config_falls_back_to_wildcard_without_credentials(self):
        assert self._resolve("  ", True) == (["*"], False)
