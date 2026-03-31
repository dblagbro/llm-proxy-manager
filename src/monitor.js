/**
 * Provider Health Monitoring and Circuit Breaker
 * Implements intelligent failure detection with external service monitoring
 */

const axios = require('axios');
const EventEmitter = require('events');

class ProviderMonitor extends EventEmitter {
  constructor(logger) {
    super();
    this.logger = logger;

    // Circuit breaker configuration
    this.circuitBreakerConfig = {
      failureThreshold: parseInt(process.env.CIRCUIT_BREAKER_THRESHOLD || '3'),
      timeout: parseInt(process.env.CIRCUIT_BREAKER_TIMEOUT || '60000'), // 60 seconds
      halfOpenTimeout: parseInt(process.env.CIRCUIT_BREAKER_HALFOPEN || '30000'), // 30 seconds
      successThreshold: parseInt(process.env.CIRCUIT_BREAKER_SUCCESS || '2') // successes to close circuit
    };

    // Provider timeouts (milliseconds)
    this.providerTimeouts = {
      anthropic: parseInt(process.env.ANTHROPIC_TIMEOUT || '30000'),
      google: parseInt(process.env.GOOGLE_TIMEOUT || '30000'),
      openai: parseInt(process.env.OPENAI_TIMEOUT || '30000'),
      grok: parseInt(process.env.GROK_TIMEOUT || '30000'),
      ollama: parseInt(process.env.OLLAMA_TIMEOUT || '60000'), // Local models may be slower
      vertex: parseInt(process.env.VERTEX_TIMEOUT || '30000'),
      'openai-compatible': parseInt(process.env.COMPATIBLE_TIMEOUT || '30000')
    };

    // Circuit breaker states per provider
    this.circuits = new Map(); // provider.id -> { state, failures, lastFailure, lastCheck }

    // External service status cache
    this.serviceStatus = new Map(); // provider.type -> { status, lastCheck, source }

    // Status page URLs for external monitoring
    this.statusPages = {
      anthropic: 'https://status.anthropic.com/api/v2/summary.json',
      openai: 'https://status.openai.com/api/v2/summary.json',
      google: 'https://status.cloud.google.com/incidents.json'
    };

    // Error patterns for billing/quota failures
    this.billingErrors = [
      /insufficient.*credit/i,
      /quota.*exceeded/i,
      /billing.*issue/i,
      /payment.*required/i,
      /subscription.*expired/i,
      /rate.*limit.*exceeded/i,
      /429.*too.*many.*requests/i
    ];

    // Monitor interval
    this.monitorInterval = null;

    this.logger.info('Provider Monitor initialized');
  }

  start() {
    // Check external status pages every 5 minutes
    this.monitorInterval = setInterval(() => {
      this.checkExternalStatus();
    }, 300000);

    // Initial check after 10 seconds
    setTimeout(() => this.checkExternalStatus(), 10000);

    this.logger.info('Provider Monitor started');
  }

  stop() {
    if (this.monitorInterval) {
      clearInterval(this.monitorInterval);
    }
    this.logger.info('Provider Monitor stopped');
  }

  getProviderTimeout(providerType) {
    return this.providerTimeouts[providerType] || 30000;
  }

  // Get circuit breaker config for a provider, with per-provider overrides
  getCircuitConfig(provider) {
    const cb = provider.circuitBreaker || {};
    return {
      failureThreshold: cb.failureThreshold != null ? parseInt(cb.failureThreshold) : this.circuitBreakerConfig.failureThreshold,
      timeout: cb.timeout != null ? parseInt(cb.timeout) * 1000 : this.circuitBreakerConfig.timeout,
      halfOpenTimeout: cb.halfOpenTimeout != null ? parseInt(cb.halfOpenTimeout) * 1000 : this.circuitBreakerConfig.halfOpenTimeout,
      successThreshold: cb.successThreshold != null ? parseInt(cb.successThreshold) : this.circuitBreakerConfig.successThreshold
    };
  }

  // Circuit Breaker State Machine
  getCircuitState(providerId) {
    if (!this.circuits.has(providerId)) {
      this.circuits.set(providerId, {
        state: 'CLOSED', // CLOSED, OPEN, HALF_OPEN
        failures: 0,
        successes: 0,
        lastFailure: null,
        lastCheck: null,
        reason: null
      });
    }
    return this.circuits.get(providerId);
  }

