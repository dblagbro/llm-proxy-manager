# LLM Proxy v1.1.0 Upgrade Implementation Plan

## Overview
Major feature upgrade adding cost tracking, streaming support for all providers, provider capabilities, circuit breaker UI, and model validation.

## Status: IN PROGRESS
- Started: 2026-03-27
- Target Completion: 2026-03-27
- Version: 1.0.6 → 1.1.0

---

## ✅ Phase 1: Cost Tracking (COMPLETED)

### 1.1 Pricing Module
- ✅ Created `/src/pricing.js` with comprehensive pricing database
- ✅ Added pricing for Anthropic, OpenAI, Google, Grok, Vertex, Ollama
- ✅ Added provider capabilities (streaming, vision, context windows)
- ✅ Added cost calculation methods
- ✅ Imported into server.js

### 1.2 Cost Integration into Server (IN PROGRESS)
**File**: `/src/server.js`

**Changes needed:**
1. Initialize pricing manager after logger (line ~35):
```javascript
const pricingManager = new PricingManager();
```

2. Update `initStats()` function (line ~125) to include cost tracking:
```javascript
function initStats(providerId) {
  if (!config.stats[providerId]) {
    config.stats[providerId] = {
      requests: 0,
      successes: 0,
      failures: 0,
      totalLatency: 0,
      totalCost: 0,           // ADD THIS
      totalInputTokens: 0,    // ADD THIS
      totalOutputTokens: 0,   // ADD THIS
      lastUsed: null,
      lastSuccess: null,
      lastError: null
    };
  }
}
```

3. Update success handler in `/v1/messages` endpoint (line ~683-705) to track costs:
```javascript
// After line 690 - config.stats[provider.id].lastSuccess = {...}
const model = req.body.model || 'claude-sonnet-4-5-20250929';
const usage = result.usage || {};
const cost = pricingManager.calculateCost(
  model,
  usage.input_tokens || 0,
  usage.output_tokens || 0
);

config.stats[provider.id].totalCost += cost;
config.stats[provider.id].totalInputTokens += (usage.input_tokens || 0);
config.stats[provider.id].totalOutputTokens += (usage.output_tokens || 0);
```

---

## 🔨 Phase 2: Streaming Support for Remaining Providers

### 2.1 OpenAI Streaming
**File**: `/src/server.js`

**Add after line 289 (after streamGemini function):**
```javascript
async function streamOpenAI(provider, request, res) {
  const response = await axios.post(
    'https://api.openai.com/v1/chat/completions',
    {
      model: request.model || 'gpt-4o-mini',
      messages: request.messages,
      max_tokens: request.max_tokens || 4096,
      temperature: request.temperature,
      top_p: request.top_p,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      responseType: 'stream',
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  // Pipe OpenAI SSE directly to client
  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}
```

**Update streaming switch in `/v1/messages` (line ~645-651):**
```javascript
if (isStreaming) {
  if (provider.type === 'anthropic') {
    await streamAnthropic(provider, req.body, res);
  } else if (provider.type === 'google') {
    await streamGemini(provider, req.body, res);
  } else if (provider.type === 'openai') {           // ADD THIS
    await streamOpenAI(provider, req.body, res);     // ADD THIS
  } else if (provider.type === 'grok') {             // ADD THIS
    await streamGrok(provider, req.body, res);       // ADD THIS
  } else if (provider.type === 'ollama') {           // ADD THIS
    await streamOllama(provider, req.body, res);     // ADD THIS
  } else if (provider.type === 'openai-compatible') { // ADD THIS
    await streamOpenAICompatible(provider, req.body, res); // ADD THIS
  }
}
```

### 2.2 Grok Streaming
**Add after streamOpenAI:**
```javascript
async function streamGrok(provider, request, res) {
  const response = await axios.post(
    'https://api.x.ai/v1/chat/completions',
    {
      model: request.model || 'grok-beta',
      messages: request.messages,
      max_tokens: request.max_tokens || 4096,
      temperature: request.temperature,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      responseType: 'stream',
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}
```

### 2.3 Ollama Streaming
**Add after streamGrok:**
```javascript
async function streamOllama(provider, request, res) {
  const baseUrl = provider.baseUrl || 'http://localhost:11434';
  const response = await axios.post(
    `${baseUrl}/api/chat`,
    {
      model: request.model || provider.model || 'llama2',
      messages: request.messages,
      stream: true
    },
    {
      headers: { 'Content-Type': 'application/json' },
      responseType: 'stream',
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}
```

