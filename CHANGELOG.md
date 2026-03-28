# Changelog

All notable changes to the LLM Proxy Manager project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-03-28

### Added
- **Forgot Password Feature**: Complete email-based password reset flow
  - Secure token generation with 1-hour expiration
  - One-time use tokens
  - Professional HTML email template
  - New `/reset-password.html` page
  - Backend endpoints: `/api/auth/forgot-password`, `/api/auth/verify-reset-token`, `/api/auth/reset-password`
- **SMTP Configuration**: Full email notification system integrated into Settings modal
  - Configurable via Web UI (Settings → Email Notifications)
  - Test email functionality
  - Used for both password resets and system alerts
- **Enhanced Cost Tracking**: Improved model detection and debug logging
  - Now checks multiple sources for accurate model pricing
  - Detailed logging shows model, token counts, and calculated cost per request
  - Fixes issue where costs showed $0.00

### Fixed
- **Dark Mode Styling**: Fixed white text on white background issue
  - All readonly input fields now properly styled with CSS variables
  - Removed hardcoded background colors (#f8f9fa)
  - Improved contrast in both light and dark themes
  - Affected settings modal, API keys, configuration displays

### Security
- Password reset tokens expire after 1 hour
- Tokens are one-time use only (marked as used after successful reset)
- Generic error messages prevent username enumeration
- All password reset activity logged for audit trail
- Validates SMTP configuration before sending emails

## [1.1.9] - 2026-03-27

### Fixed
- **Cost Tracking**: Improved model detection for accurate cost calculation
  - Checks `result.model`, `req.body.model`, and `provider.model` in priority order
  - Added debug logging for cost tracking diagnostics

## [1.1.8] - 2026-03-27

### Added
- Web-based SMTP configuration UI
- Settings modal now includes Email Notifications section

## [1.1.0] - 2026-03-25

### Added
- Initial release with core features
- Multi-provider support (Anthropic, Google, OpenAI, Grok, Ollama)
- Automatic failover with circuit breaker
- Streaming support for all providers
- Cost tracking and statistics
- Web-based management UI
- Docker deployment support

[1.2.0]: https://github.com/yourusername/llm-proxy-manager/compare/v1.1.9...v1.2.0
[1.1.9]: https://github.com/yourusername/llm-proxy-manager/compare/v1.1.8...v1.1.9
[1.1.8]: https://github.com/yourusername/llm-proxy-manager/compare/v1.1.0...v1.1.8
[1.1.0]: https://github.com/yourusername/llm-proxy-manager/releases/tag/v1.1.0
