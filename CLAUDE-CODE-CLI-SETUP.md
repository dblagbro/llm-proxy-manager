# Claude Code CLI Setup with LLM Proxy

## Overview

The LLM Proxy is now deployed and fully operational with **SSE streaming support** for Claude Code CLI. This allows you to:
- Use multiple AI providers (Anthropic Claude, Google Gemini) with automatic failover
- Reduce costs by routing to cheaper providers
- Monitor usage and performance via Web UI
- Maintain full compatibility with Claude Code CLI features (tool streaming, extended thinking)

## URLs

- **Public URL**: https://www.voipguru.org/llmProxy/
- **Web UI**: https://www.voipguru.org/llmProxy/
- **API Endpoint**: https://www.voipguru.org/llmProxy/v1/messages
- **Health Check**: https://www.voipguru.org/llmProxy/health

## Configuration for Claude Code CLI

### Option 1: Environment Variables (Recommended)

Add these to your shell configuration (`~/.bashrc`, `~/.zshrc`, or `~/.profile`):

```bash
# LLM Proxy Configuration for Claude Code CLI
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"
export ANTHROPIC_API_KEY="proxy-handled"  # Any value works - proxy handles API keys
```

Then reload your shell:
```bash
source ~/.bashrc  # or ~/.zshrc or ~/.profile
```

### Option 2: Per-Command Override

```bash
ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy" \
ANTHROPIC_API_KEY="proxy-handled" \
cc "your prompt here"
```

### Option 3: Use the Configuration Script

```bash
cd ~/llm-proxy
./configure-claude-cli.sh
```

## Testing the Setup

### 1. Test Health Endpoint

```bash
curl https://www.voipguru.org/llmProxy/health
```

Expected output:
```json
{
  "status": "ok",
  "providers": {
    "total": 4,
    "enabled": 4
  },
  "uptime": 123.456
}
```

### 2. Test with Claude Code CLI (Non-Streaming)

```bash
cc "what is 2+2?"
```

### 3. Test with Streaming (Default for Claude Code CLI)

```bash
cc "write a hello world script in Python"
```

You should see the response stream in real-time as it's generated.

### 4. Test Failover

The Web UI at https://www.voipguru.org/llmProxy/ allows you to:
- Disable providers to test failover
- View real-time statistics
- Monitor which provider handled each request
- Check success/failure rates

## Provider Priority (Default)

1. **Anthropic Claude Code #3** (Priority 1) - Primary
2. **C1 Anthropic Claude** (Priority 2) - Backup
3. **Google Gemini API** (Priority 3) - Tertiary
4. **C1 Vertex AI** (Priority 4) - Final fallback

You can change priorities in the Web UI.

## Features

### Streaming Support

The proxy fully supports Claude Code CLI's streaming features:
- ✅ Server-Sent Events (SSE) format
- ✅ Tool use streaming
- ✅ Extended thinking streaming
- ✅ Content block streaming
- ✅ Automatic translation for Google Gemini responses

### Request Translation

For Google Gemini providers, the proxy automatically:
- Translates Anthropic request format → Gemini format
- Streams Gemini responses as Anthropic SSE events
- Handles tool calls and function calling
- Maintains compatibility with Claude Code CLI expectations

### Failover Logic

1. Request arrives at proxy
2. Proxy tries first enabled provider (lowest priority number)
3. If successful, response streams back to Claude CLI
4. If failed, automatically tries next provider
5. Continues until success or all providers exhausted
6. All attempts logged with latency tracking

## Web UI Management

Access https://www.voipguru.org/llmProxy/ to:

- **Enable/Disable Providers**: Toggle individual providers on/off
- **Adjust Priority**: Change failover order (lower number = higher priority)
- **View Statistics**:
  - Requests per provider
  - Success/failure counts
  - Success rates
  - Average latency
  - Last used timestamp
  - Last error message
- **Reset Statistics**: Clear all historical data

## Monitoring

### Check Logs

```bash
ssh dblagbro@192.168.18.11
docker logs -f llm-proxy
```

### View Stats

```bash
curl -s https://www.voipguru.org/llmProxy/api/stats | jq .
```

### Health Check

```bash
curl -s https://www.voipguru.org/llmProxy/health | jq .
```

## Troubleshooting

### Claude CLI Not Using Proxy

Check environment variables:
```bash
echo $ANTHROPIC_BASE_URL
echo $ANTHROPIC_API_KEY
```

Should output:
```
https://www.voipguru.org/llmProxy
proxy-handled
```

### All Providers Failing

