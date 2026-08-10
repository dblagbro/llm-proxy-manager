# ⛔ llm-proxy v1 — RETIRED / End of Life

**Status:** Retired. **End of life:** 2026-07-15.

This `master` branch is an older snapshot of **llm-proxy v1** (the original **Node.js** LLM API
proxy, "LLM Proxy Manager"). It is no longer maintained, deployed, or supported. The `main`
branch holds the last v1 state; both are retired.

## Use llm-proxy2 instead (the current version)
The successor is **llm-proxy2**, a **Python / FastAPI** rewrite, on the **`v2` branch** of this
same repository (`dblagbro/llm-proxy-manager`).

- Current version lives on branch **`v2`** — start with its `README.md` and `AGENTS.md`.
- Runs in production served at **`/llm-proxy2/`**; distributed as Docker image
  **`dblagbro/llm-proxy-manager`** (5.x tags).
- v2 is a full rewrite — it does not share code or deployment with v1.

## What this means
- ❌ Do **not** deploy, build, or run v1. The v1 container was removed from production on 2026-07-15.
- ❌ Do **not** open new features/fixes against `master` or `main`.
- ✅ These branches remain only for historical reference / audit.

_Retired 2026-07-15. Superseded by llm-proxy2 (`v2` branch)._
