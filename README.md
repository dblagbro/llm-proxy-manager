# LLM Proxy Manager

A production-ready LLM API proxy with **multi-provider failover**, **intelligent monitoring**, **cluster mode**, **LMRH semantic routing**, and **web-based management**. Route your AI requests through multiple LLM providers (Anthropic Claude, Google Gemini, OpenAI, Grok, and more) with automatic failover, semantic task-based routing, CoT auto-engagement, and capability advertisement.

![Version](https://img.shields.io/badge/version-1.13.1-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Node](https://img.shields.io/badge/node-%3E%3D20.0.0-brightgreen)

## ✨ Features

### Core Capabilities
- 🔄 **Multi-Provider Support**: Anthropic Claude, Google Gemini, Google Vertex AI, OpenAI, Grok (xAI), Ollama, and OpenAI-compatible APIs
- 🎯 **Intelligent Failover**: Priority-based provider selection with circuit breaker protection
- ⚡ **SSE Streaming**: Full support for real-time streaming responses
- 🔌 **Unified API**: Single endpoint compatible with Anthropic's SDK format
- 📊 **Provider Statistics**: Track requests, successes, failures, and latency

### Intelligent Monitoring
- 🔴 **Circuit Breaker Pattern**: Automatic provider isolation after repeated failures (CLOSED → OPEN → HALF-OPEN states)
- 🌐 **External Service Monitoring**: Checks status pages for Anthropic, OpenAI, and Google Cloud
- 💰 **Billing Error Detection**: Identifies quota/credit issues and alerts immediately
- ⏱️ **Configurable Timeouts**: Per-provider timeout settings to prevent hung requests
- 📈 **Health Tracking**: Real-time provider health status and performance metrics

### LMRH — LLM Model Routing Hint Protocol (v1.8.0+)
The first open-source implementation of [draft-blagbrough-lmrh-00](LMRH-PROTOCOL.md).

- 🗺️ **Semantic Routing**: Callers add `LLM-Hint:` request header to express task type, latency, cost, safety, region, context length, and modality preferences
- 🏆 **Capability Scoring**: Proxy scores all provider+model pairs against the hint using weighted affinity dimensions (task=10, safety=8, region=6, latency=4, cost=3, context=2, modality=5)
- 🚫 **Hard Constraints**: Append `;require` to any affinity to enforce it — non-matching providers return HTTP 503
- 📣 **Capability Advertisement**: `LLM-Capability:` response header reports what was actually used, including unmet soft preferences
- 🧠 **CoT Auto-Engage** (v1.10.0): `LLM-Hint: task=reasoning` on a model with `native_reasoning=false` automatically engages the CoT pipeline — `cot-engaged=?1` appears in the capability header
- 🔬 **Capability Profiles UI**: Per-provider, per-model capability editor with badge display; "Scan Models" auto-populates inferred profiles
- 🔒 **RFC 8941 Structured Fields**: Fully extensible — unknown affinities are soft-ignored; backward compatible per RFC 9110 §6.3

**Example:**
```bash
# Route to a reasoning-capable, cost-efficient model in the US:
curl -X POST http://proxy:3000/v1/messages \
  -H "LLM-Hint: task=reasoning, cost=economy, region=us" \
  -H "x-api-key: your-key" \
  -d '{"model": "claude-sonnet-4-6", "max_tokens": 1024, ...}'

# Response header shows routing decision:
# LLM-Capability: v=1, provider=google-gemini-api, model=gemini-2.5-flash, task=reasoning, safety=4, latency=medium, cost=economy, region=us
```

### Claude Code Augmentation (v1.5.x+)
- 🧠 **Claude Code Key Type**: Generate API keys with type `claude-code` to enable reasoning enhancements on non-Anthropic backends
- 💭 **Streaming CoT Pipeline**: Pre-analysis pass produces a thinking block (index 0) before the main streamed response — gives Gemini/OpenAI/Grok calls the same extended reasoning appearance as Claude's native thinking
- 🌀 **Gemini 2.5 Native Thinking**: Automatic state-machine passthrough for `thought:true` parts — emitted as proper `thinking` content blocks in the SSE stream
- 🧮 **OpenAI o-series Native Reasoning**: Routes o-series models through `reasoning_effort: high` instead of CoT pipeline
- 💾 **In-Memory Session Store**: Pass `X-Session-ID` header to enrich subsequent pre-analysis turns with prior conversation context (30-min TTL, max 3 prior analyses per session)
- 🔑 **Standard Key Type**: Non-augmented pass-through for existing integrations

### Provider Emulation Layer (v1.12.0+)

The proxy emulates full Anthropic/OpenAI capability sets toward all providers — so callers always see a consistent, feature-complete API regardless of which upstream model is selected.

- 🛠️ **PBTC — Prompt-Based Tool Calling** (v1.11.0+): For providers without native tool schemas (Ollama, OpenAI-compatible, some Google models), strips tool definitions, injects plain-English instructions into the system prompt, parses `<tool_call>` / `<tool_code>` / `<function_call>` / `<tool_use>` blocks in the response, and converts them back to proper `tool_use` content blocks before returning to the caller. Callers never need to know whether the upstream supports tools natively.
- 🔖 **PBTC Multi-Tag Support** (v1.12.1): Parser recognises all common tag variants — Gemini natively responds with `<tool_code>`, others may use `<function_call>` or `<tool_use>`. `findNextPbtcTag()` picks whichever format appears first so no raw XML leaks to the caller.
- 🧠 **PBRC — Prompt-Based Reasoning Chain** (v1.12.0): For providers that lack native extended thinking, injects `<thinking>…</thinking>` system prompt instructions and parses the model's introspection blocks back into `{type:"thinking"}` content blocks (and streaming `thinking_delta` events) — identical to Anthropic's native thinking format.
- 👁️ **Vision Stripping** (v1.12.0): Image content is automatically replaced with text placeholders when the selected provider has `vision: false` in its capability profile, preventing API errors on text-only models.
- 🔁 **Bidirectional Format Translation** (v1.12.0): `/v1/messages` ↔ `/v1/chat/completions` translation flows in both directions — Anthropic-format callers reach OpenAI-type providers, and OpenAI-format callers reach Anthropic providers. `reasoning_content` field is synthesised for thinking blocks to mirror the OpenAI o1 convention.
- ⚙️ **`applyProviderEmulation()`**: Unified middleware applying vision-strip → PBTC → PBRC in sequence on every request. Hard-exclude flag (`excludeFromToolRequests`) per provider bypasses PBTC entirely when a provider should be skipped for agentic sessions.

### Analytics & Monitoring
- 📊 **Cost Analytics Dashboard**: Per-provider cost tracking with charts and session breakdowns
- 🗃️ **Per-Provider Chat Log Viewer**: Browse all requests per provider with live stream replay
- 📋 **Session Management**: View and manage active and historical sessions
- 🔢 **Dynamic Version Display**: Live version shown in top bar, loaded from `/health` endpoint

### Cluster Mode (v1.13.0+)
- 🌍 **Multi-Instance Deployment**: Deploy to 3+ servers for high availability
- 🔄 **Full Configuration Sync**: Automatic synchronization of users, API keys, providers, provider enabled/disabled state, and LMRH model capability profiles across all nodes
- 🚫 **One-Way Disable Propagation**: Disabling a provider on any node propagates to all peers — a peer can never re-enable something disabled locally
- 🧠 **LMRH Profile Sync**: Model capability profiles stored in SQLite automatically sync to all cluster peers on startup and configuration push
- 💓 **Heartbeat Monitoring**: Continuous health checks between cluster members
- 🎛️ **Independent Provider Config**: Each node can have unique provider priorities
- 📡 **Cluster Status API**: Monitor entire cluster health from any node
- 🔐 **HMAC Authentication**: Secure cluster communication with shared secrets

### Email Notifications
- 📧 **SMTP Alerts**: Email notifications for critical failures and events
- 🚨 **Alert Types**: Circuit breaker opens, billing errors, service degradation, cluster issues
- ⏰ **Throttling**: Prevents email storms with configurable throttle windows
- 🎨 **HTML Emails**: Professional formatted alerts with severity indicators

### Web Dashboard
- 🌓 **Dark Mode**: Toggle-able dark theme with persistent user preference
- 🎯 **Provider Management**: Add, edit, test, enable/disable providers with drag-and-drop priority ordering
- 👥 **User Management**: Create and manage users with role-based access
- 🔑 **API Key Generation**: Generate secure API keys for external applications
- 📝 **Activity Log**: Real-time log of all system events with color-coded status
- 📊 **Statistics Dashboard**: Monitor provider performance and usage

### Security
- 🔐 **Session Authentication**: Secure session management with HTTP-only cookies
- 🔒 **Bcrypt Passwords**: Industry-standard password hashing
- 🎫 **API Key Auth**: Token-based authentication for programmatic access
- 🛡️ **Cluster HMAC**: Cryptographic signatures for inter-node communication
- 🔍 **Key Masking**: Automatic masking of sensitive API keys in UI

## 🚀 Quick Start

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

**Default login**: `admin` / `admin` (⚠️ change immediately in production!)

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

### Deployment Scripts (Production)

For production deployment to multiple servers:

```bash
# Primary node
./deploy-tmrwww01.sh

# Secondary nodes (requires cluster secret from primary)
./deploy-tmrwww02.sh
./deploy-c1conversations.sh
```

See [Deployment Guide](#deployment) for detailed instructions.

## ⚙️ Configuration

### Environment Variables

Create a `.env` file:

```bash
# Server Configuration
NODE_ENV=production
PORT=3000
SESSION_SECRET=your-random-secret-here

# Circuit Breaker
CIRCUIT_BREAKER_THRESHOLD=3           # Failures before opening circuit
CIRCUIT_BREAKER_TIMEOUT=60000         # Milliseconds to stay open
CIRCUIT_BREAKER_HALFOPEN=30000        # Test period duration
CIRCUIT_BREAKER_SUCCESS=2             # Successes needed to close

# Provider Timeouts (milliseconds)
ANTHROPIC_TIMEOUT=30000
GOOGLE_TIMEOUT=30000
OPENAI_TIMEOUT=30000
GROK_TIMEOUT=30000
OLLAMA_TIMEOUT=60000

# Cluster Mode (Optional)
CLUSTER_ENABLED=false
CLUSTER_NODE_ID=node1
CLUSTER_NODE_NAME="LLM Proxy Node 1"
CLUSTER_NODE_URL=http://localhost:3000
CLUSTER_SYNC_SECRET=shared-cluster-secret
CLUSTER_PEERS=node2:http://node2:3000,node3:http://node3:3000

# Email Notifications (Optional)
SMTP_ENABLED=false
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your-email@example.com
SMTP_PASS=your-app-password
SMTP_FROM=llm-proxy@example.com
SMTP_TO=admin@example.com
SMTP_MIN_SEVERITY=WARNING
ALERT_THROTTLE_MINUTES=15

# Provider API Keys (Optional - can configure via Web UI)
ANTHROPIC_KEY_1=sk-ant-api03-...
GOOGLE_API_KEY_1=AIzaSy...
OPENAI_KEY_1=sk-...
```

See [.env.example](.env.example) for complete configuration options.

### Provider Configuration

**Option 1: Web UI** (Recommended)
1. Navigate to the dashboard
2. Click "➕ Add Provider"
3. Configure provider details and API key
4. Set priority (lower number = higher priority)
5. Click Test to verify configuration

**Option 2: Environment Variables**
Pre-configure providers in `.env` file (see above).

## 🔌 Supported Providers

| Provider | Type Value | Required Fields | Cost (per 1M tokens) | Notes |
|----------|------------|----------------|---------------------|-------|
| **Anthropic Claude** | `anthropic` | API Key | $3-15 (Sonnet) | Best quality, streaming support |
| **Google Gemini** | `google` | API Key | $0.075-0.30 (Flash) | Most cost-effective |
| **Google Vertex AI** | `vertex` | API Key, Project ID, Location | Varies | OAuth 2.0 required |
| **OpenAI** | `openai` | API Key | $10-30 (GPT-4) | Widely compatible |
| **Grok (xAI)** | `grok` | API Key | TBD | X.AI's models |
| **Ollama** | `ollama` | Base URL, Model | Free | Self-hosted local models |
| **OpenAI-Compatible** | `openai-compatible` | Base URL, API Key | Varies | LM Studio, LocalAI, etc. |

## 📡 API Usage

### With Generated API Keys

```bash
curl -X POST http://localhost:3000/v1/messages \
  -H "x-api-key: llm-proxy-your-generated-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Streaming

```bash
curl -X POST http://localhost:3000/v1/messages \
  -H "x-api-key: your-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Write a poem"}],
    "stream": true
  }'
