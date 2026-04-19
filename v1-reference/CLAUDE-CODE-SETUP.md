# Using LLM Proxy Manager with Claude Code

This guide explains how to configure Claude Code (and other Claude sessions) to use your LLM Proxy Manager instead of direct Anthropic API access.

## Prerequisites

- LLM Proxy Manager deployed and running
- At least one Anthropic provider configured in the proxy
- API key generated in the proxy's Web UI

## Quick Setup

### Step 1: Generate Proxy API Key

1. Access your proxy's Web UI: `http://your-proxy-server:3000`
2. Login with admin credentials
3. Navigate to "API Keys" tab
4. Click "Generate New API Key"
5. Enter a name like "Claude Code - Workstation"
6. **Select key type "Claude Code"** — enables reasoning/thinking augmentation when routed to non-Anthropic backends (Gemini, Grok, OpenAI). Use "Standard" for plain pass-through.
7. **Copy the generated key immediately** (you won't see it again)

### Step 2: Configure Claude Code

Claude Code respects Anthropic's environment variables. Set these in your shell configuration:

**For bash/zsh** (`~/.bashrc` or `~/.zshrc`):
```bash
# LLM Proxy Configuration
export ANTHROPIC_BASE_URL="http://your-proxy-server:3000"
export ANTHROPIC_API_KEY="llm-proxy-your-generated-key-here"
```

**For Windows PowerShell** (`$PROFILE`):
```powershell
$env:ANTHROPIC_BASE_URL = "http://your-proxy-server:3000"
$env:ANTHROPIC_API_KEY = "llm-proxy-your-generated-key-here"
```

### Step 3: Reload Shell and Test

```bash
# Reload shell configuration
source ~/.bashrc  # or source ~/.zshrc

# Test with Claude Code
claude --version

# Start a new Claude session (should now use proxy)
claude
```

## Cluster Setup (Recommended)

For production use, deploy multiple proxy instances for redundancy:

### Architecture

```
┌──────────────────────────────────────────────────┐
│              Your Applications                    │
│            (Claude Code, Scripts, etc.)           │
└────────────┬──────────────┬──────────────────────┘
             │              │
      Primary│       Backup │
             ▼              ▼
       ┌─────────┐    ┌─────────┐
       │ Proxy 1 │    │ Proxy 2 │
       │TMRwww01 │    │TMRwww02 │
       └────┬────┘    └────┬────┘
            │              │
       ┌────▼──────────────▼────┐
       │    Cluster Sync         │
       │  (Config, Users, Keys)  │
       └─────────────────────────┘
```

### Multi-Proxy Configuration

**Option A: Client-Side Failover** (Recommended for Claude Code)

Create a wrapper script that tries proxies in order:

**`~/bin/claude-with-proxy`**:
```bash
#!/bin/bash

# List of proxy URLs in priority order
PROXIES=(
    "http://tmrwww01:3000"
    "http://tmrwww02:3000"
    "http://c1conversations-avaya-01:3000"
)

# Your proxy API key
PROXY_KEY="llm-proxy-your-key-here"

# Try each proxy until one works
for PROXY_URL in "${PROXIES[@]}"; do
    # Check if proxy is healthy
    if curl -sf "${PROXY_URL}/health" > /dev/null 2>&1; then
        export ANTHROPIC_BASE_URL="${PROXY_URL}"
        export ANTHROPIC_API_KEY="${PROXY_KEY}"
        echo "✓ Using proxy: ${PROXY_URL}"
        exec claude "$@"
    fi
done

echo "✗ All proxies unavailable, falling back to direct API"
unset ANTHROPIC_BASE_URL
exec claude "$@"
```

Make it executable:
```bash
chmod +x ~/bin/claude-with-proxy
```

Update your shell config to use the wrapper:
```bash
alias claude='claude-with-proxy'
```

**Option B: Load Balancer** (For production environments)

Use HAProxy, nginx, or another load balancer:

**nginx config** (`/etc/nginx/sites-available/llm-proxy`):
```nginx
upstream llm_proxies {
    server tmrwww01:3000 max_fails=3 fail_timeout=30s;
    server tmrwww02:3000 max_fails=3 fail_timeout=30s backup;
    server c1conversations-avaya-01:3000 max_fails=3 fail_timeout=30s backup;
}

server {
    listen 80;
    server_name llm-proxy.internal;

    location / {
        proxy_pass http://llm_proxies;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # SSE streaming support
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
    }

    location /health {
        proxy_pass http://llm_proxies/health;
        proxy_connect_timeout 5s;
        proxy_read_timeout 5s;
    }
}
```

Then configure Claude Code to use the load balancer:
```bash
export ANTHROPIC_BASE_URL="http://llm-proxy.internal"
export ANTHROPIC_API_KEY="llm-proxy-your-key-here"
```

## Usage with Other Applications

### Python (Anthropic SDK)

```python
import os
from anthropic import Anthropic

# Configure to use proxy
client = Anthropic(
    api_key=os.environ.get("ANTHROPIC_API_KEY", "llm-proxy-your-key"),
    base_url=os.environ.get("ANTHROPIC_BASE_URL", "http://your-proxy:3000")
)

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
print(message.content)
```

### JavaScript/TypeScript

```javascript
import Anthropic from '@anthropic-ai/sdk';

const client = new Anthropic({
    apiKey: process.env.ANTHROPIC_API_KEY || 'llm-proxy-your-key',
    baseURL: process.env.ANTHROPIC_BASE_URL || 'http://your-proxy:3000'
});

const message = await client.messages.create({
    model: 'claude-sonnet-4-5-20250929',
    max_tokens: 1024,
    messages: [{ role: 'user', content: 'Hello!' }]
});
console.log(message.content);
```

### curl

```bash
curl -X POST http://your-proxy:3000/v1/messages \
  -H "x-api-key: llm-proxy-your-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

## Monitoring Your Usage

### Web UI Dashboard

Access the proxy's Web UI to monitor:
- Active providers and their status
- Circuit breaker states
- Recent activity log
- API key usage statistics

### API Endpoints

**Check cluster status**:
```bash
curl http://your-proxy:3000/cluster/status \
  -H "x-api-key: your-proxy-key"
```

**Check provider health**:
```bash
curl http://your-proxy:3000/health
```

## Cost Savings

### How It Saves Money

1. **Automatic Failover**: If your primary provider (e.g., Claude) is rate-limited, automatically fails over to cheaper alternatives (e.g., Gemini)

2. **Load Distribution**: Spread requests across multiple API keys to avoid hitting individual key rate limits

3. **Circuit Breaker**: Prevents wasting API calls on providers that are currently failing

4. **Centralized Monitoring**: Track all API usage in one place to optimize costs

### Provider Cost Comparison

Configure providers in order of cost-effectiveness:

**Example Priority Setup**:
```
Priority 1: Google Gemini 2.5 Flash ($0.075/$0.30 per 1M tokens)
Priority 2: Anthropic Claude Sonnet ($3/$15 per 1M tokens)
Priority 3: OpenAI GPT-4 ($10/$30 per 1M tokens)
Priority 4: Anthropic Claude Opus ($15/$75 per 1M tokens)
```

Most requests use Gemini (cheapest), failover to Claude/GPT-4 only when needed.

## Security Considerations

### Network Security

**Firewall Rules**:
```bash
# Allow proxy access only from trusted IPs
iptables -A INPUT -p tcp --dport 3000 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 3000 -j DROP
```

### HTTPS/TLS

For production, deploy behind a reverse proxy with TLS:

**Caddy** (automatic HTTPS):
```caddyfile
llm-proxy.yourdomain.com {
    reverse_proxy localhost:3000
}
```

**Certbot + nginx**:
```bash
certbot --nginx -d llm-proxy.yourdomain.com
```

### API Key Rotation

Regularly rotate proxy API keys:
1. Generate new key in Web UI
2. Update client configurations
3. Delete old key after grace period

## Troubleshooting

### Connection Refused

```bash
# Check if proxy is running
curl http://your-proxy:3000/health

# Check firewall
telnet your-proxy 3000

# Check proxy logs
docker logs llm-proxy
```

### Authentication Failed

```bash
# Verify API key
curl http://your-proxy:3000/v1/messages \
  -H "x-api-key: your-key" \
  -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4-5-20250929","max_tokens":10,"messages":[{"role":"user","content":"test"}]}'
```

### All Providers Failing

1. Check Web UI activity log
2. Verify provider API keys are valid
3. Check circuit breaker states
4. Test providers individually in UI

### Cluster Sync Issues

```bash
# Check cluster status
curl http://proxy1:3000/cluster/status \
  -H "x-api-key: your-key"

# Check network connectivity between nodes
ping proxy2
telnet proxy2 3000
```

## Best Practices

### For Development

- Use lower-cost providers (Gemini) for development
- Reserve expensive providers (Claude Opus) for production
- Set up separate API keys for dev/prod environments

### For Production

- Deploy 3+ proxy instances for redundancy
- Use load balancer for automatic failover
- Enable email alerts for critical failures
- Monitor circuit breaker states daily
- Set up provider priority based on your workload

### For Cost Optimization

1. **Start cheap**: Put Gemini Flash as Priority 1
2. **Quality fallback**: Claude Sonnet as Priority 2
3. **Power reserve**: Claude Opus as Priority 3
4. **Monitor usage**: Check which provider is handling most requests
5. **Adjust priorities**: Based on your quality/cost needs

## Migration Strategy

### Phase 1: Parallel Running (Week 1)
- Deploy proxies but don't use them yet
- Add all your current provider API keys
- Test with curl/scripts (not Claude Code yet)
- Monitor for errors

### Phase 2: Limited Use (Week 2)
- Configure 1-2 less critical Claude sessions to use proxy
- Keep personal sessions on direct API
- Monitor for issues

### Phase 3: Full Migration (Week 3+)
- Once confident, migrate all sessions to proxy
- Keep direct API credentials as emergency backup
- Monitor cost savings

## Support

For issues or questions:
- Check activity log in Web UI
- Review proxy logs: `docker logs llm-proxy`
- Check GitHub Issues: [repository link]
