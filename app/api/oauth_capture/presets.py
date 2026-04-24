"""
Known-CLI presets for the OAuth capture wizard.

Each entry tells the UI what this vendor's CLI expects (env var names,
default upstream host(s), a human-readable setup hint). Adding a new
preset is a one-liner — no code changes elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CapturePreset:
    key: str                   # short id stored in profile.preset
    label: str                 # UI-friendly name
    cli_hint: str              # which CLI this captures (shown in wizard)
    primary_upstream: str      # default upstream host (no trailing slash)
    extra_upstreams: tuple[str, ...] = ()
    env_var_names: tuple[str, ...] = ()   # which env vars the CLI checks
    setup_hint: str = ""
    # v2.6.0: in-browser terminal support. If non-empty, the UI shows a
    # "Login to <vendor>" button that spawns this command in the sidecar
    # container. argv[0] must match CLI_WHITELIST in
    # sidecar/capture-runner.py. Leave blank to hide the button (user
    # falls back to manual env-var capture).
    login_cmd: str = ""


PRESETS: dict[str, CapturePreset] = {
    "claude-code": CapturePreset(
        key="claude-code",
        label="Anthropic — Claude Code CLI",
        cli_hint="`claude login` on the workstation",
        primary_upstream="https://console.anthropic.com",
        env_var_names=("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_URL", "ANTHROPIC_API_URL"),
        setup_hint="Run `claude login` then `claude \"ping\"` to capture both auth and first chat.",
        login_cmd="claude login",  # v2.6.0: in-browser terminal support
    ),
    "openai-codex": CapturePreset(
        key="openai-codex",
        label="OpenAI — Codex CLI / ChatGPT Plus",
        cli_hint="`codex auth` or the OpenAI CLI",
        primary_upstream="https://auth.openai.com",
        extra_upstreams=("https://api.openai.com",),
        env_var_names=("OPENAI_BASE_URL", "OPENAI_AUTH_URL"),
        setup_hint="ChatGPT Plus/Team tokens refresh ~every 1h; capture twice in quick succession.",
    ),
    "github-copilot": CapturePreset(
        key="github-copilot",
        label="GitHub Copilot",
        cli_hint="`gh copilot` or VS Code login",
        primary_upstream="https://github.com",
        extra_upstreams=("https://api.githubcopilot.com",),
        env_var_names=("GH_HOST",),
        setup_hint="Device-code flow — capture the /login/device dance end-to-end.",
    ),
    "azure-aad": CapturePreset(
        key="azure-aad",
        label="Microsoft / Azure AD (Azure OpenAI)",
        cli_hint="`az login` or `m365 login`",
        primary_upstream="https://login.microsoftonline.com",
        env_var_names=("AZURE_OPENAI_ENDPOINT",),
        setup_hint="MSAL device-code flow; tenant ID is part of the auth URL path.",
    ),
    "google-oauth": CapturePreset(
        key="google-oauth",
        label="Google — gcloud / Gemini CLI",
        cli_hint="`gcloud auth login` or `gemini auth`",
        primary_upstream="https://accounts.google.com",
        extra_upstreams=("https://oauth2.googleapis.com",),
        env_var_names=("CLOUDSDK_AUTH_AUTHORITY",),
        setup_hint="Browser PKCE flow with localhost redirect; we capture both the authorize + token exchange.",
    ),
    "xai-grok": CapturePreset(
        key="xai-grok",
        label="xAI — Grok (X Premium+)",
        cli_hint="any Grok CLI wrapper",
        primary_upstream="https://api.x.ai",
        setup_hint="TBD — no public CLI yet; capture whichever tool you use.",
    ),
    "cohere": CapturePreset(
        key="cohere",
        label="Cohere",
        cli_hint="`cohere login`",
        primary_upstream="https://dashboard.cohere.com",
        extra_upstreams=("https://api.cohere.com",),
    ),
    "custom": CapturePreset(
        key="custom",
        label="Custom / other",
        cli_hint="any CLI",
        primary_upstream="https://example.com",  # placeholder — user must edit
        setup_hint="Set the upstream URL yourself.",
    ),
}
