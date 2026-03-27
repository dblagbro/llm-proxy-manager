# LLM Proxy Manager

A production-ready LLM API proxy with **multi-provider failover**, **web-based management**, and **automatic provider selection**. Route your AI requests through multiple LLM providers (Anthropic Claude, Google Gemini, OpenAI, Grok, and more) with automatic failover when one provider fails.

## Features

### Core Capabilities
- **Multi-Provider Support**: Anthropic Claude, Google Gemini, Google Vertex AI, OpenAI, Grok (xAI), Ollama, and OpenAI-compatible APIs
- **Automatic Failover**: Priority-based provider selection with automatic failover to backup providers
- **Server-Sent Events (SSE) Streaming**: Full support for streaming responses from Claude and Gemini
- **Unified API**: Single endpoint that works with multiple providers using Anthropic's API format

### Management & Security
- **Web-Based Dashboard**: Beautiful UI for managing providers, testing configurations, and monitoring activity
- **Authentication System**: Session-based authentication with bcrypt password hashing
- **User Management**: Create and manage multiple users with role-based access (admin/user)
- **API Key Management**: Generate API keys for external applications with usage tracking
- **Activity Logging**: Real-time activity log showing provider tests, logins, configuration changes

### Monitoring & Analytics
- **Provider Statistics**: Track requests, successes, failures, and latency per provider
- **Usage Tracking**: Monitor API key usage with request counts and timestamps
- **Health Checks**: Built-in health endpoint for monitoring and orchestration
- **Activity Dashboard**: Visual timeline of all system events with color-coded status

## Quick Start

### Docker (Recommended)

```bash
# Clone the repository
git clone https://github.com/yourusername/llm-proxy-manager.git
cd llm-proxy-manager

# Start the service
docker-compose up -d

# Access the web interface
open http://localhost:3100
```

**Default login**: `admin` / `admin` (change immediately in production!)

### Manual Installation

```bash
# Install dependencies
npm install

# Copy and configure environment
cp .env.example .env
# Edit .env with your settings

# Start the server
npm start

# For development with auto-reload
npm run dev
```

## Configuration

### Environment Variables

Create a `.env` file or set environment variables:

```bash
# Server
NODE_ENV=production
PORT=3000
SESSION_SECRET=your-random-secret-here

# Optional: Pre-configure API keys
ANTHROPIC_KEY_1=sk-ant-api03-...
GOOGLE_API_KEY_1=AIzaSy...
OPENAI_KEY_1=sk-...
```

### Provider Configuration

Providers can be configured in two ways:

1. **Web UI** (recommended): Navigate to the dashboard and use the "Add Provider" button
2. **Environment Variables**: Pre-configure providers using the `.env` file

## Supported Providers

| Provider | Type Value | Required Fields | Notes |
|----------|------------|----------------|-------|
| Anthropic Claude | `anthropic` | API Key | Uses claude-sonnet-4-5 by default |
| Google Gemini | `google` | API Key | Uses gemini-2.5-flash by default |
| Google Vertex AI | `vertex` | API Key, Project ID, Location | Requires OAuth 2.0 token |
| OpenAI | `openai` | API Key | Official OpenAI API |
| Grok (xAI) | `grok` | API Key | X.AI's Grok models |
| Ollama | `ollama` | Base URL, Model Name | For self-hosted models |
| OpenAI-Compatible | `openai-compatible` | Base URL, API Key | For 3rd party services |

## API Usage

### With Generated API Keys

```bash
# Generate an API key in the web UI first, then use it:
curl -X POST http://localhost:3100/v1/messages \
  -H "x-api-key: llm-proxy-your-generated-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Direct Access (for testing)

```bash
# Set your ANTHROPIC_API_KEY environment variable to "proxy-handled"
# and ANTHROPIC_BASE_URL to your proxy URL
export ANTHROPIC_BASE_URL="http://localhost:3100"
export ANTHROPIC_API_KEY="your-generated-proxy-key"

# Now use the Anthropic SDK normally
anthropic messages create \
  --model claude-sonnet-4-5-20250929 \
  --max-tokens 1024 \
  --messages '[{"role":"user","content":"Hello!"}]'
```

### Streaming

```bash
curl -X POST http://localhost:3100/v1/messages \
  -H "x-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Write a poem"}],
    "stream": true
  }'
```

## Architecture

```
┌─────────────────┐
│  Web Dashboard  │
│   (Port 3100)   │
└────────┬────────┘
         │
    ┌────▼─────┐
    │  Proxy   │
    │  Server  │
    └────┬─────┘
         │
    ┌────▼────────────────────────────┐
    │   Priority-Based Router          │
    │   (Automatic Failover)           │
    └────┬────────────────────────────┘
         │
    ┌────▼────────────────────────────┐
    │        Provider Pool             │
    ├──────────┬──────────┬───────────┤
    │ Anthropic│  Google  │  OpenAI   │
    │  Grok    │  Vertex  │  Ollama   │
    └──────────┴──────────┴───────────┘
```

## Deployment

### Production Considerations

1. **Change Default Credentials**: Update admin password immediately
2. **Set SESSION_SECRET**: Use a cryptographically random secret
3. **HTTPS**: Deploy behind a reverse proxy (nginx, Caddy, Traefik)
4. **Persistent Storage**: Ensure `./config` and `./logs` directories are backed up
5. **API Key Security**: Store provider API keys securely, rotate regularly

### Reverse Proxy (nginx)

```nginx
server {
    listen 443 ssl;
    server_name llm-proxy.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:3100;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;
    }
}
```

### Docker Compose (with nginx)

See `docker-compose.yml` for a complete example including:
- Persistent volumes for config and logs
- Health checks
- Network isolation
- Automatic restart

## Development

```bash
# Install dependencies
npm install

# Run tests
npm test

# Run Playwright UI mode for test debugging
npm run test:ui

# Start development server with auto-reload
npm run dev
```

## API Endpoints

### Public Endpoints

- `POST /v1/messages` - Main proxy endpoint (requires API key)
- `GET /health` - Health check

### Web UI Endpoints (require session authentication)

- `GET /` - Dashboard
- `POST /api/auth/login` - Login
- `POST /api/auth/logout` - Logout
- `GET /api/config` - Get configuration
- `POST /api/config` - Update configuration
- `POST /api/test-provider` - Test a provider
- `GET /api/activity-log` - Get activity log
- `GET /api/client-keys` - List API keys
- `POST /api/client-keys` - Generate new API key

## Troubleshooting

### Provider Tests Failing

1. Check API key is valid in provider settings
2. Verify API key has sufficient credits/quota
3. Check activity log for specific error messages
4. Test provider directly using test button in UI

### Activity Log Empty

- Activity log only shows events after the feature was added
- Perform some actions (test providers, save config, login) to generate entries

### Streaming Not Working

- Ensure you're setting `"stream": true` in the request
- Verify your client supports Server-Sent Events (SSE)
- Check nginx/reverse proxy is configured for SSE (see deployment section)

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Dependencies & Licenses

All dependencies use permissive licenses (MIT, Apache-2.0, BSD-2-Clause):
- express (MIT)
- axios (MIT)
- winston (MIT)
- bcrypt (MIT)
- @google/generative-ai (Apache-2.0)
- And others (see package.json)

## Support

- **Issues**: [GitHub Issues](https://github.com/yourusername/llm-proxy-manager/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/llm-proxy-manager/discussions)

---

**Built with ❤️ for the AI community**
