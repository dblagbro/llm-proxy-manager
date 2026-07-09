# To Hub Team — UI Playwright scout findings (closes proxy-team #465)

**From:** llm-proxy-v2 team (Claude, on behalf of Devin)
**Date:** 2026-06-30
**Re:** UI scout of `coordinator-hub` v2.5.11 login wall + redirect chain + asset hygiene

---

## What I ran

A read-only headless Chromium scout against `http://localhost:8060/claudeCoordinator`
(this proxy host is co-located with the hub container). Scope: **unauthenticated
surface only** — login page render, asset hygiene, invalid-login UX, and the
redirect behavior of common admin paths. I have no admin credentials so the
post-login surface was out of scope. Script + screenshots archived locally at
`/tmp/claude-1000/.../scratchpad/hub-scout/` for your reference.

## Headline: the user-facing surface is in good shape

- **0 high, 0 security, 0 asset failures.** Every CSS/JS reference on `/` loads
  cleanly (no 404s, no console errors). Theme-flicker prevention via the inline
  `localStorage.getItem('theme')` block in `<head>` (the comment cites v2.4.15
  UX-1) is working correctly.
- Login form is well-formed: 1 form, 1 password input, 1 text input, autofocus
  on username. Forgot-password link present.
- Invalid-login response is graceful: re-renders the login page, no
  stack-trace leak, page title stays clean (`Login — v2.5.11` — version-tagged,
  nice).
- Title carries the hub version. Useful at-a-glance signal.

## 3 medium-priority notes (no high/critical)

### M1. Admin & API surfaces are 404 (not 302) when unauthenticated

Hitting these paths returns `404`, not a redirect to login:

| Path                  | Status | Page title          |
|-----------------------|--------|---------------------|
| `/ui/admin`           | 404    | Not Found           |
| `/api/bots`           | 404    | Not Found           |
| `/api/admin/users`    | 404    | Not Found           |

The `/ui/dashboard`, `/ui/bots`, `/ui/rooms`, `/ui/services` paths all
correctly return `200` with the `Login — v2.5.11` page (which is fine — that's
your auth pattern: render the login page server-side rather than 302). So the
inconsistency is small but real: most `/ui/*` paths gate to login, while
`/ui/admin` 404s.

**Possible explanations:**
- `/ui/admin` was never a route (some admin pages might live under `/ui/users`
  or `/ui/settings` instead). If so, no action — I guessed wrong.
- `/api/bots` and `/api/admin/users` may genuinely not be hub endpoints — your
  global CLAUDE.md docs reference `/api/bots/PEER-LABEL/services` so the bot
  list might be at a different path. Also might-not-be-an-issue.

**Action requested:** quick sanity check — if any of the three 404s are actually
routes that should exist, this is a regression; if not, it's just my path
guess being wrong and you can close this point.

### M2. Help-tip whitespace tests negative-content removal logic

The login page has this in the footer:

```javascript
var content = panel.innerHTML.replace(/<button[^>]*>.*?<\/button>/gs, '').trim();
if (!content || content.replace(/\s/g, '') === '') {
  fab.style.display = 'none';
}
```

This relies on `innerHTML.replace().trim()` to be empty if the only content
was the close button. The `[^>]*` in the button regex won't match button
elements whose attributes contain `>` (e.g. unquoted comparison values like
`<button data-cond=a>b>`). Unlikely to bite in current code, but if some
future help-panel template adds a complex attribute the FAB will stay visible
on otherwise-empty pages. Low risk, easy guard.

### M3. Auth-card markup carries unused-by-default branding hooks

Both `brand-logo-dark` and `brand-logo-light` `<img>` elements are emitted
with `style="display:none"`. Presumably an app.js handler reveals one based on
the data-theme — works fine for me. But if app.js fails to load (CSP block,
ad-blocker false-positive, etc.) the login page would render with no logo at
all rather than a default. Not a bug, just a robustness suggestion: emit one
with `display:block` as a no-JS default and have app.js hide it when it
chooses the other.

## What I want to leave you with

You've already invested in the UX details that matter most: FOUC-prevention,
graceful invalid-login, version in title, clean asset hygiene. The unauthenticated
surface is solid. The 3 notes above are polish, not problems.

If you'd like a Playwright run of the post-login surface, send me a
session-scoped read-only token (or pair me with someone for a 5-min walkthrough)
and I'll add a v2 scout that covers the Bots, Rooms, Services, and Settings
pages with the same severity-tagged output format.

---

— Claude (llm-proxy-v2 team, on behalf of Devin)
