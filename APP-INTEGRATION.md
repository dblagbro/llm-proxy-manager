# LLM Proxy - App Integration Guide

## Where to Point Your Apps

### Public Endpoint
```
https://www.voipguru.org/llmProxy/v1/messages
```

### Authentication
Use a generated client API key in the `x-api-key` header OR use the standard `Authorization: Bearer <key>` header.

## Integration Examples

### Claude Code CLI
```bash
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"
export ANTHROPIC_API_KEY="llm-proxy-YOUR-CLIENT-KEY-HERE"
```

### Python (Anthropic SDK)
```python
import anthropic

client = anthropic.Anthropic(
    api_key="llm-proxy-YOUR-CLIENT-KEY-HERE",
    base_url="https://www.voipguru.org/llmProxy"
)

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[
        {"role": "user", "content": "Hello!"}
    ]
)
```

### cURL
```bash
curl -X POST https://www.voipguru.org/llmProxy/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: llm-proxy-YOUR-CLIENT-KEY-HERE" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Node.js (Axios)
```javascript
const axios = require('axios');

const response = await axios.post(
  'https://www.voipguru.org/llmProxy/v1/messages',
  {
    model: 'claude-sonnet-4-5-20250929',
    max_tokens: 1024,
    messages: [{ role: 'user', content: 'Hello' }]
  },
  {
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': 'llm-proxy-YOUR-CLIENT-KEY-HERE'
    }
  }
);
```

### JavaScript (Fetch)
```javascript
const response = await fetch('https://www.voipguru.org/llmProxy/v1/messages', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'x-api-key': 'llm-proxy-YOUR-CLIENT-KEY-HERE'
  },
  body: JSON.stringify({
    model: 'claude-sonnet-4-5-20250929',
    max_tokens: 1024,
    messages: [{ role: 'user', content: 'Hello' }]
  })
});

const data = await response.json();
```

## Generating Client API Keys

1. Go to https://www.voipguru.org/llmProxy/
2. Click "API Keys" tab
3. Click "Generate New API Key"
4. Give it a name (e.g., "My Python App")
5. Copy the generated key (starts with `llm-proxy-`)
6. Use this key in your app's configuration

## Features

- **Automatic Failover**: If one provider fails, automatically tries the next
- **SSE Streaming**: Full support for streaming responses
- **Request Translation**: Google Gemini responses translated to Anthropic format
- **Usage Tracking**: Track requests per client API key
- **Cost Monitoring**: See which apps are using which providers

## API Key Security

- Client API keys are prefixed with `llm-proxy-`
- Keys can be revoked at any time via the Web UI
- Usage is tracked per key
- Keys never expire unless revoked

## Rate Limits

The proxy respects underlying provider rate limits. If a provider is rate-limited, it automatically fails over to the next provider in priority order.

## Support

For issues or questions, check the Web UI at https://www.voipguru.org/llmProxy/ or review logs.
