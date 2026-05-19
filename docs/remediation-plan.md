# Remediation plan — v4.3.0 QA pass (2026-05-18)

Companion to `docs/bug-log.md` (findings) and `docs/4.3-qa-report.md` (full
results). Covers the 5 findings from the v4.3.0 QA pass.

**Status (updated 2026-05-18): GROUPS 1 + 2 IMPLEMENTED** on the `v2` branch
and verified — pending the v4.3.1 release. **Group 3** (operational, BUG-023)
not started — it is an ops action, no code. The original plan and
`docs/backup-plan.md` were reviewed before the fix phase began.

---

## Release-blocker assessment

**None.** v4.3.0 has no critical / high / medium defects. The release is
sound and is correctly live on all 3 nodes. Every finding below is
low-severity, a coverage gap, an enhancement, or an operational note —
none requires a hold or a hotfix.

## Findings grouped by severity & subsystem

| # | Finding | Severity | Subsystem | Fix type |
|---|---------|----------|-----------|----------|
| BUG-024 | voice-button pulse ignores `prefers-reduced-motion` | enhancement | frontend (AIRI voice) | local, trivial |
| BUG-020 | pre-login `/api/auth/me` 401 console noise | low | frontend (auth bootstrap) | local, small |
| BUG-021 | no automated test for message→speak wiring | low | tests | local, additive |
| BUG-022 | audible TTS playback unverifiable headless | low | tests / release process | process + optional local |
| BUG-023 | c1conv at 9/10 healthy providers | low | operational (fleet) | ops action, no code |

## Fix groups

### Group 1 — Quick wins (frontend, trivial; ship together)

- **BUG-024** — add `motion-reduce:animate-none` alongside `animate-pulse`
  in `AiriSpeaker.tsx`, `AiriMicButton.tsx`, `AiriHandsFree.tsx`.
- **BUG-020** — make the boot auth probe not log a console error on the
  expected 401 (e.g. treat 401 as a normal "logged-out" result without a
  failed-resource log; the functional handling is already correct).
- **Effort:** ~30 min. **Risk:** very low — cosmetic, no behavior change.
- **Retest:** rebuild the frontend; Playwright check — clean console on a
  fresh load; voice buttons still animate (and stop animating under a
  `prefers-reduced-motion` emulated context).

### Group 2 — Test coverage (additive; no product code)

- **BUG-021** — add a Playwright integration test: speaker on → AIRI chat
  turn → assert `POST /api/airi/speak` fires on message completion.
- **BUG-022** — add a real-browser manual verification step to the release
  checklist ("enable the speaker, send a chat turn, confirm Airy is
  audible"); optionally prime the `<audio>` element inside the speaker-toggle
  click handler to harden against autoplay-policy rejection (this *is* a
  small product change — keep it in this group only if the manual check ever
  shows an autoplay failure).
- **Effort:** ~1–2 h. **Risk:** none (tests only), unless the optional
  audio-priming change is taken — then low.
- **Retest:** run the new test; full `test_airi_voice.py`.

### Group 3 — Operational (no code)

- **BUG-023** — on c1conv, identify the unhealthy provider
  (`/health.circuitBreakers` + the activity log) and reset/refresh its
  credential. This is fleet hygiene, unrelated to v4.3.0; can be done
  independently and immediately.
- **Effort:** ~15 min. **Risk:** low. **Retest:** `c1conv /health` → 10/10.

## Local vs architectural

All findings are **local** fixes — no architectural change is required. No
fix depends on another. Groups 1, 2, and 3 are fully independent and can be
done in any order or in parallel.

## Recommended packaging

- Groups 1 + 2 → a single **v4.3.1** patch release (frontend-only product
  change + tests). Goes through the normal `tools/cut-release.sh` ceremony.
- Group 3 → an immediate operational action, no release needed.

## Risky changes requiring extra caution

None. The only change that touches product behavior is the optional
audio-priming in BUG-022 — and that should only be taken if a real-browser
check actually shows an autoplay failure; otherwise it is a tests-only
change set.

## Retest scope after the fix release

If a v4.3.1 ships (Groups 1+2): re-run `tests/unit/` (must stay 2130+ green),
the new TTS integration test, and a Playwright sanity check of the AIRI panel
in both themes (clean console, voice buttons render + animate correctly).
A full QA pass is **not** required for a frontend-only cosmetic patch.

---

## 2026-05-19 — Post-v4.3.2 verification pass findings

**Status: PROPOSED — awaiting operator review. No fixes implemented.**

Two new bugs found verifying the v4.3.2 release. Both stem from the same
architectural misread of grok-web (one shared public-URL bridge, not
per-node sidecars).

### Group A — release blocker / regression (BUG-025 + BUG-026)

| Bug | Severity | Subsystem | Fix type |
|---|---|---|---|
| **BUG-025** | high | grok-bridge container on tmrwww01 | operational (restart) + hardening (compose healthcheck) |
| **BUG-026** | medium | v4.3.2 keepalive patch (dead code) | code: revert or correct premise |

### Recommended fix order

1. **BUG-025 (immediate, ops, low-risk)** — `docker restart
   llm-proxy2-grok-bridge` on tmrwww01. Single named container, no stack
   impact. If the bridge persists its Grok cookies the session recovers
   automatically; otherwise the v4.4 per-node-auth flow becomes urgent.
   *Retest:* probe `http://llm-proxy2-grok-bridge:8000/status` from inside
   `llm-proxy2`, send one grok-web probe, confirm CB closes. ETA: 1 min.
2. **BUG-025 hardening (follow-up release, e.g. v4.3.3)** — add a compose
   healthcheck on the grok-bridge so an internal crash (Playwright page,
   FastAPI process) restarts the container automatically instead of the
   bridge sitting "Up 10 days" with a dead service inside. *Retest:* kill
   the inner process manually, watch the container restart. Bundle with
   BUG-026 fix below.
3. **BUG-026 (next release — v4.3.3 or rolled into the v4.4 arc)** —
   choose:
   - **Revert** the keepalive.py addition (recommended — once BUG-025 is
     fixed, the noise it was trying to suppress is gone at source). The
     `_local_sidecar_reachable` helper can be kept if useful, but the
     gate that uses it should be removed since the premise doesn't apply
     to grok-web's shared-bridge architecture.
   - **Or** correct the gate — e.g. only skip on an actual `ConnectError`
     from `complete_grok_web`, not a speculative pre-check.
   *Retest:* unit test that drives the skip path with a stubbed unreachable
   bridge; live verification that c1conv/tmrwww02 still probe normally.
4. **BUG-023 status (corrected)** — the original "no local sidecar"
   diagnosis was wrong; the real cause is BUG-025. Closing-via-BUG-025 is
   appropriate once the bridge is restored.

### Risky changes

None of these are risky individually. The combination "revert v4.3.2 +
ship v4.3.3" is two releases in quick succession — acceptable for
defect-driven patches, but a reminder that v4.3.2 itself should have had
the live verification this pass just did *before* cutting the release.
That's a process gap (see qa-notes).

### Backup / rollback (delta over `backup-plan.md`)

- BUG-025: a `docker restart <container>` has automatic rollback (if it
  comes up broken, restart it again or recreate from the same image).
  Worth noting in advance: confirm the grok-bridge **image tag** is pinned
  in compose before the restart, so a recreate uses the same code.
- BUG-026 revert: the `v4.3.1` Docker image is on the Hub
  (`dblagbro/llm-proxy2:4.3.1`) — rollback is one retag + recreate per
  node, same procedure as any other llm-proxy2 release.
