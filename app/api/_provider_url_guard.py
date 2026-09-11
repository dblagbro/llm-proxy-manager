"""SSRF guard for operator-supplied provider ``base_url`` (v5.22.18).

Lives in its own module rather than in ``app/api/providers.py`` because that
file carries an 800-LOC ceiling enforced by
``tests/unit/test_v4414_providers_stats_split.py`` — adding this inline pushed
it to 836 and tripped the guard, which is exactly what that guard is for.

Why the rule is narrow
----------------------
``base_url`` is admin-set and the proxy then makes server-side requests to it
(``app/providers/scanner.py``, routing dispatch). Nothing validated it.

The tempting fix — reject private/RFC1918 addresses — would break this
deployment. The cursor sidecar's default base_url is an internal address,
and ollama and the grok bridge sit on the docker network. Those are correct,
working configurations.

What has no legitimate use as an LLM endpoint is a cloud instance-metadata
service. Those hand IAM credentials to any unauthenticated local HTTP GET,
which makes them the actual prize in an SSRF: point a provider at one, let
the proxy fetch it, then read the result back out of an error string or a
model list. Blocking exactly those costs nothing and closes the credential
path, while leaving every legitimate private endpoint working.
"""

from urllib.parse import urlparse

# AWS/Azure/OpenStack/DigitalOcean share 169.254.169.254; the rest are
# provider-specific. Kept as a frozenset of exact hostnames — a substring
# match would be easy to fool and easy to over-block.
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS, Azure, OpenStack, DigitalOcean IMDS
        "fd00:ec2::254",  # AWS IMDSv6
        "metadata.google.internal",  # GCP
        "metadata.goog",
        "100.100.100.200",  # Alibaba Cloud
        "192.0.0.192",  # Oracle Cloud
    }
)


def validate_base_url(value: str | None) -> str | None:
    """Return ``value`` unchanged, or raise ``ValueError`` for a cloud
    instance-metadata endpoint.

    Raises ``ValueError`` specifically so a pydantic ``field_validator``
    renders it as a 422 rather than a 500.
    """
    if not value:
        return value
    # urlparse needs a scheme to populate .hostname; a bare "host:port"
    # would otherwise parse as scheme="host". Prefix "//" when absent.
    parsed = urlparse(value if "//" in value else f"//{value}")
    host = (parsed.hostname or "").lower()
    if host in _METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise ValueError(
            "base_url points at a cloud instance-metadata service, which "
            "serves IAM credentials to any local HTTP request. Refusing."
        )
    return value
