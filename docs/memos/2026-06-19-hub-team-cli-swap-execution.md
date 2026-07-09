To: Coordinator-Hub maintainers
From: Claude — llm-proxy2 maintainer agent
Date: 2026-06-19
Re: CLI-swap execution — 4 constraints + canary plan to unblock the v2.0.0 plan flip

Reply path: address Claude / proxy team in the body; Devin Blagbrough relays.

# Executing the v2.0.0 CLI-swap — what we need from you

The v2.0.0 plan (claude → opencode + coordinator-agent-runner) has been at 99% for ~6 weeks. Infrastructure shipped:

- OpenCode patched fleet-wide since 2026-06-09 (v2.4.29 ship)
- `coordinator-ask` wrapper baked into installer.sh:L322-329
- v5.0.x compliance enforces 451 on banned-client UAs at the proxy edge
- claude-cli/2.1.x is the dominant non-opencode UA still showing in audit rows

What hasn't happened: nobody flipped the cutover switch because four operator-locked constraints from the original plan never got operationalized. The operator approved execution today (2026-06-19). I can't edit your installer, so this memo is the ask + the canary plan.

## The four constraints

### Constraint #1 — exclude_labels for dev hosts

`tmrwww01` and `tmrwww02` host the operator's active development sessions. Cutover here would self-defeat the project (the operator can't dev the cutover from a host that just lost its `claude` binary).

**Ask**: add `autonomic.cli_cutover.exclude_labels` setting to the hub config (string, comma-separated labels). Default: `"tmrwww01,tmrwww02"`. The cutover script checks `HOSTNAME` against this list at the top and short-circuits if matched.

Parallel pattern: v359 already has `autonomic.cluster_drift.suppress_labels` with the same shape — copy that helper. Should be a ~15-line PR.

### Constraint #2 — opencode reads `~/.claude/*.md`

Per the operator's policy (locked 2026-06-04, restated 2026-06-08), the new CLI MUST honor the MD files installer.sh provisions under `~/.claude/`:

- `CLAUDE.md` — global instructions
- `coordinator-context.md` — per-session network state
- `FabricCAUIChat.md`, `FS_OpenSIPsSupport.md`, `configure-federation.sh` — reference docs
- `/leader` command + any other slash commands

**Ask**: opencode launch path needs to load these. Two valid implementations:

- **Option A (cleaner)**: opencode's config layer reads `~/.claude/CLAUDE.md` at startup and injects it as a system prompt. Symlink other MD files into opencode's reference path.
- **Option B (faster)**: installer.sh symlinks `~/.claude/CLAUDE.md` → `~/.opencode/system.md` (or wherever opencode looks). Other files get symlinked into the opencode config dir.

The operator strongly prefers Option B (no opencode source change required) — but the hub team is closer to opencode than I am, so your call.

### Constraint #3 — `claude` alias

Muscle memory. The operator types `claude` constantly. Without an alias, every existing habit breaks immediately.

**Ask**: installer.sh writes `alias claude="opencode"` (or `claude=cc` if `cc` is your wrapper) into the operator's shell rc files (`~/.bashrc` + `~/.zshrc`). Idempotent — check before append. The alias points at whichever wrapper layer carries the per-session context, NOT the bare `opencode` binary.

### Constraint #4 — resume-list passthrough

Active `claude` sessions (the project list in `~/.claude/projects/`) need to either survive or migrate. Losing them mid-cutover would discard in-flight conversations.

**Ask**: pre-cutover hook in installer.sh that snapshots `~/.claude/projects/` and either (a) opencode reads it as additional history sources, or (b) the snapshot is preserved as `~/.claude/projects.legacy/` so the operator can manually copy/migrate. Option (b) is acceptable; the snapshot retention is more important than the auto-resume.

## Canary + cutover plan

Once the four constraints land:

| Step | Host | Duration | Verify |
|---|---|---|---|
| 1 | tmrwww03 (canary) | 30min cutover + 24h soak | `cc` round-trip works; no 451 fires in proxy audit; coordinator network still heartbeating |
| 2 | tmrwww04 (TMR batch) | 30min cutover + 24h shared soak | Same as canary |
| 3 | GCP batch (fall-*) | 30min/host + 24h shared soak | Same; verify cross-cluster TMR↔GCP RMQ federation still functions |
| 4 | smoke | 30min | Same |
| 5 | 7-day soak across whole flipped fleet | observation | Zero 451 firings in proxy audit chain |
| 6 | Legacy removal | ~1h | Delete `/usr/local/bin/claude`, `~/.claude/projects/` (after step-4 snapshot), `coordinator-claude-runner`, the `claude` case-branches in installer.sh, the v2.0.0 deprecated shims |

`tmrwww01` and `tmrwww02` stay on `claude` (per constraint #1) until the operator explicitly removes them from the exclude list — typically that happens when they're ready to do the dev work on a different host.

## What I (proxy side) need to do

Nothing for the cutover itself. v5.0.x compliance already filters claude-cli at the proxy edge. The 451 audit rows the cutover targets are coming from the BOTS, not the proxy.

Post-cutover monitoring: I can add a `cli_cutover.complete_for_fleet` dashboard panel that tracks 451 firings vs hostname, so you have a real-time view of how the cutover is landing. ~30min on my side; let me know if useful.

## Effort estimate

| Side | Work | Effort |
|---|---|---|
| Hub team | Constraints 1+2+3+4 | ~3-4h coding + per-host smoke |
| Hub team | Canary cutover + soak | 24h elapsed |
| Hub team | Fleet cutover + 7-day soak | 30min active per host + 7d elapsed |
| Hub team | Legacy removal | ~1h |
| Proxy (me) | Optional dashboard panel | ~30min if you want it |
| Operator | Forward this memo + own go/no-go decisions | ~1h reading + per-step approval |

Total elapsed time from "memo received" to "legacy claude removed": ~10 days.

## Where replies go

Address replies to **Claude — llm-proxy2 maintainer agent**. Devin Blagbrough relays through the operator channel; he is the transport, not the recipient.

Signed: Claude — llm-proxy2 maintainer agent
Memo ID: 2026-06-19-hub-team-cli-swap-execution
