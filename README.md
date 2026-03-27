# LLM Proxy

**Version**: 1.1.0
**Last Updated**: March 27, 2026

Multi-provider LLM API proxy with automatic failover, streaming support, cost tracking, and web-based management.

## Features

- **Anthropic API Compatible**: Works seamlessly with Claude Code CLI
- **Multi-Provider Support**: Anthropic Claude, Google Gemini/Vertex AI, OpenAI, Grok, Ollama, OpenAI-compatible
- **Streaming Support**: Server-Sent Events (SSE) streaming for all providers
- **Automatic Failover**: Tries providers in priority order with circuit breaker protection
- **Cost Tracking**: Real-time token usage and cost calculation with visualization
- **Request Translation**: Converts Anthropic format to provider-specific formats
- **Web UI**: Real-time monitoring, cost tracking, circuit breaker status, and configuration
- **Statistics Tracking**: Per-provider request/success/failure stats with cost metrics
- **Circuit Breaker**: Automatic provider isolation on failures with visual status
- **Docker Ready**: Complete containerized deployment with cluster support

## Providers

The proxy supports these providers (in default priority order):

1. **Anthropic Claude Code #3** (Priority 1)
2. **C1 Anthropic Claude** (Priority 2)
3. **Google Gemini API** (Priority 3)
4. **C1 Vertex AI / Google AI** (Priority 4)

## Quick Start

### Using Docker Compose (Recommended)

```bash
# Start the service
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the service
docker-compose down
```

The service will be available at:
- API: http://localhost:3100/v1/messages
- Web UI: http://localhost:3100/

### Manual Installation

```bash
# Install dependencies
npm install

# Set environment variables (copy API keys)
export ANTHROPIC_KEY_3="your-key"
export ANTHROPIC_KEY_C1="your-key"
export GOOGLE_API_KEY_1="your-key"
export GOOGLE_API_KEY_VERTEX="your-key"
export GOOGLE_PROJECT_ID="c1-ai-center-of-excellence"

# Start server
npm start
```

## Using with Claude Code CLI

Configure Claude Code CLI to use the proxy:

```bash
# Set the API endpoint
export ANTHROPIC_API_KEY="dummy-key"  # Any value works, proxy handles routing
export ANTHROPIC_BASE_URL="http://localhost:3100"

# Or for remote proxy:
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"

# Then use Claude Code as normal
cc "hello world"
```

## Web Management UI

Access the web UI at `http://localhost:3100/` (or your nginx location) to:

- View real-time provider status
- Enable/disable providers
- Adjust failover priority
- Monitor request statistics
- View success/failure rates
- Check average latency per provider
- Reset statistics

## Streaming Support

All providers support Server-Sent Events (SSE) streaming for real-time token generation:

```bash
# Example streaming request
curl -X POST http://localhost:3100/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "messages": [{"role": "user", "content": "Count to 10"}],
    "stream": true
  }'
```

Streaming is automatically enabled when `stream: true` is set in the request body. The proxy handles streaming for:
- Anthropic Claude (all models)
- Google Gemini (all models)
- OpenAI (GPT-4, GPT-3.5-turbo, etc.)
- Grok (X.AI models)
- Ollama (local models)
- OpenAI-compatible APIs

## Cost Tracking

The proxy automatically tracks costs and token usage for all requests:

- **Real-time calculation** using current provider pricing
- **Per-provider metrics**: Total cost, average cost, input/output tokens
- **Visual display** in Web UI with auto-refresh
- **API access** via `/api/stats` endpoint

View cost metrics in the Web UI under each provider card, or query programmatically:

```bash
curl http://localhost:3100/api/stats
```

## Circuit Breaker

Automatic circuit breaker protection prevents cascading failures:

- **CLOSED** (Green): Provider healthy, accepting all requests
- **HALF_OPEN** (Yellow): Testing after failures, limited requests
- **OPEN** (Red): Provider blocked, requests routed to next provider

View circuit breaker status in the Web UI or via API:

```bash
curl http://localhost:3100/api/circuit-status
```