```

### With Anthropic SDK

```python
import os
from anthropic import Anthropic

client = Anthropic(
    api_key="llm-proxy-your-generated-key",
    base_url="http://localhost:3000"
)

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)
print(message.content)
```

### Claude Code Integration

Configure Claude Code to use the proxy:

```bash
export ANTHROPIC_BASE_URL="http://your-proxy:3000"
export ANTHROPIC_API_KEY="llm-proxy-your-generated-key"
```

See [CLAUDE-CODE-SETUP.md](CLAUDE-CODE-SETUP.md) for complete integration guide.

### Claude Code Key Type (Reasoning Augmentation)

When generating an API key in the web UI, choose key type **Claude Code** to enable reasoning enhancements for non-Anthropic backends:

- Requests routed to **Anthropic**: plain pass-through (Anthropic has native thinking)
- Requests routed to **Gemini 2.5**: native `thinkingConfig` budget injected; `thought:true` parts emitted as thinking blocks
- Requests routed to **OpenAI o-series**: `reasoning_effort: high` added natively
- Requests routed to **everything else** (Gemini non-2.5, Grok, OpenAI, etc.): CoT pipeline — a pre-analysis call produces a thinking block at SSE index 0, then the augmented main call streams from index 1

To use session memory across turns, pass a consistent `X-Session-ID` header:

```bash
curl -X POST http://proxy:3000/v1/messages \
  -H "x-api-key: your-claude-code-key" \
  -H "X-Session-ID: my-session-abc" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-4-6", "max_tokens": 2000, "stream": true, ...}'
