# llm-proxy v2 — Claude Code Guide

## What this is
Python/FastAPI rewrite of llm-proxy v1. Served at `/llm-proxy2/` on 3 nodes via the main
nginx + docker-compose stack at `/home/dblagbro/docker/`.

## Deployment nodes
- tmrwww01 (this server)
- tmrwww02
- c1conversations-avaya-01-s23

## Critical paths
- App source: `/home/dblagbro/llm-proxy-v2/`
- Docker compose: `/home/dblagbro/docker/docker-compose.yml` (container: `llm-proxy2`)
- nginx location: `/home/dblagbro/docker/config/nginx/projects-locations.d/llm-proxy2.conf`
- Frontend build output: `frontend/dist/` (built inside Docker image)

## Sub-path deployment
The app runs at `/llm-proxy2/`, NOT at root. Three things must stay in sync:
1. `frontend/vite.config.ts` — `base: '/llm-proxy2/'`
2. `frontend/src/App.tsx` — `<BrowserRouter basename="/llm-proxy2">`
3. `frontend/src/api/client.ts` — `const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')`

Breaking any of these causes API calls to go to the wrong nginx location or React Router
to resolve paths incorrectly.

## Docker rules (from global CLAUDE.md)
- NEVER run `docker compose down` or touch other containers
- To rebuild: `sudo docker compose build llm-proxy2 && sudo docker compose up -d --force-recreate --no-deps llm-proxy2`
- To reload nginx: `sudo docker exec nginx nginx -s reload`
- Run compose commands from `/home/dblagbro/docker/`, NOT from this directory

## Python 3.13 notes
- Do NOT use `passlib` — it crashes on Python 3.13. Use `bcrypt` package directly (see `app/auth/admin.py`)
- Do NOT use `await` inside generator expressions — use explicit async for loops

## FastAPI SPA routing
- `/assets` is mounted as StaticFiles for JS/CSS
- All other paths use `/{full_path:path}` catch-all returning `FileResponse(index.html)`
- `StaticFiles(html=True)` at root does NOT work for SPA routing — don't use it

## Testing
- Integration tests: `python -m pytest tests/integration/test_playwright_ui.py -v`
- Tests run against live deployment at https://www.voipguru.org/llm-proxy2/
- Install playwright first: `playwright install chromium`
- Each test gets its own browser context (no shared cookie state between tests)

## Default credentials
- admin / admin (created on first boot if no users exist)