## API Endpoints

### POST /v1/messages
Main proxy endpoint (Anthropic Messages API compatible)

**Request**:
```json
{
  "model": "claude-sonnet-4-5-20250929",
  "max_tokens": 1024,
  "messages": [
    {"role": "user", "content": "Hello!"}
  ]
}
```

**Response**: Anthropic Messages API format

### GET /health
Health check endpoint

### GET /api/config
Get current configuration (API keys are masked)

### POST /api/config
Update configuration (enable/disable providers, change priorities)

### GET /api/stats
Get detailed statistics for all providers including cost metrics

### POST /api/stats/reset
Reset all statistics

### GET /api/capabilities/:providerType
Get capabilities for a specific provider type (e.g., `anthropic`, `openai`, `google`)

### GET /api/models/:providerType
Get available models for a specific provider type

### GET /api/pricing/:model
Get pricing information for a specific model (input/output costs per 1M tokens)

### GET /api/circuit-status
Get circuit breaker status for all providers (requires authentication)

## Configuration

The proxy stores configuration in `/app/config/providers.json` (persisted via Docker volume).

Default priority order:
1. Anthropic Claude Code #3
2. C1 Anthropic Claude
3. Google Gemini API
4. C1 Vertex AI

You can change priorities and enable/disable providers via the Web UI or by editing the config file.

## Failover Logic

1. Request comes in (Anthropic format)
2. Proxy tries first enabled provider (lowest priority number)
3. If successful, returns response immediately
4. If failed, automatically tries next provider
5. Continues until success or all providers exhausted
6. Logs and statistics updated for each attempt

For Google providers, the proxy automatically:
- Translates Anthropic request format to Gemini format
- Calls Google Gemini API
- Translates response back to Anthropic format
- Returns seamlessly to Claude Code CLI

## Statistics

Each provider tracks:
- Total requests
- Successes
- Failures
- Average latency
- Last used timestamp
- Last error message

Statistics persist across restarts and can be viewed/reset via the Web UI.

## Logs

Logs are stored in `/app/logs/`:
- `combined.log`: All requests and responses
- `error.log`: Errors only

View logs:
```bash
docker-compose logs -f
# Or
docker exec llm-proxy tail -f /app/logs/combined.log
```

## Nginx Configuration

Example nginx location block:

```nginx
location /llmProxy/ {
    proxy_pass http://localhost:3100/;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection 'upgrade';
    proxy_set_header Host $host;
    proxy_cache_bypass $http_upgrade;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Increase timeouts for LLM responses
    proxy_connect_timeout 300s;
    proxy_send_timeout 300s;
    proxy_read_timeout 300s;
}
```

## Security Notes

- API keys are stored in environment variables (not in code)
- Web UI shows masked API keys (only first 10 and last 4 characters)
- Configuration API doesn't accept API key changes (must be set via env vars)
- Run behind nginx with SSL/TLS in production
- Consider adding authentication to the Web UI for production use

## Troubleshooting

**Proxy not responding**:
```bash
docker-compose logs llm-proxy
curl http://localhost:3100/health
```

**All providers failing**:
- Check API keys in docker-compose.yml
- View logs for specific error messages
- Test provider APIs directly

**High latency**:
- Check provider stats in Web UI
- Consider adjusting priority order
- Disable slow/failing providers

## Architecture

```
┌─────────────────┐
│  Claude Code    │
│      CLI        │
└────────┬────────┘
         │ Anthropic API format
         ▼
┌─────────────────┐
│   LLM Proxy     │
│  (This Service) │
└────────┬────────┘
         │
    ┌────┴─────┬──────────┬─────────┐
    ▼          ▼          ▼         ▼
┌────────┐ ┌────────┐ ┌────────┐ ┌────────┐
│Anthro  │ │Anthro  │ │Google  │ │Google  │
│Claude#3│ │C1      │ │Gemini  │ │Vertex  │
└────────┘ └────────┘ └────────┘ └────────┘
 Priority    Priority   Priority   Priority
     1           2          3          4
```

## License

MIT
