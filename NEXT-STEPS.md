# LLM Proxy - Next Steps & Enhancements

## ✅ Current Status (Completed)

1. **Core Proxy Functionality**
   - Multi-provider support (Anthropic + Google Gemini)
   - Automatic failover with priority ordering
   - SSE streaming for Claude Code CLI
   - Request translation for Google Gemini
   - Statistics tracking per provider

2. **Deployment**
   - Deployed on TMRwww01 (192.168.18.11)
   - Public access: https://www.voipguru.org/llmProxy/
   - Docker containerized with auto-restart
   - Nginx reverse proxy configured

3. **Basic Web UI**
   - Provider status display
   - Enable/disable toggles
   - Statistics dashboard
   - Basic configuration

## 🚧 Enhancements Needed

### 1. Client API Key Management (HIGH PRIORITY)

**Why**: Other apps need to authenticate with the proxy without exposing provider API keys.

**Implementation**:
- Generate client API keys (format: `llm-proxy-<random>`)
- Validate incoming requests against client keys
- Track usage per client key
- Web UI to create/view/revoke client keys
- Per-key statistics and quotas

**Backend Changes**:
```javascript
// Add to config
config.clientApiKeys = [
  {
    id: 'key-1',
    key: 'llm-proxy-abc123...',
    name: 'My Python App',
    created: '2026-03-26T...',
    lastUsed: '2026-03-26T...',
    requests: 100,
    enabled: true
  }
];

// Add middleware
function validateApiKey(req, res, next) {
  const apiKey = req.headers['x-api-key'] ||
                 req.headers.authorization?.replace('Bearer ', '');

  const clientKey = config.clientApiKeys.find(k => k.key === apiKey && k.enabled);
  if (!clientKey) {
    return res.status(401).json({ error: 'Invalid API key' });
  }

  req.clientKey = clientKey;
  next();
}

// Apply to /v1/messages endpoint
app.post('/v1/messages', validateApiKey, async (req, res) => {
  // existing code...
  // Track usage: req.clientKey.requests++
});
```

**Web UI Changes**:
- New "API Keys" tab
- "Generate New Key" button
- List of keys with usage stats
- Revoke/Enable/Disable buttons
- Copy key to clipboard

### 2. Enhanced Web UI (CURRENT WORK)

**Created**: `public/index-enhanced.html`

**Features**:
- Drag-and-drop reordering of providers
- Edit provider settings (name, API key, priority)
- Add new providers
- Delete providers
- Test individual providers
- Settings modal with Claude CLI config
- Export configuration

**To Deploy**:
```bash
cd ~/llm-proxy
mv public/index.html public/index-basic.html
mv public/index-enhanced.html public/index.html
./update-streaming.sh
```

### 3. Provider Testing Endpoint

**Add to server**:
```javascript
app.post('/api/test-provider', async (req, res) => {
  const { type, apiKey, projectId } = req.body;
  const startTime = Date.now();

  try {
    if (type === 'anthropic') {
      await axios.post('https://api.anthropic.com/v1/messages', {
        model: 'claude-sonnet-4-5-20250929',
        max_tokens: 10,
        messages: [{ role: 'user', content: 'Hi' }]
      }, {
        headers: {
          'x-api-key': apiKey,
          'anthropic-version': '2023-06-01'
        }
      });
    } else if (type === 'google') {
      await axios.post(
        `https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-pro:generateContent?key=${apiKey}`,
        { contents: [{ parts: [{ text: 'Hi' }] }] }
      );
    }

    res.json({
      success: true,
      latency: Date.now() - startTime,
      response: 'Provider responded successfully'
    });
  } catch (error) {
    res.json({
      success: false,
      error: error.response?.data?.error?.message || error.message
    });
  }
});
```

### 4. API Key Validation Updates

**Current**: Provider API keys stored in docker-compose.yml env vars
**Needed**: Support updating API keys via Web UI

