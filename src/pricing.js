/**
 * Provider Pricing Database and Cost Calculation
 * Tracks costs per provider/model with real-time cost calculation
 */

class PricingManager {
  constructor() {
    // Pricing database - cost per 1M tokens (USD)
    // Updated as of March 2026
    this.pricing = {
      // Anthropic Claude
      'claude-sonnet-4-5-20250929': { input: 3.00, output: 15.00, provider: 'anthropic' },
      'claude-3-5-sonnet-20241022': { input: 3.00, output: 15.00, provider: 'anthropic' },
      'claude-3-opus-20240229': { input: 15.00, output: 75.00, provider: 'anthropic' },
      'claude-3-sonnet-20240229': { input: 3.00, output: 15.00, provider: 'anthropic' },
      'claude-3-haiku-20240307': { input: 0.25, output: 1.25, provider: 'anthropic' },

      // OpenAI GPT
      'gpt-4o': { input: 2.50, output: 10.00, provider: 'openai' },
      'gpt-4o-mini': { input: 0.15, output: 0.60, provider: 'openai' },
      'gpt-4-turbo': { input: 10.00, output: 30.00, provider: 'openai' },
      'gpt-4': { input: 30.00, output: 60.00, provider: 'openai' },
      'gpt-3.5-turbo': { input: 0.50, output: 1.50, provider: 'openai' },

      // Google Gemini
      'gemini-2.5-flash': { input: 0.10, output: 0.40, provider: 'google' },
      'gemini-2.0-flash': { input: 0.10, output: 0.40, provider: 'google' },
      'gemini-1.5-pro': { input: 1.25, output: 5.00, provider: 'google' },
      'gemini-1.5-flash': { input: 0.075, output: 0.30, provider: 'google' },

      // xAI Grok
      'grok-beta': { input: 5.00, output: 15.00, provider: 'grok' },
      'grok-2': { input: 5.00, output: 15.00, provider: 'grok' },

      // Vertex AI (Google Cloud pricing)
      'gemini-pro': { input: 0.50, output: 1.50, provider: 'vertex' },

      // Ollama (local - free)
      'llama2': { input: 0, output: 0, provider: 'ollama' },
      'llama3': { input: 0, output: 0, provider: 'ollama' },
      'mistral': { input: 0, output: 0, provider: 'ollama' },
      'codellama': { input: 0, output: 0, provider: 'ollama' }
    };

    // Provider capabilities
    this.capabilities = {
      anthropic: {
        streaming: true,
        vision: true,
        maxTokens: 200000,
        contextWindow: 200000
      },
      google: {
        streaming: true,
        vision: true,
        maxTokens: 8192,
        contextWindow: 1000000
      },
      openai: {
        streaming: true,
        vision: true,
        maxTokens: 16384,
        contextWindow: 128000
      },
      grok: {
        streaming: true,
        vision: false,
        maxTokens: 8192,
        contextWindow: 131072
      },
      ollama: {
        streaming: true,
        vision: false,
        maxTokens: 8192,
        contextWindow: 8192
      },
      vertex: {
        streaming: false,
        vision: true,
        maxTokens: 8192,
        contextWindow: 1000000
      },
      'openai-compatible': {
        streaming: true,
        vision: false,
        maxTokens: 8192,
        contextWindow: 8192
      }
    };
  }

  /**
   * Calculate cost for a request
   * @param {string} model - Model name
   * @param {number} inputTokens - Input tokens used
   * @param {number} outputTokens - Output tokens used
   * @returns {number} Cost in USD
   */
  calculateCost(model, inputTokens, outputTokens) {
    const pricing = this.pricing[model];

    if (!pricing) {
      // Unknown model - return 0 or estimate based on provider
      return 0;
    }

    const inputCost = (inputTokens / 1000000) * pricing.input;
    const outputCost = (outputTokens / 1000000) * pricing.output;

    return inputCost + outputCost;
  }

  /**
   * Get pricing info for a model
   * @param {string} model - Model name
   * @returns {object|null} Pricing info or null
   */
  getPricing(model) {
    return this.pricing[model] || null;
  }

  /**
   * Get all models for a provider type
   * @param {string} providerType - Provider type
   * @returns {array} Array of model names
   */
  getModelsForProvider(providerType) {
    return Object.keys(this.pricing).filter(
      model => this.pricing[model].provider === providerType
    );
  }

  /**
   * Get provider capabilities
   * @param {string} providerType - Provider type
   * @returns {object} Capabilities object
   */
  getCapabilities(providerType) {
    return this.capabilities[providerType] || {
      streaming: false,
      vision: false,
      maxTokens: 4096,
      contextWindow: 4096
    };
  }

  /**
   * Check if model supports streaming
   * @param {string} model - Model name
   * @returns {boolean} True if streaming supported
   */
  supportsStreaming(model) {
    const pricing = this.pricing[model];
    if (!pricing) return false;

    const capabilities = this.capabilities[pricing.provider];
    return capabilities ? capabilities.streaming : false;
  }

  /**
   * Get cost estimate for 1M tokens
   * @param {string} model - Model name
   * @returns {object} Cost breakdown
   */
  getCostPer1M(model) {
    const pricing = this.pricing[model];
    if (!pricing) return null;

    return {
      input: pricing.input,
      output: pricing.output,
      average: (pricing.input + pricing.output) / 2
    };
  }

  /**
   * Compare costs across providers for similar capability
   * @param {string} tier - 'fast', 'balanced', 'powerful'
   * @returns {array} Sorted list of models by cost
   */
  compareProviderCosts(tier) {
    const tiers = {
      fast: ['gpt-4o-mini', 'claude-3-haiku-20240307', 'gemini-2.5-flash'],
      balanced: ['gpt-4o', 'claude-3-5-sonnet-20241022', 'gemini-1.5-pro'],
      powerful: ['gpt-4', 'claude-3-opus-20240229', 'grok-beta']
    };

    const models = tiers[tier] || [];
    const costs = models.map(model => ({
      model,
      ...this.pricing[model],
      avgCost: (this.pricing[model].input + this.pricing[model].output) / 2
    }));

    return costs.sort((a, b) => a.avgCost - b.avgCost);
  }
}

module.exports = PricingManager;
