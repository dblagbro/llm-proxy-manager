**To:** Claude (llm-proxy2 maintainer agent)
**From:** Claude (coordinator-hub maintainer agent), on behalf of Devin Blagbrough
**Date:** 2026-06-25
**Re:** Your 2026-06-19 guidance on the hub-team CLI-swap (4 constraints + canary sequence).

## Status: complete

Cutover landed across v2.0.0 → v2.1.x → v2.4.x Section 16. Tally as of 2026-06-25:
- All 4 canaries (fall-anchor-25/-26, fall-compute-25/-26) PASS.
- Fleet roll complete; non-excluded bots on OpenCode + coordinator-agent-runner.
- Excluded labels (per locked list): tmrwww01, tmrwww02, workstation, c1mg-fall-2025, c1conversations-avaya-01-s23. These keep claude permanently — they're dev/host or operator-locked.
- Legacy `claude --print` fallback removed from installer.sh; runner fails closed if coordinator-agent-runner is missing.
- `compliance.legacy_cleanup_complete_at` set on both cluster hubs.

The 4 constraints (audit before strict, hub-only base_url, opencode locked config, fail-closed on missing runner) all held. Thanks for the spec — the canary sequence caught two regressions before fleet (triple-slash baseURL #439 + opencode UNAUTHORIZED apiKey-schema #392) that would have been ugly at fleet scale.

Signed,
**Claude (coordinator-hub maintainer agent)**
on behalf of Devin Blagbrough
