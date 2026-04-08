/**
 * Provider Hold-Down System
 *
 * Replaces the old circuit breaker with a per-provider hold-down + retest mechanism.
 *
 * Per-provider config fields (stored in providers.json / SQLite):
 *   holdDownSeconds   — seconds to hold a provider after failureThreshold failures (default 180, 0 = disabled)
 *   maxLatencyMs      — enforced in the routing loop via Promise.race (default 1200, 0 = no limit)
 *   failureThreshold  — consecutive failures before hold-down kicks in (default 2)
 *
 * Hold-down lifecycle:
 *   failures >= threshold → enter hold-down (holdDownUntil = now + holdDownSeconds*1000)
 *   schedule retest at 90% of hold-down elapsed
 *   retest passes → clear hold-down immediately
 *   retest fails  → restart hold-down from now, schedule next retest
 *   manual release / manual hold via admin API
 */

'use strict';

const axios = require('axios');
const EventEmitter = require('events');

// Default values — override per provider via provider fields
const DEFAULT_HOLD_DOWN_SECONDS = 180;
const DEFAULT_FAILURE_THRESHOLD = 2;

class ProviderHoldDown extends EventEmitter {
  /**
   * @param {object}   logger      Winston logger
   * @param {Function} getProvider (providerId) => live provider object from config
   */
  constructor(logger, getProvider) {
    super();
    this.logger = logger;
    this.getProvider = getProvider;

    // In-memory hold-down state keyed by provider.id
    // { consecutiveFailures, holdDownUntil, retestTimer }
    this.state = new Map();

    this.logger.info('ProviderHoldDown initialized');
  }

  // ── Internal helpers ────────────────────────────────────────────────────

  _getState(providerId) {
    if (!this.state.has(providerId)) {
      this.state.set(providerId, {
        consecutiveFailures: 0,
        holdDownUntil: null,
        retestTimer: null
      });
    }
    return this.state.get(providerId);
  }

  _clearRetestTimer(state) {
    if (state.retestTimer) {
      clearTimeout(state.retestTimer);
      state.retestTimer = null;
    }
  }

  _holdDownSeconds(provider) {
    return provider.holdDownSeconds != null
      ? parseInt(provider.holdDownSeconds)
      : DEFAULT_HOLD_DOWN_SECONDS;
  }

  _failureThreshold(provider) {
    return provider.failureThreshold != null
      ? parseInt(provider.failureThreshold)
      : DEFAULT_FAILURE_THRESHOLD;
  }

  _scheduleRetest(provider, holdDownMs) {
    const state = this._getState(provider.id);
    this._clearRetestTimer(state);

    // Retest fires at 90% of hold-down duration
    const retestDelayMs = Math.max(holdDownMs * 0.9, 5000);

    state.retestTimer = setTimeout(async () => {
      state.retestTimer = null;
      // Fetch the live provider record in case config changed
      const live = this.getProvider(provider.id) || provider;
      await this._fireRetest(live);
    }, retestDelayMs);

    this.logger.info(`Hold-down retest scheduled for ${provider.name} in ${Math.round(retestDelayMs / 1000)}s`);
  }

  async _fireRetest(provider) {
    this.logger.info(`Hold-down retest firing for provider: ${provider.name} (${provider.id})`);
    try {
      await this._sendProbe(provider);
      // Probe passed — restore provider
      const state = this._getState(provider.id);
      state.consecutiveFailures = 0;
      state.holdDownUntil = null;
      this._clearRetestTimer(state);
      this.logger.info(`Hold-down retest PASSED for ${provider.name} — provider restored`);
      this.emit('holddown.cleared', { provider });
    } catch (err) {
      this.logger.warn(`Hold-down retest FAILED for ${provider.name}: ${err.message}`);
      const holdDownSeconds = this._holdDownSeconds(provider);
      if (holdDownSeconds > 0) {
        const holdDownMs = holdDownSeconds * 1000;
        const state = this._getState(provider.id);
        state.holdDownUntil = Date.now() + holdDownMs;
        this._scheduleRetest(provider, holdDownMs);
        this.logger.warn(`Hold-down restarted for ${provider.name} until ${new Date(state.holdDownUntil).toISOString()}`);
        this.emit('holddown.restarted', { provider });
      }
    }
  }

