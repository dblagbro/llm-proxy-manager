# Changelog

All notable changes to the LLM Proxy Manager project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.4.0] - 2026-04-01

### Added
- **Analytics Dashboard**: New card on the main page showing total requests, cost, tokens, and success rate summary tiles. Per-provider panels display request count, success rate, avg latency, and cost with bar-chart sparklines colored by success/failure ratio. Time-window selector: Last Hour, Last 24h, Last 7 Days, All Time. Data comes from an in-memory hourly ring buffer (168 buckets = 7 days) updated on every request completion.
- **Live Stream Chat Logs**: The per-provider chat log viewer now has a 🔴 Live Stream toggle. When enabled, new log entries are pushed to the browser in real-time via Server-Sent Events instead of polling. The connection shows a live status indicator (● Live / ○ Disconnected).

### Changed
- Chat log modal auto-refresh checkbox replaced with Live Stream SSE toggle

## [1.3.9] - 2026-04-01

### Added
- **Layer 3 — Conductor/Worker Parallel Racing**: When `CONDUCTOR_MODE=true`, non-streaming requests race the top N providers simultaneously (configurable via `CONDUCTOR_WORKERS`, default 2). The first valid response wins. Falls through to sequential 3-pass routing if all workers fail. Streaming requests are unaffected (always sequential).
- **Layer 5 — Advanced Session Management**: Active session registry tracks all logged-in sessions with IP, user agent, login time, and last-active timestamp. Request correlation IDs (`x-request-id`) are injected on every request and returned in response headers. Sessions auto-extend on every authenticated request. New API endpoints: `GET /api/sessions`, `DELETE /api/sessions/:id`, `DELETE /api/sessions`.
- **Session Management UI**: Profile Settings modal now shows all active sessions with IP, browser, and timestamps. Current session is highlighted. Individual sessions can be revoked, or all other sessions revoked at once.

## [1.3.8] - 2026-04-01

### Added
- **Layer 1d — Streaming First-Chunk Buffer**: SSE headers are now held in a buffered proxy until the first data chunk actually arrives from the provider. This means the latency guard (`Promise.race`) can still trigger a failover if the provider accepts the connection but never sends data, fixing the "headers already sent, can't failover" problem for hanging providers.
- **Layer 4a — Context Window Auto-Truncation**: Before dispatching to each provider, the request messages are checked against that provider's known context window. If the estimated token count exceeds 85% of the window, oldest non-system messages are trimmed until it fits. The system prompt and most recent user turn are always preserved.
- **Layer 4b-4d — Structured Error Classification**: Every provider error is now classified (`auth_error`, `not_found`, `client_error`, `context_exceeded`, `rate_limit`, `transient`, `timeout`, `network`, `unknown`). Auth errors (401/403), 404s, client errors (400/422), and context-exceeded errors no longer trigger hold-down. Rate limits and transient errors (500/503/529) do. The category is logged in both the structured JSON log and the chat log failover entry.
- **Streaming Chat Logs for Gemini and OpenAI**: `streamGemini` and `streamOpenAI` now accumulate response text during streaming and write a complete `[ASSISTANT]` entry to the chat log on stream end, including latency and token count.

### Changed
- Piped streaming providers (Anthropic, Grok, Ollama, OpenAI-compatible) log a `[ASSISTANT] (streamed — text not captured)` note since their streams are piped directly without transformation

## [1.3.7] - 2026-04-01

### Added
- **Per-Provider Chat Logs** — Every request and response is written to `/app/logs/chat-<provider-name>.log` in human-readable format showing `[USER]` / `[ASSISTANT]` turns, tool calls, tool results, latency, token counts, and cost per request
- **Chat Log Viewer in Web UI** — Each provider panel now has a **📋 Log** button that opens a full-screen modal showing the provider's chat log with selectable line count (100–2000), manual refresh, and 4-second auto-refresh toggle
- Failovers and XML sentinel triggers are logged inline in the chat log

### Fixed
- Chat log modal fetch used absolute path `/api/...` which failed behind nginx subpath — changed to relative `./api/...`
- Chat log modal was too small; now 1100px wide, 92vh tall, flex-column layout with resizable `<pre>` area

## [1.3.6] - 2026-03-31

### Added
- **Layer 1c — Turn Validator**: Validates Gemini `contents` array before sending — warns on non-user first turn, consecutive same-role turns, empty parts arrays
- **Layer 1e — XML Sentinel**: Scans non-streaming responses for leaked internal XML/function tags (`<execute_bash>`, `<thought>`, `<function_calls>`, `<invoke>`, `functionCall {}`, `<parameter>`) and automatically fails over to the next provider
- **Layer 2 — Capability Router**: Filters providers before the 3-pass loop based on request requirements — tool calls require `toolCalling`, image content requires `vision`, long context requires sufficient `contextWindow`
- `PROVIDER_CAPS` table defining capabilities per provider type