  canAttemptProvider(provider) {
    const circuit = this.getCircuitState(provider.id);
    const now = Date.now();

    // Provider explicitly disabled
    if (!provider.enabled) {
      return { allowed: false, reason: 'Provider disabled' };
    }

    // Check external service status
    const externalStatus = this.serviceStatus.get(provider.type);
    if (externalStatus && externalStatus.status === 'major_outage') {
      return {
        allowed: false,
        reason: `${provider.type} reporting major outage (${externalStatus.source})`
      };
    }

    // Circuit breaker logic
    switch (circuit.state) {
      case 'CLOSED':
        // Normal operation
        return { allowed: true, reason: 'Circuit closed' };

      case 'OPEN': {
        // Check if timeout expired -> move to HALF_OPEN
        const cbCfg = this.getCircuitConfig(provider);
        if (circuit.lastFailure && (now - circuit.lastFailure > cbCfg.timeout)) {
          circuit.state = 'HALF_OPEN';
          circuit.successes = 0;
          this.logger.info(`Circuit HALF_OPEN for provider: ${provider.name} (${provider.id})`);
          this.emit('circuit.half_open', { provider });
          return { allowed: true, reason: 'Circuit half-open (testing)' };
        }
        return {
          allowed: false,
          reason: `Circuit open until ${new Date(circuit.lastFailure + cbCfg.timeout).toISOString()}`
        };
      }

      case 'HALF_OPEN':
        // Allow limited testing
        return { allowed: true, reason: 'Circuit half-open (testing)' };

      default:
        return { allowed: true, reason: 'Unknown state' };
    }
  }

  recordSuccess(provider) {
    const circuit = this.getCircuitState(provider.id);

    if (circuit.state === 'HALF_OPEN') {
      circuit.successes++;

      if (circuit.successes >= this.getCircuitConfig(provider).successThreshold) {
        // Restore provider
        circuit.state = 'CLOSED';
        circuit.failures = 0;
        circuit.successes = 0;
        circuit.reason = null;
        this.logger.info(`Circuit CLOSED for provider: ${provider.name} (${provider.id}) - recovered`);
        this.emit('circuit.closed', { provider });
      }
    } else if (circuit.state === 'CLOSED') {
      // Decay failure count on success
      circuit.failures = Math.max(0, circuit.failures - 1);
    }

    circuit.lastCheck = Date.now();
  }

  recordFailure(provider, error) {
    const circuit = this.getCircuitState(provider.id);
    const now = Date.now();

    circuit.failures++;
    circuit.lastFailure = now;
    circuit.lastCheck = now;

    // Check for billing/quota errors
    const isBillingError = this.detectBillingError(error);
    if (isBillingError) {
      circuit.reason = 'Billing/quota issue detected';
      this.emit('billing.error', { provider, error: error.message });
      this.logger.error(`Billing error detected for ${provider.name}: ${error.message}`);
    }

    // Open circuit if threshold reached
    if (circuit.state === 'CLOSED' && circuit.failures >= this.getCircuitConfig(provider).failureThreshold) {
      circuit.state = 'OPEN';
      circuit.reason = isBillingError ? 'Billing/quota issue' : 'Too many failures';
      this.logger.error(`Circuit OPEN for provider: ${provider.name} (${provider.id}) - ${circuit.reason}`);
      this.emit('circuit.open', { provider, reason: circuit.reason });
    } else if (circuit.state === 'HALF_OPEN') {
      // Failed during testing, reopen circuit
      circuit.state = 'OPEN';
      circuit.successes = 0;
      circuit.reason = 'Test failed';
      this.logger.warn(`Circuit re-OPEN for provider: ${provider.name} (${provider.id})`);
      this.emit('circuit.reopen', { provider });
    }
  }

  detectBillingError(error) {
    const errorMsg = error.message || error.toString();
    return this.billingErrors.some(pattern => pattern.test(errorMsg));
  }

