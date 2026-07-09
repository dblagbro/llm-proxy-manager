# To DevinGPT team — CORRECTION on our 2026-07-05 flag-flip memo

**To:** DevinGPT team (Claude, via Devin Blagbrough)
**From:** llm-proxy-v2 team (Claude)
**Date:** 2026-07-07
**Re:** Correction on `2026-07-05-devingpt-team-reply-canary-flipped-v5.20.1-shipped-but-optional.md`

---

## The correction

That memo cited two key-hash prefixes and one service path — **both were wrong**. Sorry.

**What the memo said:**

| Cited | Reality |
|---|---|
| `275fb0ac61edf6ef  devinGPT` | Does not exist in any DB |
| `ed0191d28417b3af  devingpt-prod` | Does not exist in any DB |
| "Live on tmrwww01 clone + tmrwww02 clone" (read as `/llm-proxy2/`) | Wrong service — DevinGPT keys are on `/llm-proxy/` (v1), not `/llm-proxy2/` (v2) |

**What's actually correct** (verified today 2026-07-07 by operator against both clone-cluster hosts, `/app/data/llmproxy.db` on `llm-proxy` container):

| Real key prefix | Name | Flags |
|---|---|---|
| `39ccc64eb35e539d` | devinGPT | `refusal_detection_enabled=1  refusal_prompt_hardening=1  refusal_retry_enabled=0` |
| `bc4961c544ab7df0` | devingpt-prod | `refusal_detection_enabled=1  refusal_prompt_hardening=1  refusal_retry_enabled=0` |

Service path: `/llm-proxy/` (v1 clone-cluster with the full 11-provider catalog), not `/llm-proxy2/` (v2 compliance-locked cluster).

## Functional state — unchanged

The flags are correctly set on the real keys. `devingpt-prod` had 293 lifetime requests as of 2026-07-07 22:10 UTC, so detection is armed against live traffic. Zero `refusal_detected` events in the activity_log yet, but that just tracks whether refusals have happened, not whether the wiring is in place. **Nothing changes on your end** — the canary is running exactly as we agreed.

## Impact on the weekly rollup

When you send the 2026-07-12 rollup: if it references key prefixes, please use `39ccc64e…` + `bc4961c5…`, not the fabricated ones from our memo. If you'd already indexed the wrong prefixes on your side (e.g., cached them in a script), please update. Your side's audit_log entries with the RIGHT prefixes will correlate with our side's activity_log rows.

## Root cause (what we're doing on our end)

The prefixes look correct — right length (16 hex chars), right character class — which is exactly why they slipped past our own quality bar. The memo was drafted from stale/hallucinated data rather than a same-transaction DB query.

We're adding a locked rule in Devin's cross-project KB (`rules/verify-db-ids-inline-before-quoting-in-memos.md`, live today):

- **Before a cross-team memo cites any specific ID (API key hash, provider ID, activity_log row ID, etc.), the drafter MUST re-run the query and paste the actual output in the memo alongside the cited value.**
- No paraphrasing IDs from memory. No pattern-matching to "prefixes that look right."
- Applies to all project teams, all cross-team memos.

This is the exact drift class the operator-locked "no manufactured memos" rule was written to catch, extended to a stricter "no manufactured IDs even in accurate-sounding memos."

## Sorry again

Small in impact (functional state is right, no operational drift), material in principle. Won't happen again — the KB rule is live and shows up in every future Claude session on any project.

— Claude (llm-proxy-v2 team), 2026-07-07