### Changed
- `maxLatencyMs` default changed from 1200ms to **1800ms** in routing loop, provider mapper, and Web UI tooltip/placeholder

## [1.3.5] - 2026-03-30

### Fixed
- **Critical: Cost tracking broken** — `let result` was scoped inside the `else {}` block, making it inaccessible to cost tracking code after the block. Moved declaration to outer `try {}` scope. All costs were showing $0.00 before this fix.
- **Bug C1: Upstream error leak** — When all providers failed, the raw upstream error was returned to the client. Now returns a clean `503 overloaded_error` response.
- **Pricing fuzzy matching** — `calculateCost()` now strips version suffixes (`-001`, `-002`, `-exp`, `-latest`, `-preview-*`, date stamps like `-20241022`) to match model names that don't exactly match the pricing table

### Added
- `SESSION_SECRET` environment variable support — set a stable value to preserve sessions across container restarts
- `sessionTimeoutMinutes` in Settings modal — configurable login session duration (default changed from 15 min to **480 min / 8 hours**)

## [1.3.4] - 2026-03-29

### Added
- **Session Timeout field** in Settings modal — configurable login timeout with 8-hour default
- Session timeout applied at login time via `req.session.cookie.maxAge`

### Fixed
- Settings modal SMTP save was returning HTML (502 during container restart) — not a code bug; verified endpoint works correctly

## [1.3.3] - 2026-03-29

### Fixed
- All 5 failing Playwright auth.spec.js tests:
  - Logout: open `.user-dropdown-toggle` first, then scoped menu item selector
  - Add provider: corrected submit button selector, added `.first()` for strict mode
  - Toggle: use `dispatchEvent('click')` to bypass CSS visibility issue
  - Settings modal: `.first()` on h3 to avoid strict mode violation
  - API config: scoped textarea to `#settingsModal`

## [1.3.2] - 2026-03-28

### Added
- Hold-down monitoring system with per-provider consecutive failure tracking
- Retest at 90% of hold-down timer before full release
- Hold-down status visible in Web UI and via `/api/holddown-status`
- Manual release via `/monitoring/holddown/release`

## [1.2.1] - 2026-03-28

### Fixed
- **OpenAI Message Format Conversion**: Fixed HTTP 404 errors when using OpenAI providers
  - Anthropic-format messages (content as array of objects) were passed directly to OpenAI API
  - Added conversion from `[{type: 'text', text: '...'}]` to simple string content
  - Fixes both streaming and non-streaming OpenAI requests

## [1.2.0] - 2026-03-28

### Added
- **Forgot Password**: Complete email-based password reset flow with secure tokens (1-hour expiry, one-time use)
- **SMTP Configuration**: Full email notification system in Settings modal with test email functionality
- **Enhanced Cost Tracking**: Improved model detection, checks `result.model` / `req.body.model` / `provider.model`

### Fixed
- Dark mode styling: readonly input fields now use CSS variables instead of hardcoded `#f8f9fa`

### Security
- Password reset tokens expire after 1 hour, one-time use only
- Generic error messages prevent username enumeration

## [1.1.9] - 2026-03-27

### Fixed
- Cost tracking: improved model detection priority order with debug logging

## [1.1.8] - 2026-03-27

### Added
- Web-based SMTP configuration UI in Settings modal

## [1.1.0] - 2026-03-25

### Added
- Initial release with core features
- Multi-provider support (Anthropic, Google, OpenAI, Grok, Ollama)
- Automatic failover with circuit breaker
- Streaming support for all providers
- Cost tracking and statistics
- Web-based management UI
- Docker deployment support
- Cluster sync with heartbeat

[1.3.7]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.3.6...v1.3.7
[1.3.6]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.3.5...v1.3.6
[1.3.5]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.3.4...v1.3.5
[1.3.4]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.3.3...v1.3.4
[1.3.3]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.3.2...v1.3.3
[1.3.2]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.2.1...v1.3.2
[1.2.1]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.1.9...v1.2.0
[1.1.9]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.1.8...v1.1.9
[1.1.8]: https://github.com/dblagbro/llm-proxy-manager/compare/v1.1.0...v1.1.8
[1.1.0]: https://github.com/dblagbro/llm-proxy-manager/releases/tag/v1.1.0