  async checkExternalStatus() {
    this.logger.info('Checking external service status pages...');

    for (const [providerType, statusUrl] of Object.entries(this.statusPages)) {
      try {
        const response = await axios.get(statusUrl, {
          timeout: 10000,
          headers: { 'User-Agent': 'LLM-Proxy-Manager/1.0' }
        });

        let status = 'operational';
        let incidents = [];

        // Parse response based on provider
        if (providerType === 'anthropic' || providerType === 'openai') {
          // Statuspage.io format
          const data = response.data;
          if (data.status && data.status.indicator) {
            switch (data.status.indicator) {
              case 'none':
                status = 'operational';
                break;
              case 'minor':
                status = 'degraded';
                break;
              case 'major':
              case 'critical':
                status = 'major_outage';
                break;
            }
          }

          if (data.components) {
            // Check API component specifically
            const apiComponent = data.components.find(c =>
              c.name.toLowerCase().includes('api')
            );
            if (apiComponent && apiComponent.status !== 'operational') {
              status = 'degraded';
            }
          }
        } else if (providerType === 'google') {
          // Google Cloud Status format
          const data = response.data;
          if (Array.isArray(data) && data.length > 0) {
            // Recent incidents
            const recentIncidents = data.filter(incident => {
              const created = new Date(incident.created);
              const hoursSince = (Date.now() - created.getTime()) / 3600000;
              return hoursSince < 24 && incident.currently_affected;
            });

            if (recentIncidents.length > 0) {
              status = 'degraded';
              incidents = recentIncidents.map(i => i.external_desc);
            }
          }
        }

        this.serviceStatus.set(providerType, {
          status: status,
          lastCheck: new Date(),
          source: statusUrl,
          incidents: incidents
        });

        if (status !== 'operational') {
          this.logger.warn(`External status for ${providerType}: ${status}`);
          this.emit('external.degraded', { providerType, status, incidents });
        } else {
          this.logger.info(`External status for ${providerType}: operational`);
        }

      } catch (err) {
        this.logger.warn(`Failed to check status page for ${providerType}: ${err.message}`);
        // Don't update status if we can't reach the status page
      }
    }
  }

  getExternalStatus(providerType) {
    return this.serviceStatus.get(providerType) || {
      status: 'unknown',
      lastCheck: null,
      source: null
    };
  }

  getAllCircuitStates() {
    const states = [];
    for (const [providerId, circuit] of this.circuits.entries()) {
      states.push({
        providerId,
        state: circuit.state,
        failures: circuit.failures,
        successes: circuit.successes,
        lastFailure: circuit.lastFailure ? new Date(circuit.lastFailure).toISOString() : null,
        reason: circuit.reason,
        nextRetry: circuit.state === 'OPEN' && circuit.lastFailure
          ? new Date(circuit.lastFailure + this.circuitBreakerConfig.timeout).toISOString()
          : null
      });
    }
    return states;
  }

  getMonitoringStatus() {
    const externalStatus = {};
    for (const [providerType, status] of this.serviceStatus.entries()) {
      externalStatus[providerType] = {
        status: status.status,
        lastCheck: status.lastCheck ? status.lastCheck.toISOString() : null,
        incidents: status.incidents || []
      };
    }

    return {
      circuitBreaker: {
        config: this.circuitBreakerConfig,
        states: this.getAllCircuitStates()
      },
      externalStatus: externalStatus,
      timeouts: this.providerTimeouts
    };
  }

  // Manual circuit control (for emergency override)
  manualReset(providerId) {
    const circuit = this.getCircuitState(providerId);
    circuit.state = 'CLOSED';
    circuit.failures = 0;
    circuit.successes = 0;
    circuit.lastFailure = null;
    circuit.reason = 'Manual reset';
    this.logger.info(`Circuit manually CLOSED for provider: ${providerId}`);
    this.emit('circuit.manual_reset', { providerId });
  }

  manualOpen(providerId, reason = 'Manual override') {
    const circuit = this.getCircuitState(providerId);
    circuit.state = 'OPEN';
    circuit.reason = reason;
    circuit.lastFailure = Date.now();
    this.logger.warn(`Circuit manually OPEN for provider: ${providerId} - ${reason}`);
    this.emit('circuit.manual_open', { providerId, reason });
  }
}

module.exports = ProviderMonitor;
