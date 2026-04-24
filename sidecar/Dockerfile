# llm-proxy2-capture — sidecar that runs vendor CLIs for OAuth capture.
#
# The main llm-proxy2 container talks to this sidecar over the internal
# docker network. The sidecar spawns a PTY running a whitelisted CLI,
# pipes its stdin/stdout to a WebSocket that the main container relays
# to the admin's browser. All HTTPS the CLI makes is routed back through
# the main proxy's /api/oauth-capture/{profile}/... passthrough because
# we set the vendor's own `*_BASE_URL` env vars in the PTY's environment.
#
# Adding another CLI in a future release = add an install line here +
# add an entry to app/api/oauth_capture/presets.py (login_cmd field).
# No UI or routing changes required.

FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git \
    # Node (for npm-installed CLIs like claude-code)
    nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# v2.6.0 ships claude-code only. Future versions add codex, gh, etc.
RUN npm install -g @anthropic-ai/claude-code@latest

# BROWSER=echo: any CLI that tries `xdg-open <url>` will print the URL to
# stdout instead of crashing. xterm.js on the frontend turns URLs into
# clickable links — admin clicks it in their real browser to approve OAuth.
ENV BROWSER=echo \
    TERM=xterm-256color \
    LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir \
    aiohttp==3.9.5 \
    ptyprocess==0.7.0

WORKDIR /app
COPY capture-runner.py /app/capture-runner.py

EXPOSE 4000
CMD ["python", "/app/capture-runner.py"]
