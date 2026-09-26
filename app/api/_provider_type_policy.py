"""Provider-type retirement policy (v5.22.24).

Why this exists
---------------
The 2026-09-26 market review (`docs/market-review.md`) recommended NARROW:
stop building the consumer-subscription OAuth capability. The driving evidence
is Anthropic's own Claude Code legal and compliance page, which states that
subscription OAuth is "intended exclusively" for Claude Code and native
Anthropic applications, that Anthropic "does not permit third-party developers
... to route requests through Free, Pro, or Max plan credentials on behalf of
their users", and that developers "may not collect, store, or intermediate
Claude.ai credentials or session tokens". Reporting indicates enforcement at
the API layer began in January 2026.

Our own telemetry agreed before the review did: `Devin-Codex-Gmail` returned
1 success in 4,457 requests over 24 days, and `Grok-Web-Devin` was 0/27 in a
12-hour window.

This module is a **gate, not a demolition**. Retiring a provider type is
staged deliberately:

  RETIRED   — no live provider uses it; creating a new one is refused. The
              dispatch code may still exist but is unreachable in practice.
  DEPRECATED— a live provider still depends on it and is carrying real
              traffic, so refusing it outright would cost availability.
              Creating a new one is refused; the existing row keeps working
              until a sanctioned replacement is verified.

`claude-oauth` is DEPRECATED rather than RETIRED on purpose: at the time of
writing, `Devin-Anthropic-Max-VG` was the healthiest provider in the fleet
(16 successes, 0 failures over 12h) while two of the three metered-key
providers were unverified. Pulling it before a metered replacement is proven
would trade a compliance risk for an outage. See the Phase 2 gate in
`docs/market-review.md`.
"""

from pydantic import field_validator

# No live provider uses these, and none should be created again.
RETIRED_PROVIDER_TYPES = frozenset({"ChatGPT-oauth-plan", "grok-web"})

# Still load-bearing. No NEW ones, but existing rows keep serving until a
# metered replacement is verified healthy.
DEPRECATED_PROVIDER_TYPES = frozenset({"claude-oauth"})

_REASON = (
    "Consumer-subscription OAuth is not a permitted integration path: the "
    "vendor terms reserve subscription credentials for their own first-party "
    "clients and prohibit third parties from storing or intermediating them. "
    "Use an API key from the provider's console instead. "
    "See docs/market-review.md (2026-09-26)."
)


def validate_provider_type(value: str | None) -> str | None:
    """Reject creation of a retired or deprecated provider type.

    Raises ``ValueError`` so a pydantic field validator renders a 422 rather
    than a 500. Existing rows are untouched — this guards the write path only,
    which is what keeps a deprecated type serving while still being closed to
    new configuration.
    """
    if not value:
        return value
    if value in RETIRED_PROVIDER_TYPES:
        raise ValueError(f"provider_type {value!r} is retired. {_REASON}")
    if value in DEPRECATED_PROVIDER_TYPES:
        raise ValueError(
            f"provider_type {value!r} is deprecated and closed to new "
            f"providers. {_REASON}"
        )
    return value


class ProviderFieldGuards:
    """Pydantic validators for operator-supplied provider fields.

    A mixin rather than inline validators because ``app/api/providers.py``
    carries an 800-LOC ceiling enforced by
    ``tests/unit/test_v4414_providers_stats_split.py`` — adding them inline
    pushed it to 803 and tripped that guard, which is what it is for.
    ``ProviderUpdate`` subclasses ``ProviderCreate``, so both paths inherit
    these from one definition.
    """

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, v):
        from app.api._provider_url_guard import validate_base_url

        return validate_base_url(v)

    @field_validator("provider_type")
    @classmethod
    def _check_provider_type(cls, v):
        return validate_provider_type(v)
