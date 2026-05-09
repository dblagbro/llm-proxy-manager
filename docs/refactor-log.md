# Refactor log

A running ledger of architectural changes. New entries go on top.
Pair with [`architecture.md`](architecture.md) for the static module map.

Each entry follows this shape:

```
## YYYY-MM-DD — <short title> (R<N>, version <ver>)
**What**: 1-2 sentences on the change.
**Why**: motivation + the symptom it fixes.
**Files**: list of touched files.
**Outcome**: line-count delta + test-pass status.
**Next**: optional pointer to the next-target if any.
```

---

## 2026-05-09 (PM, second pass) — Extract grok_web manual-mode HTTP setup (R3, v3.5.0+)

**What**: The 3 dispatch functions in `app/providers/grok_web.py`
(`complete_grok_web`, `stream_grok_web`, `stream_grok_web_anthropic`)
each opened with the same 6-line setup pattern:

```python
conv_id = _pick_conversation_id(provider_extra_config)
mode_id = _model_to_mode_id(model)
url = f"{GROK_BASE_URL}/rest/app-chat/conversations/{conv_id}/responses"
headers = _build_headers(provider_extra_config, conv_id)
body = _build_body(prompt, mode_id)
```

Extracted to a single helper `_build_manual_request(extra_config, prompt, model)`
returning `(conv_id, mode_id, url, headers, body)`. Each dispatch
function now opens with the helper call + format-specific httpx
invocation; the URL pattern, header convention, and body shape
live in one place.

**Why**: Three places to update if any of the conventions ever
change (e.g. grok.com adds a header, the URL pattern changes,
the modeId mapping picks up a new tier). The 6-line setup wasn't
"big" duplication but it was conceptually load-bearing — a future
fix to the URL or header construction would have to land 3 times.
The audit also identified `app/api/providers.py` (939L, 21
functions) as the largest file but explicitly DEFERRED that split
under the operator's "avoid over-fragmentation" rule — 21 functions
in one logically-named file is intuitive; splitting into per-CRUD
files would worsen lookup speed.

`_messages_streaming.py` (701L) was also surveyed — it's bigger
than `_completions_streaming.py` because it carries the
claude-oauth-specific helpers (`_complete_claude_oauth`,
`_stream_claude_oauth`, `_inject_claude_code_system`,
`_refresh_oauth_token`), not because of duplication. Correct
asymmetry, no extraction.

**Files**:
- `app/providers/grok_web.py` — added `_build_manual_request`
  helper (~50 lines including the docstring); collapsed 3 inline
  6-line setup blocks to 3-line helper calls

**Outcome**:
- `grok_web.py`: 812 → 845 lines (+33; helper docstring is verbose
  on purpose — explains the 3-call-site rationale)
- **Maintenance surface**: 3 places to update URL/header/body
  setup → 1 place
- Tests: **1035 passing** (no regression)

**Next**: Tool emulation (~35 lines, 60% duplicated) and
streaming/hedging orchestration (~59 lines, 70% duplicated)
remain on the deferred list from R1/R2 — still recommended ONLY
when a concrete bug forces editing both messages.py and
completions.py. R4 candidates from this pass:

- `_messages_streaming.py` line 376–600 region: the
  claude-oauth dispatch+stream pair has internal duplication of
  request-body construction. A helper similar to
  `_build_manual_request` could absorb header + body shaping.
  ~30 lines of duplication, lower payoff than R3.
- `app/api/providers.py` per-CRUD endpoint splits — DEFERRED per
  intuitiveness rule. Reconsider if file crosses 1200L or if a
  single function bloats past ~150 lines.
- `app/api/lmrh_v2.py` (647L) — split into endpoints + render
  modules pre-emptively. Currently below the 800L threshold;
  defer until it crosses.

---

## 2026-05-09 — Extract cache + CoT orchestration to `_request_pipeline` (R1+R2, v3.5.0+)

**What**: The cache-decision-and-serve block (35 lines, 100% duplicated)
and the CoT-E engagement block (42 lines, 80% duplicated) lived in both
`app/api/messages.py` and `app/api/completions.py`. Extracted to two
helpers in `app/api/_request_pipeline.py`:

- `maybe_serve_from_cache(...) → (CacheDecision, Optional[Response])`
- `maybe_engage_cot(...) → Optional[StreamingResponse]`

Both helpers take the wire-format-specific bits (SSE / JSON builders,
stream functions) as callable parameters, so the Anthropic and OpenAI
shapes pass their own builders without duplicating the orchestration.

**Why**: Pre-R1 a bug fix in cache decision logic had to land in two
places, and the same was true for CoT critique-provider pickup. The
2026-05-09 audit (Explore agent estimate) found ~250 lines of true
duplication between the two endpoints, of which cache + CoT were the
top 2 highest-value extractable patterns. Tool emulation + hedging
were also identified but deferred to avoid over-fragmentation in a
single pass.

A near-bug surfaced during R1: the first cut of `maybe_serve_from_cache`
returned only the response (or None), losing the `cache_decision`
local that downstream `maybe_store()` calls relied on. The
`try: maybe_store(...) except Exception: pass` blocks silently swallowed
the resulting NameError so tests passed but cache write-back was quietly
skipped. Caught during line-count review; helper now returns the
decision tuple so callers can pass it onward.

**Files**:
- `app/api/_request_pipeline.py` — added two helpers (~190 lines including docstrings)
- `app/api/messages.py` — replaced cache + CoT inline blocks with helper calls; cleaned 3 now-unused imports
- `app/api/completions.py` — same; cleaned 3 now-unused imports

**Outcome**:
- `messages.py`: 813 → 783 lines (−30)
- `completions.py`: 630 → 601 lines (−29)
- `_request_pipeline.py`: 312 → 501 lines (+189)
- Net file LOC: +130 (helper docstrings explain why, which is the point)
- **Maintenance surface**: TWO places to update cache or CoT logic → ONE
- Tests: **1035 passing** (no regression; same count as pre-refactor)

**Next**: Tool emulation (~35 lines, 60% duplicated) and streaming/hedging
orchestration (~59 lines, 70% duplicated) are the next highest-value
extraction targets per the 2026-05-09 audit. Recommended ONLY if a
concrete bug shows up that requires editing both endpoints — otherwise
the current state is the right balance between sharing and clarity.
The `_request_pipeline.py` module should not exceed ~700 lines or it
itself becomes the over-fragmentation problem; if more orchestration
needs sharing, consider splitting into
`_request_pipeline/cache.py` + `_request_pipeline/cot.py` etc.

---

(no earlier entries — this is the first formal refactor pass after the
2026-05-09 v3.5.0 model-identity work)
