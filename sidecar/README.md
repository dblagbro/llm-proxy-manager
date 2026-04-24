# llm-proxy2-capture — OAuth capture sidecar

Lightweight sidecar container that runs vendor CLI tools (claude, codex,
gh, etc.) inside a PTY and pipes them to a browser terminal via
WebSocket. The main `llm-proxy2` container is the trust boundary — the
sidecar has no auth of its own and is only reachable over the internal
docker network.

## Build

```bash
# from the repo root
docker build -t llm-proxy2-capture:latest sidecar/
```

## Run (standalone, for debugging)

```bash
docker run --rm -it --network llm-proxy-net \
  --name llm-proxy2-capture-test -p 4000:4000 \
  llm-proxy2-capture:latest

curl localhost:4000/health
```

## Adding a CLI

1. Add the install step to `Dockerfile` (keep it pinned).
2. Add the binary name to `CLI_WHITELIST` in `capture-runner.py`.
3. Add the preset in `app/api/oauth_capture/presets.py` with `login_cmd`
   set to the exact command (e.g. `"codex auth login"`).
4. Rebuild the image + push to Docker Hub.

No changes required in the UI — a new preset automatically lights up
the Login button in the OAuth Capture wizard.

## Security

- `CLI_WHITELIST` gates every `/spawn` request. No shell is invoked; the
  first argv element is the binary we run, the rest are its args.
- Only one concurrent session. `?takeover=1` explicitly opts into killing
  the existing one (done by the main proxy when an admin clicks "take
  over" in the UI).
- PTY is killed after 15 min of no WS traffic.
- `BROWSER=echo` means any CLI that tries `xdg-open <url>` prints the
  URL instead of trying to spawn a browser in a container with no X11.