### 2.4 OpenAI-Compatible Streaming
**Add after streamOllama:**
```javascript
async function streamOpenAICompatible(provider, request, res) {
  const baseUrl = provider.baseUrl || 'http://localhost:8080';
  const response = await axios.post(
    `${baseUrl}/v1/chat/completions`,
    {
      model: request.model || provider.model || 'gpt-3.5-turbo',
      messages: request.messages,
      max_tokens: request.max_tokens || 4096,
      temperature: request.temperature,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      responseType: 'stream',
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}
```

---

## 🎨 Phase 3: UI Enhancements

### 3.1 Add New API Endpoints
**File**: `/src/server.js`

**Add before line 757 (before /health endpoint):**

```javascript
// Get provider capabilities
app.get('/api/capabilities/:providerType', (req, res) => {
  const { providerType } = req.params;
  const capabilities = pricingManager.getCapabilities(providerType);
  res.json(capabilities);
});

// Get available models for provider
app.get('/api/models/:providerType', (req, res) => {
  const { providerType } = req.params;
  const models = pricingManager.getModelsForProvider(providerType);
  res.json({ models });
});

// Get pricing for model
app.get('/api/pricing/:model', (req, res) => {
  const { model } = req.params;
  const pricing = pricingManager.getPricing(model);

  if (!pricing) {
    return res.status(404).json({ error: 'Model not found' });
  }

  res.json({
    model,
    ...pricing,
    costPer1M: pricingManager.getCostPer1M(model)
  });
});

// Get circuit breaker status
app.get('/api/circuit-status', requireAuth, (req, res) => {
  const statuses = {};

  for (const provider of config.providers) {
    const circuit = providerMonitor.getCircuitState(provider.id);
    statuses[provider.id] = {
      providerName: provider.name,
      state: circuit.state,
      failures: circuit.failures,
      successes: circuit.successes,
      lastFailure: circuit.lastFailure,
      reason: circuit.reason
    };
  }

  res.json(statuses);
});
```

### 3.2 Update Stats Response to Include Costs
**File**: `/src/server.js`, line ~832

**Replace the /api/stats endpoint:**
```javascript
app.get('/api/stats', (req, res) => {
  // Add cost calculations to stats
  const enhancedStats = {};

  for (const [providerId, stats] of Object.entries(config.stats)) {
    const provider = config.providers.find(p => p.id === providerId);

    enhancedStats[providerId] = {
      ...stats,
      avgLatency: stats.requests > 0 ? stats.totalLatency / stats.requests : 0,
      successRate: stats.requests > 0 ? (stats.successes / stats.requests) * 100 : 0,
      avgCost: stats.requests > 0 ? stats.totalCost / stats.requests : 0,
      costPer1MTokens: provider ? pricingManager.getCostPer1M(provider.model) : null
    };
  }

  res.json(enhancedStats);
});
```

### 3.3 Update UI to Show Costs and Circuit Breaker
**File**: `/public/index.html`

**Find the `renderProviderCard()` function (around line 900-1000) and add:**

```javascript
// After success rate display, add cost info:
<div style="margin-top: 10px; padding: 10px; background: var(--bg2); border-radius: 4px;">
    <div><strong>Total Cost:</strong> $${stats.totalCost ? stats.totalCost.toFixed(4) : '0.0000'}</div>
    <div><strong>Avg Cost/Request:</strong> $${stats.avgCost ? stats.avgCost.toFixed(4) : '0.0000'}</div>
    <div><strong>Total Tokens:</strong> ${((stats.totalInputTokens || 0) + (stats.totalOutputTokens || 0)).toLocaleString()}</div>
</div>

// Add circuit breaker status:
<div id="circuit-${provider.id}" style="margin-top: 10px; padding: 8px; border-radius: 4px;"></div>
```