```

## 🏗️ Architecture

### Standalone Mode

```
┌─────────────────┐
│  Applications   │
└────────┬────────┘
         │
    ┌────▼─────┐
    │  Proxy   │
    │  Server  │
    └────┬─────┘
         │
    ┌────▼────────────────────┐
    │   Circuit Breaker       │
    │ (Intelligent Failover)  │
    └────┬────────────────────┘
         │
    ┌────▼────────────────────┐
    │    Provider Pool         │
    ├──────────┬──────────────┤
    │ Priority │ Priority 2   │
    │    1     │ (Backup)     │
    │Anthropic │   Google     │
    └──────────┴──────────────┘
```

### Cluster Mode (3+ Nodes)

```
┌──────────────────────────────────────────┐
│          Client Applications              │
│  (Failover between proxy instances)       │
└────────┬──────────┬──────────┬───────────┘
         │          │          │
         ▼          ▼          ▼
    ┌────────┐ ┌────────┐ ┌────────┐
    │Proxy 1 │ │Proxy 2 │ │Proxy 3 │
    │TMRwww01│ │TMRwww02│ │C1-Hub  │
    └───┬────┘ └───┬────┘ └───┬────┘
        │          │          │
        └──────────┴──────────┘
         Cluster Sync (Config)
        │          │          │
        ▼          ▼          ▼
    Different provider priorities per node