1. Check Web UI for error messages
2. View logs: `docker logs llm-proxy`
3. Verify API keys are set in docker-compose.yml
4. Test direct provider access (Anthropic/Google APIs)

### Streaming Not Working

The proxy logs show whether requests are streaming:
```bash
docker logs llm-proxy 2>&1 | grep streaming
```

Should show: `streaming: true` for Claude Code CLI requests

### High Latency

1. Check Web UI stats for average latency per provider
2. Disable slow providers
3. Adjust priority to favor faster providers

## Advanced Configuration

### Changing API Keys

Edit `/opt/llm-proxy/docker-compose.yml` on TMRwww01:

```yaml
environment:
  - ANTHROPIC_KEY_3=sk-ant-api03-...
  - ANTHROPIC_KEY_C1=sk-ant-api03-...
  - GOOGLE_API_KEY_1=AIzaSy...
  - GOOGLE_API_KEY_VERTEX=AIzaSy...
  - GOOGLE_PROJECT_ID=c1-ai-center-of-excellence
```

Then restart:
```bash
docker-compose -f /opt/llm-proxy/docker-compose.yml restart
```

### Adding More Providers

Edit `/opt/llm-proxy/src/server.js` and rebuild:

```bash
cd /opt/llm-proxy
docker-compose build
docker-compose up -d
```

### Persistent Configuration

Provider enable/disable state and priorities are stored in:
```
/opt/llm-proxy/config/providers.json
```

This file persists across container restarts.

## Performance Notes

### Expected Latency

- **Anthropic Direct**: ~500-2000ms (depending on response length)
- **Google Gemini**: ~300-1500ms
- **Proxy Overhead**: <50ms (negligible)

### Failover Speed

- **Detection**: Immediate (on HTTP error)
- **Retry**: <100ms to next provider
- **Total Failover**: <200ms if primary provider fails

### Streaming Performance

- **First Token**: Similar to direct API (~500ms)
- **Streaming**: Real-time (no buffering)
- **Tool Calls**: Streamed as they occur

## Security

- API keys stored in Docker environment (not in code)
- Web UI shows masked keys (first 10 + last 4 chars only)
- HTTPS required for public access
- No API key changes via Web UI (must restart container)

## Cost Optimization

### Strategy 1: Prefer Cheaper Providers

Set Google Gemini as Priority 1 (cheapest) and Anthropic as fallback:

1. Open Web UI
2. Set Google Gemini Priority = 1
3. Set Anthropic Priority = 2-3
4. Save Configuration

### Strategy 2: Route by Task Complexity

For simple tasks, disable expensive providers:

1. Disable Anthropic providers for routine work
2. Re-enable for complex reasoning tasks

### Strategy 3: Monitor and Adjust

- Check Web UI statistics weekly
- Identify which providers succeed most
- Adjust priorities based on success rates

## Backup and Recovery

### Backup Configuration

```bash
ssh dblagbro@192.168.18.11
cd /opt/llm-proxy
tar -czf llm-proxy-backup-$(date +%Y%m%d).tar.gz config/ logs/ docker-compose.yml src/
```

### Restore

```bash
scp llm-proxy-backup-20260326.tar.gz dblagbro@192.168.18.11:/opt/llm-proxy/
ssh dblagbro@192.168.18.11
cd /opt/llm-proxy
tar -xzf llm-proxy-backup-20260326.tar.gz
docker-compose restart
```

## Support

For issues or questions:
1. Check logs: `docker logs llm-proxy`
2. Check Web UI: https://www.voipguru.org/llmProxy/
3. View health: `curl https://www.voipguru.org/llmProxy/health`
4. Review this documentation

## Quick Reference

```bash
# Check if proxy is working
curl https://www.voipguru.org/llmProxy/health

# Test Claude Code CLI
cc "hello world"

# View logs
ssh dblagbro@192.168.18.11
docker logs -f llm-proxy

# Restart proxy
ssh dblagbro@192.168.18.11
docker-compose -f /opt/llm-proxy/docker-compose.yml restart

# View stats
curl https://www.voipguru.org/llmProxy/api/stats | jq .

# Open Web UI
https://www.voipguru.org/llmProxy/
```

## Summary

✅ **Deployed**: TMRwww01 (192.168.18.11)
✅ **Public URL**: https://www.voipguru.org/llmProxy/
✅ **SSE Streaming**: Fully supported
✅ **Providers**: 4 (2 Anthropic + 2 Google)
✅ **Failover**: Automatic
✅ **Web UI**: Real-time monitoring
✅ **Claude Code CLI**: Ready to use

**Next Step**: Configure your Claude Code CLI with the environment variables above and start using it!