  async _sendProbe(provider) {
    // Minimal probe — NOT counted in stats, NOT cost-tracked, NOT logged to activity log
    const maxLatencyMs = (provider.maxLatencyMs != null && parseInt(provider.maxLatencyMs) > 0)
      ? parseInt(provider.maxLatencyMs)
      : 10000;

    switch (provider.type) {
      case 'anthropic':
        await axios.post(
          'https://api.anthropic.com/v1/messages',
          {
            model: provider.model || 'claude-haiku-4-5-20251001',
            messages: [{ role: 'user', content: 'hi' }],
            max_tokens: 1,
            stream: false
          },
          {
            headers: {
              'Content-Type': 'application/json',
              'x-api-key': provider.apiKey,
              'anthropic-version': '2023-06-01'
            },
            timeout: maxLatencyMs
          }
        );
        break;

      case 'openai':
        await axios.post(
          'https://api.openai.com/v1/chat/completions',
          {
            model: provider.model || 'gpt-4o-mini',
            messages: [{ role: 'user', content: 'hi' }],
            max_tokens: 1,
            stream: false
          },
          {
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer ${provider.apiKey}`
            },
            timeout: maxLatencyMs
          }
        );
        break;

      case 'grok':
        await axios.post(
          'https://api.x.ai/v1/chat/completions',
          {
            model: provider.model || 'grok-beta',
            messages: [{ role: 'user', content: 'hi' }],
            max_tokens: 1,
            stream: false
          },
          {
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer ${provider.apiKey}`
            },
            timeout: maxLatencyMs
          }
        );
        break;

      case 'google':
        await axios.post(
          `https://generativelanguage.googleapis.com/v1beta/models/${provider.model || 'gemini-2.5-flash'}:generateContent?key=${provider.apiKey}`,
          {
            contents: [{ role: 'user', parts: [{ text: 'hi' }] }],
            generationConfig: { maxOutputTokens: 1 }
          },
          {
            headers: { 'Content-Type': 'application/json' },
            timeout: maxLatencyMs
          }
        );
        break;

      case 'openai-compatible': {
        const baseUrl = provider.baseUrl || 'http://localhost:8080';
        await axios.post(
          `${baseUrl}/v1/chat/completions`,
          {
            model: provider.model || 'default',
            messages: [{ role: 'user', content: 'hi' }],
            max_tokens: 1,
            stream: false
          },
          {
            headers: {
              'Content-Type': 'application/json',
              'Authorization': `Bearer ${provider.apiKey}`
            },
            timeout: maxLatencyMs
          }
        );
        break;
      }

      case 'ollama': {
        const baseUrl = provider.baseUrl || 'http://localhost:11434';
        await axios.post(
          `${baseUrl}/api/chat`,
          {
            model: provider.model || 'llama2',
            messages: [{ role: 'user', content: 'hi' }],
            stream: false,
            options: { num_predict: 1 }
          },
          {
            headers: { 'Content-Type': 'application/json' },
            timeout: maxLatencyMs
          }
        );
        break;
      }

      case 'vertex':
        // Vertex requires OAuth tokens — skip probe, log warning only
        this.logger.warn(`Hold-down retest skipped for Vertex provider ${provider.name} (OAuth probe not supported)`);
        // Throw so the retest counts as failed and hold-down continues
        throw new Error('Vertex retest not supported — hold-down continues');

      default:
        throw new Error(`No probe support for provider type: ${provider.type}`);
    }
  }

  // ── Public API ───────────────────────────────────────────────────────────

  /**
   * Returns true if the provider is currently in hold-down and should be skipped.
   */
  isInHoldDown(provider) {
    const state = this._getState(provider.id);
    if (!state.holdDownUntil) return false;
    if (Date.now() >= state.holdDownUntil) {
      // Timer expired without retest clearing it — release automatically
      state.holdDownUntil = null;
      state.consecutiveFailures = 0;
      return false;
    }
    return true;
  }

  recordSuccess(provider) {
    const state = this._getState(provider.id);
    state.consecutiveFailures = 0;
    // holdDownUntil is managed by the retest timer — don't touch it here
  }

  /**
   * @param {object} provider
   * @param {number} latencyMs  Actual call latency in ms
   * @param {Error}  error      The error that caused the failure (may be null for latency breach)
   */
  recordFailure(provider, latencyMs, error) {
    const state = this._getState(provider.id);
    state.consecutiveFailures++;

    const holdDownSeconds = this._holdDownSeconds(provider);
    const failureThreshold = this._failureThreshold(provider);

    this.logger.warn(
      `Provider ${provider.name} failure #${state.consecutiveFailures}` +
      ` (threshold: ${failureThreshold}, latency: ${latencyMs}ms,` +
      ` error: ${error?.message || 'latency breach'})`
    );

    // Enter hold-down if threshold reached and not already held
    if (holdDownSeconds > 0 && state.consecutiveFailures >= failureThreshold && !state.holdDownUntil) {
      const holdDownMs = holdDownSeconds * 1000;
      state.holdDownUntil = Date.now() + holdDownMs;
      this._scheduleRetest(provider, holdDownMs);
      this.logger.error(
        `Provider ${provider.name} entering hold-down for ${holdDownSeconds}s` +
        ` (until ${new Date(state.holdDownUntil).toISOString()})`
      );
      this.emit('holddown.entered', { provider, consecutiveFailures: state.consecutiveFailures });
    }
  }

  /**
   * Manual release of hold-down via admin API.
   */
  manualRelease(providerId) {
    const state = this._getState(providerId);
    this._clearRetestTimer(state);
    state.consecutiveFailures = 0;
    state.holdDownUntil = null;
    this.logger.info(`Hold-down manually released for provider: ${providerId}`);
    this.emit('holddown.manual_release', { providerId });
  }

  /**
   * Manual hold-down via admin API.
   */
  manualHold(providerId, durationSeconds) {
    const provider = this.getProvider(providerId) || { id: providerId, name: providerId };
    const secs = durationSeconds != null ? parseInt(durationSeconds) : DEFAULT_HOLD_DOWN_SECONDS;
    const state = this._getState(providerId);
    this._clearRetestTimer(state);
    if (secs > 0) {
      const holdDownMs = secs * 1000;
      state.holdDownUntil = Date.now() + holdDownMs;
      this._scheduleRetest(provider, holdDownMs);
    }
    this.logger.warn(`Hold-down manually applied to provider: ${providerId} for ${secs}s`);
    this.emit('holddown.manual_hold', { providerId, durationSeconds: secs });
  }

  /**
   * Returns current hold-down state for all providers that have been touched.
   * Also exposes getState() as a public method for admin endpoints.
   */
  getState(providerId) {
    return this._getState(providerId);
  }

  getAllHoldDownStates() {
    const result = [];
    for (const [providerId, state] of this.state.entries()) {
      result.push({
        providerId,
        consecutiveFailures: state.consecutiveFailures,
        inHoldDown: state.holdDownUntil != null && Date.now() < state.holdDownUntil,
        holdDownUntil: state.holdDownUntil ? new Date(state.holdDownUntil).toISOString() : null,
        retestScheduled: state.retestTimer != null
      });
    }
    return result;
  }

  getMonitoringStatus() {
    return { holdDown: { states: this.getAllHoldDownStates() } };
  }

  // Lifecycle stubs (called by server.js startup/shutdown)
  start() { this.logger.info('ProviderHoldDown started'); }
  stop() {
    for (const state of this.state.values()) {
      this._clearRetestTimer(state);
    }
    this.logger.info('ProviderHoldDown stopped');
  }
}

module.exports = ProviderHoldDown;