```

**Redundancy-within-Redundancy**:
- **Layer 1**: Applications fail over between proxy instances
- **Layer 2**: Each proxy fails over between LLM providers

## 🚢 Deployment

### Single Node (Standalone)

```bash
npm install --production
cp .env.example .env
# Edit .env with your configuration
npm start
```

Access at `http://localhost:3000`

### Multi-Node Cluster

#### Step 1: Deploy Primary Node

```bash
cd /path/to/llm-proxy-manager
./deploy-tmrwww01.sh
```

**IMPORTANT**: Save the cluster secret that is displayed!

#### Step 2: Deploy Secondary Nodes

```bash
# On each secondary node
./deploy-tmrwww02.sh
# Enter the cluster secret from Step 1
```

#### Step 3: Configure Providers

On each node:
1. Access Web UI: `http://node-hostname:3000`
2. Login and change default password
3. Add provider API keys
4. Configure provider priorities (can differ per node)
5. Test providers

#### Step 4: Verify Cluster

Check cluster status:
```bash
curl http://node1:3000/cluster/status \
  -H "x-api-key: your-api-key" | jq
```

### Docker Deployment

```bash
# Build image
docker build -t llm-proxy-manager:latest .

# Run container
docker run -d \
  -p 3000:3000 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -e SESSION_SECRET=your-secret \
  -e CLUSTER_ENABLED=true \
  -e CLUSTER_NODE_ID=node1 \
  --name llm-proxy \
  llm-proxy-manager:latest
```