**Add function to load circuit breaker status:**
```javascript
async function loadCircuitStatus() {
    try {
        const response = await fetch('./api/circuit-status');
        const statuses = await response.json();

        for (const [providerId, status] of Object.entries(statuses)) {
            const circuitDiv = document.getElementById(`circuit-${providerId}`);
            if (!circuitDiv) continue;

            let color, text;
            if (status.state === 'CLOSED') {
                color = 'var(--success)';
                text = '● Circuit: CLOSED';
            } else if (status.state === 'HALF_OPEN') {
                color = 'var(--warning)';
                text = '◐ Circuit: HALF-OPEN (testing)';
            } else {
                color = 'var(--error)';
                text = `✖ Circuit: OPEN (blocked)`;
            }

            circuitDiv.innerHTML = `
                <div style="color: ${color}; font-weight: bold;">${text}</div>
                ${status.reason ? `<div style="color: var(--text-muted); font-size: 12px;">Reason: ${status.reason}</div>` : ''}
            `;
            circuitDiv.style.background = status.state === 'OPEN' ? 'var(--error-bg)' : 'transparent';
        }
    } catch (error) {
        console.error('Failed to load circuit status:', error);
    }
}

// Call in auto-refresh (add to existing interval around line 2087):
setInterval(() => {
    loadStats();
    loadClusterStatus();
    loadCircuitStatus();  // ADD THIS
    loadActivityLog();
}, 10000);
```

---

## 📋 Phase 4: Testing & Deployment

### 4.1 Update Version Number
**File**: `/public/index.html`, line 614
```html
<h1>🤖 LLM Proxy Manager <span style="font-size: 14px; color: var(--text-muted); font-weight: normal;">v1.1.0</span></h1>
```

### 4.2 Test Checklist
- [ ] Cost tracking works for all requests
- [ ] Streaming works for OpenAI
- [ ] Streaming works for Grok
- [ ] Streaming works for Ollama
- [ ] Streaming works for OpenAI-compatible
- [ ] Circuit breaker status displays correctly
- [ ] Model endpoint returns correct models
- [ ] Pricing endpoint returns correct prices
- [ ] Capabilities endpoint returns correct info
- [ ] UI shows costs per provider
- [ ] UI shows circuit breaker status

### 4.3 Build & Deploy
```bash
# Build Docker image
cd /home/dblagbro/llm-proxy
sudo docker build -t llm-proxy-manager:1.1.0 -t llm-proxy-manager:latest -t dblagbro/llm-proxy-manager:1.1.0 -t dblagbro/llm-proxy-manager:latest .

# Push to registry
sudo docker push dblagbro/llm-proxy-manager:1.1.0
sudo docker push dblagbro/llm-proxy-manager:latest

# Deploy to tmrwww01
sshpass -p 'Super*120120' ssh -o StrictHostKeyChecking=no dblagbro@tmrwww01 \
  "sudo docker stop llm-proxy && sudo docker rm llm-proxy && \
   sudo docker rmi -f dblagbro/llm-proxy-manager:latest && \
   sudo docker pull dblagbro/llm-proxy-manager:latest && \
   sudo docker run -d --name llm-proxy --restart unless-stopped --network docker_default -p 3100:3000 \
   -v /home/dblagbro/llm-proxy/config:/app/config \
   -v /home/dblagbro/llm-proxy/logs:/app/logs \
   -e PORT=3000 -e CLUSTER_ENABLED=true -e CLUSTER_NODE_ID=www1 \
   -e 'CLUSTER_NODE_NAME=LLM Proxy www1' \
   -e CLUSTER_NODE_URL=https://www.voipguru.org/llmProxy \
   -e CLUSTER_SYNC_SECRET=llm-cluster-sync-2026 \
   -e 'CLUSTER_PEERS=www2:https://www2.voipguru.org/llmProxy' \
   dblagbro/llm-proxy-manager:latest"

# Deploy to tmrwww02 (similar command with www2 vars)
```

---

## 🎯 Summary of Changes

### New Features:
1. ✅ Cost tracking per provider with token usage
2. 🔨 Streaming support for OpenAI, Grok, Ollama, OpenAI-compatible
3. 🔨 Provider capabilities endpoint
4. 🔨 Model listing endpoint
5. 🔨 Pricing information endpoint
6. 🔨 Circuit breaker status display in UI
7. 🔨 Cost display in provider statistics
8. 🔨 Token usage tracking

### Files Modified:
- `/src/server.js` - Main server with pricing integration and streaming
- `/public/index.html` - UI updates for costs and circuit breaker
- `/src/pricing.js` - NEW pricing and capabilities module

### Breaking Changes:
- None - all changes are additive

### Configuration Changes:
- None - existing config format unchanged
