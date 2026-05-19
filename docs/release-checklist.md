# Release checklist — manual post-deploy verification

`tools/cut-release.sh` automates the release ceremony (tag, GitHub release,
image push, backup) and the unit + integration suites cover the automatable
checks. This file lists the **manual** checks a human must do after a
fleet deploy — the things that cannot be verified by headless automation.

Run these on each release, on a **real browser**, against the live fleet.

## Every release

- [ ] `GET /llm-proxy2/health` on all 3 nodes → `status:healthy`, the
      expected `version`, `healthyProviders` matches expectations.
- [ ] Log in on a real browser; the dashboard and the Routing/LMRH page
      render with no console errors.
- [ ] Spot-check the release's headline feature end-to-end.

## Voice — text-to-speech (v4.3+)

Headless Chromium has no audio device, so automated tests verify the
**wiring** (`/api/airi/speak` fires, a valid WAV comes back) but **not that
sound is actually produced**. This must be checked by a human (BUG-022):

- [ ] On the Routing page, turn on the **speaker toggle** (the 🔊 button by
      the AIRI input).
- [ ] Send a chat message to AIRI and wait for the reply.
- [ ] **Confirm Airy's answer is spoken aloud** and is intelligible.
- [ ] Send a second message while the first is still speaking → the new
      utterance supersedes it (Airy does not talk over itself).
- [ ] Tap the speaker button while it is speaking → playback stops.
- [ ] Known edge case to watch: browser autoplay policy. The speaker is
      enabled by a click (a user gesture), but playback is triggered later
      by a message-completion event. If a browser ever refuses to play,
      that is the autoplay policy — note it; the fix is to prime the
      `<audio>` element inside the toggle's click handler.

## Voice — input (v4.2+)

- [ ] Push-to-talk: tap the mic, speak, confirm the transcript fills the
      input (and does **not** auto-send).
- [ ] Hands-free: enable it, say "Airy …", confirm the command is captured.