Or use Docker Compose:
```bash
docker-compose up -d
```

### Reverse Proxy (nginx)

```nginx
server {
    listen 443 ssl;
    server_name llm-proxy.example.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location / {
        proxy_pass http://localhost:3000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection 'upgrade';
        proxy_set_header Host $host;
        proxy_cache_bypass $http_upgrade;

        # SSE streaming support
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding off;
    }
}
```

## 📊 Monitoring

### Health Check Endpoint

```bash
curl http://localhost:3000/health
```

Response:
```json
{
  "status": "healthy",
  "uptime": 3600,
  "providers": {
    "enabled": 3,
    "healthy": 2
  }
}
```

### Cluster Status

```bash
curl http://localhost:3000/cluster/status \
  -H "x-api-key: your-api-key"
```

### Circuit Breaker Status

```bash
curl http://localhost:3000/monitoring/status \
  -H "Cookie: session-cookie"
```

Shows circuit breaker states for all providers.

### Activity Log

View real-time activity in the Web UI or via logs:

```bash
# Systemd
sudo journalctl -u llm-proxy -f

# Docker
docker logs -f llm-proxy

# Docker Compose
docker-compose logs -f
```

## 🔧 Troubleshooting

### Provider Tests Failing

1. Check API key is valid in provider settings
2. Verify API key has sufficient credits/quota
3. Check activity log for specific error messages
4. View circuit breaker state in monitoring dashboard
5. Test provider directly (outside proxy) to confirm API is working

### Circuit Breaker Stuck Open

Manual reset via Web UI:
1. Navigate to Settings → Monitoring
2. Find the provider with open circuit
3. Click "Reset Circuit Breaker"

Or via API:
```bash
curl -X POST http://localhost:3000/monitoring/circuit/reset \
  -H "Cookie: session-cookie" \
  -H "Content-Type: application/json" \
  -d '{"providerId": "provider-id"}'
```

### Cluster Sync Issues