**Options**:
a) Keep in env vars (restart required) - ✅ Current, simple, secure
b) Store in config file (no restart) - More flexible but less secure
c) Hybrid: Env vars as defaults, allow overrides in config

**Recommendation**: Keep current approach (env vars) for now. Add note in Web UI that API key changes require container restart.

### 5. Usage Quotas & Rate Limiting

**Future Enhancement**: Per-client-key rate limiting

```javascript
// In validateApiKey middleware
if (clientKey.rateLimit) {
  const now = Date.now();
  const windowStart = now - 60000; // 1 minute
  const recentRequests = clientKey.requestTimestamps.filter(t => t > windowStart);

  if (recentRequests.length >= clientKey.rateLimit.requestsPerMinute) {
    return res.status(429).json({ error: 'Rate limit exceeded' });
  }
}
```

### 6. Cost Tracking

**Add to config**:
```javascript
config.providerCosts = {
  'anthropic-claude-3': {
    inputCostPer1MTok: 3.00,
    outputCostPer1MTok: 15.00
  },
  'google-gemini-1': {
    inputCostPer1MTok: 0.35,
    outputCostPer1MTok: 1.05
  }
};
```

**Track in stats**:
```javascript
stats[providerId].totalInputTokens += usage.input_tokens;
stats[providerId].totalOutputTokens += usage.output_tokens;
stats[providerId].estimatedCost = calculateCost(stats[providerId]);
```

**Show in Web UI**: Cost per provider, cost per client key

## 📋 Implementation Priority

1. **IMMEDIATE** (This Session):
   - [x] Create enhanced Web UI with full settings
   - [ ] Add client API key management backend
   - [ ] Deploy enhanced Web UI
   - [ ] Test client API key generation/validation

2. **NEXT** (Follow-up):
   - [ ] Per-key usage tracking
   - [ ] Provider test endpoint
   - [ ] Cost tracking
   - [ ] Rate limiting

3. **FUTURE**:
   - [ ] Usage analytics dashboard
   - [ ] Email alerts for failures
   - [ ] Webhook notifications
   - [ ] Multi-user admin access

## 🎯 Quick Wins

**To get basic client API key support working now**:

1. Add to server.js (before app.listen):
```javascript
// Simple API key validation
const MASTER_API_KEY = process.env.MASTER_API_KEY || 'llm-proxy-master-key-change-me';

function validateApiKey(req, res, next) {
  const apiKey = req.headers['x-api-key'] ||
                 req.headers.authorization?.replace('Bearer ', '');

  if (!apiKey || apiKey !== MASTER_API_KEY) {
    return res.status(401).json({ error: 'Invalid API key' });
  }
  next();
}

// Apply to main endpoint (exempt health, stats, UI)
app.post('/v1/messages', validateApiKey, async (req, res) => {
  // existing code
});
```

2. Add to docker-compose.yml:
```yaml
environment:
  - MASTER_API_KEY=llm-proxy-YOUR-SECURE-KEY-HERE
```

3. Document the key:
```
Client API Key: llm-proxy-YOUR-SECURE-KEY-HERE
Use in x-api-key header or Authorization: Bearer header
```

This gives you immediate protection while we build the full key management system.

## 📝 Notes

- Enhanced Web UI is ready in `public/index-enhanced.html`
- Backend needs client API key management added
- Current setup works but is open (no auth required)
- Adding simple master key is quickest solution
- Full key management system is better long-term solution

## 🚀 Deployment Commands

```bash
# Deploy enhanced Web UI
cd ~/llm-proxy
./update-streaming.sh

# Add master key to docker-compose.yml
ssh dblagbro@192.168.18.11
nano /opt/llm-proxy/docker-compose.yml
# Add MASTER_API_KEY line
docker-compose -f /opt/llm-proxy/docker-compose.yml restart

# Test
curl -H "x-api-key: YOUR-KEY" https://www.voipguru.org/llmProxy/health
```