1. Check network connectivity between nodes
2. Verify cluster secret matches on all nodes
3. Check firewall rules (port 3000)
4. Review cluster peer configuration in `.env`
5. Check activity log for sync errors

### Email Alerts Not Sending

1. Verify SMTP configuration in `.env`
2. Test SMTP connection: `npm run test:email`
3. Check SMTP credentials
4. Verify firewall allows outbound SMTP
5. Review logs for SMTP errors

## 📚 Documentation

- **[FEATURES.md](FEATURES.md)** - Complete feature documentation
- **[LMRH-PROTOCOL.md](LMRH-PROTOCOL.md)** - LMRH protocol RFC draft and implementation guide
- **[CLUSTER-ARCHITECTURE.md](CLUSTER-ARCHITECTURE.md)** - Cluster design and implementation
- **[CLAUDE-CODE-SETUP.md](CLAUDE-CODE-SETUP.md)** - Claude Code integration guide
- **[PUBLISHING.md](PUBLISHING.md)** - GitHub and Docker Hub publishing guide
- **[.env.example](.env.example)** - Complete configuration reference

## 💰 Cost Optimization

### Recommended Provider Priority Setup

**For Maximum Cost Savings**:
```
Priority 1: Google Gemini Flash ($0.075/$0.30 per 1M tokens)
Priority 2: Anthropic Claude Sonnet ($3/$15 per 1M tokens)
Priority 3: Anthropic Claude Opus ($15/$75 per 1M tokens)
```

Most requests use Gemini (cheapest), failover to Claude only when:
- Gemini is rate-limited
- Gemini circuit breaker opens
- Gemini external status shows issues

**Estimated Savings**: 90-95% cost reduction vs. using Claude Opus exclusively

### Multi-Provider Strategy

Spread API keys across nodes to maximize rate limits:
- **Node 1**: Gemini Key #1, Claude Key #1
- **Node 2**: Gemini Key #2, Claude Key #2
- **Node 3**: Gemini Key #3, Claude Key #3

Load distribution across nodes prevents any single key from hitting rate limits.

## 🛡️ Security Considerations

### Production Deployment

1. **Change Default Password**: Update admin password immediately
2. **Set SESSION_SECRET**: Use cryptographically random secret
3. **Enable HTTPS**: Deploy behind reverse proxy with TLS
4. **Persistent Storage**: Ensure `./config` and `./logs` are backed up
5. **Secure API Keys**: Store provider API keys securely, rotate regularly
6. **Cluster Security**: Use strong cluster secrets (32+ character random string)
7. **Email Security**: Use app-specific passwords, not primary email password

### Firewall Rules

```bash
# Allow only trusted IPs to access proxy
iptables -A INPUT -p tcp --dport 3000 -s 192.168.1.0/24 -j ACCEPT
iptables -A INPUT -p tcp --dport 3000 -j DROP

# Allow cluster communication between nodes
iptables -A INPUT -p tcp --dport 3000 -s <node2-ip> -j ACCEPT
iptables -A INPUT -p tcp --dport 3000 -s <node3-ip> -j ACCEPT
```

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 📦 Dependencies

All dependencies use permissive open-source licenses:

- **express** (MIT) - Web framework
- **axios** (MIT) - HTTP client
- **winston** (MIT) - Logging
- **bcrypt** (MIT) - Password hashing
- **nodemailer** (MIT) - Email notifications
- **@google/generative-ai** (Apache-2.0) - Gemini SDK
- And more (see [package.json](package.json))

## 🆘 Support

- **Documentation**: See `.md` files in repository root
- **Issues**: [GitHub Issues](https://github.com/yourusername/llm-proxy-manager/issues)
- **Discussions**: [GitHub Discussions](https://github.com/yourusername/llm-proxy-manager/discussions)

## 🌟 Star History

If you find this project useful, please consider giving it a star! ⭐

---

**Built with ❤️ for the AI community**
