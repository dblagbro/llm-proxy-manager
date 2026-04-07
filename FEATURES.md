# LLM Proxy Manager - Feature Overview

This document provides a comprehensive overview of all features in the LLM Proxy Manager.

## Table of Contents

1. [Claude Code Augmentation (v1.5.x)](#claude-code-augmentation-v15x)
2. [Core Proxy Features](#core-proxy-features)
3. [Intelligent Monitoring](#intelligent-monitoring)
4. [Cluster Mode](#cluster-mode)
5. [Email Notifications](#email-notifications)
6. [Web Dashboard](#web-dashboard)
7. [Authentication & Security](#authentication--security)

---

## Claude Code Augmentation (v1.5.x)

### API Key Types

When creating an API key in the web UI, two key types are available:

- **Claude Code** — Enables reasoning/thinking augmentation for non-Anthropic backends. Intended for use with the `cc` coordinator wrapper or Claude Code CLI.
- **Standard** — Plain pass-through. No augmentation. Use for direct API integrations.

### Augmentation Routing

The `getAugmentationMode()` function selects the augmentation strategy per request:

| Backend | Mode | Behavior |
|---------|------|----------|
| Anthropic | `passthrough` | No augmentation — Anthropic has native extended thinking |
| OpenAI o-series (`o1`, `o3`, `o4-mini`, etc.) | `native-o-series` | Adds `reasoning_effort: high` to the request |
| Gemini 2.5 models | `native-gemini-thinking` | Injects `thinkingConfig` budget; `thought:true` SSE parts emitted as `thinking` blocks |
| All others (Gemini non-2.5, Grok, other OpenAI, etc.) | `cot-pipeline` | Full CoT augmentation pipeline (see below) |

Only applies when the API key has `keyType = 'claude-code'`. Standard keys always use pass-through.

### Streaming CoT Pipeline

For backends that don't have native thinking support, the CoT pipeline synthesizes a thinking block:

1. **Pre-analysis call** (non-streaming, max 400 tokens): sends the user's last message with a meta-prompt asking the model to identify key considerations, constraints, and approach.
2. **Thinking block emitted** at SSE index 0 containing the pre-analysis result.
3. **Augmented main call** streamed from index 1 with reasoning injected into the system prompt as an `<augmented_reasoning>` block.

The client receives a standard Anthropic SSE stream with a thinking block followed by the main response — identical in structure to Claude's native extended thinking output.

### In-Memory Session Store

Pass an `X-Session-ID` header to accumulate context across turns:

- The proxy stores up to 3 prior pre-analysis results per session (30-minute TTL).
- On subsequent turns, prior analyses are injected into the pre-analysis prompt as context, enabling coherent multi-turn reasoning without a persistent backend.
- Sessions are per-process (not persisted to disk); resets on container restart.

### Gemini 2.5 Thinking State Machine

For Gemini 2.5 models with `thinkingConfig` enabled:

- SSE parts with `thought: true` trigger a `THINKING` state — emitted as a `thinking` content block.
- The first non-thinking part closes the thinking block and opens a `text` content block at the next index.
- Subsequent text parts continue the same text block.

---

## Core Proxy Features

### Multi-Provider Support

Support for 7+ LLM provider types:
- **Anthropic Claude** (claude-sonnet-4-5, claude-opus, etc.)
- **Google Gemini** (gemini-2.5-flash, gemini-pro, etc.)
- **Google Vertex AI** (Cloud-based Gemini)
- **OpenAI** (GPT-4, GPT-3.5-turbo, etc.)
- **Grok (xAI)** (grok-beta, grok-2, etc.)
- **Ollama** (Self-hosted local models)
- **OpenAI-Compatible APIs** (LM Studio, LocalAI, etc.)

### Automatic Failover

**Priority-Based Routing**: Providers are tried in order of priority (1 = highest).

**Intelligent Failover**:
- Automatically tries next provider if current one fails
- Circuit breaker prevents repeatedly trying failed providers
- External status monitoring preemptively disables degraded providers
- Configurable timeouts per provider type

**Example**:
```javascript
// Provider 1: Anthropic (Priority 1) - Tries first
// Provider 2: Google (Priority 2) - Fallback if Anthropic fails
// Provider 3: OpenAI (Priority 3) - Last resort
```

### Server-Sent Events (SSE) Streaming

Full support for streaming responses:
- Real-time token-by-token output
- Works with Anthropic and Google Gemini
- Compatible with Anthropic's SDK streaming format
- Automatic connection management and cleanup

**Usage**:
```javascript
const response = await fetch('/v1/messages', {
  method: 'POST',
  headers: {
    'x-api-key': 'your-key',
    'Content-Type': 'application/json'
  },
  body: JSON.stringify({
    model: 'claude-sonnet-4-5-20250929',
    max_tokens: 1024,
    messages: [{ role: 'user', content: 'Hello!' }],
    stream: true
  })
});

const reader = response.body.getReader();
// Process SSE events...
```

### Unified API Format

All providers use Anthropic's API format:
- Consistent request/response structure
- Automatic translation for non-Anthropic providers
- Drop-in replacement for Anthropic SDK

---

## Intelligent Monitoring

### Circuit Breaker Pattern

Automatically protects against cascading failures:

**States**:
- **CLOSED**: Normal operation, provider is healthy
- **OPEN**: Too many failures, provider temporarily disabled
- **HALF-OPEN**: Testing if provider has recovered

**Configuration**:
```env
CIRCUIT_BREAKER_THRESHOLD=3        # Failures before opening
CIRCUIT_BREAKER_TIMEOUT=60000      # Milliseconds to stay open
CIRCUIT_BREAKER_HALFOPEN=30000     # Test period duration
CIRCUIT_BREAKER_SUCCESS=2          # Successes needed to close
```

**Manual Control**:
- Reset circuit breaker via Web UI
- Force open circuit breaker for maintenance
- View circuit breaker status for all providers

### External Service Monitoring

Proactively monitors provider status pages:
- **Anthropic**: https://status.anthropic.com
- **OpenAI**: https://status.openai.com
- **Google Cloud**: https://status.cloud.google.com

**Features**:
- Checks every 5 minutes
- Automatically degrades providers during outages
- Alerts administrators of service issues
- Caches status to prevent API rate limits

### Billing Error Detection

Intelligently detects and handles billing/quota errors:

**Detected Patterns**:
- "insufficient credit"
- "quota exceeded"
- "billing issue"
- "payment required"
- "subscription expired"
- "rate limit exceeded"
- 429 status codes

**Actions**:
- Immediately opens circuit breaker
- Sends critical email alert
- Logs detailed error information
- Suggests remediation steps

### Configurable Timeouts

Per-provider timeout configuration:

```env
ANTHROPIC_TIMEOUT=30000     # 30 seconds
GOOGLE_TIMEOUT=30000        # 30 seconds
OPENAI_TIMEOUT=30000        # 30 seconds
GROK_TIMEOUT=30000          # 30 seconds
OLLAMA_TIMEOUT=60000        # 60 seconds (local models slower)
VERTEX_TIMEOUT=30000        # 30 seconds
COMPATIBLE_TIMEOUT=30000    # 30 seconds
```

**Benefits**:
- Prevents hung requests
- Faster failover to backup providers
- Customizable per provider type

---

## Cluster Mode

Deploy multiple proxy instances for high availability and load distribution.

### Architecture

```
┌─────────────────────────────────────┐
│      Client Applications            │
│  (Choose which proxy to use)        │
└──────┬──────────┬──────────┬────────┘
       │          │          │
       ▼          ▼          ▼
   ┌──────┐   ┌──────┐   ┌──────┐
   │Proxy1│   │Proxy2│   │Proxy3│
   └───┬──┘   └───┬──┘   └───┬──┘
       │          │          │
       └──────────┴──────────┘
         Cluster Sync
```

### Features

**Configuration Synchronization**:
- Users (admin accounts)
- API Keys (generated client keys)
- Activity Log (optional)

**Not Synchronized** (node-specific):
- Provider configurations
- Provider statistics
- Provider priority ordering

### Configuration

**Node 1** (Primary):
```env
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=proxy1
CLUSTER_NODE_NAME="Primary Proxy"
CLUSTER_NODE_URL=http://proxy1.example.com:3000
CLUSTER_SYNC_SECRET=shared-secret-here
CLUSTER_PEERS=proxy2:http://proxy2.example.com:3000,proxy3:http://proxy3.example.com:3000
```

**Node 2** (Secondary):
```env
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=proxy2
CLUSTER_NODE_NAME="Secondary Proxy"
CLUSTER_NODE_URL=http://proxy2.example.com:3000
CLUSTER_SYNC_SECRET=shared-secret-here
CLUSTER_PEERS=proxy1:http://proxy1.example.com:3000,proxy3:http://proxy3.example.com:3000
```

### Cluster Health Check

GET `/cluster/status` returns:
```json
{
  "clusterEnabled": true,
  "localNode": {
    "id": "proxy1",
    "status": "healthy",
    "providers": 3,
    "healthyProviders": 2
  },
  "peers": [
    {
      "id": "proxy2",
      "status": "healthy",
      "latency": 15,
      "providers": 3,
      "healthyProviders": 3
    }
  ],
  "totalNodes": 2,
  "healthyNodes": 2
}
```

### Redundancy-within-Redundancy

**Layer 1**: Application-level failover between proxies
**Layer 2**: Proxy-level failover between LLM providers

**Example**:
```javascript
const proxies = [
  'http://proxy1.example.com:3000',
  'http://proxy2.example.com:3000',
  'http://proxy3.example.com:3000'
];

// Try each proxy in order
for (const proxyUrl of proxies) {
  try {
    const response = await fetch(`${proxyUrl}/v1/messages`, ...);
    if (response.ok) return response;
  } catch (err) {
    continue; // Try next proxy
  }
}
```

---

## Email Notifications

### SMTP Configuration

```env
SMTP_ENABLED=true
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_SECURE=false
SMTP_USER=your-email@gmail.com
SMTP_PASS=your-app-password
SMTP_FROM=llm-proxy@example.com
SMTP_TO=admin@example.com
SMTP_MIN_SEVERITY=WARNING
ALERT_THROTTLE_MINUTES=15
```

### Alert Types

**Circuit Breaker Open** (ERROR):
- Provider repeatedly failing
- Circuit breaker opened
- Suggests checking API key and service status

**Billing/Quota Error** (CRITICAL):
- Insufficient credits
- Rate limit exceeded
- Subscription expired
- Immediate action required

**External Service Down** (WARNING):
- Provider status page reporting issues
- May impact all providers of that type
- Includes incident descriptions

**Cluster Node Down** (WARNING):
- Peer node not responding
- Lost heartbeat connection
- May indicate network or node issues

**All Providers Down** (CRITICAL):
- No providers available
- Proxy cannot serve requests
- Immediate attention required

### Alert Throttling

Prevents email storms:
- Only one alert per type per throttle window
- Default: 15 minutes
- Configurable via `ALERT_THROTTLE_MINUTES`

### Email Format

HTML-formatted emails with:
- Color-coded severity indicators
- Clear problem description
- Suggested remediation steps
- Timestamp and source information

---

## Web Dashboard

### Dark Mode

**Toggle**: Moon/sun icon in header
**Persistence**: Saved in browser localStorage
**Scope**: Per-browser preference

**Features**:
- Comprehensive dark theme
- Smooth transitions
- All UI elements styled
- Automatic initialization

### Provider Management

**Add Provider**:
1. Click "➕ Add Provider"
2. Select provider type
3. Enter API key and configuration
4. Set priority (lower = higher priority)
5. Enable/disable toggle

**Edit Provider**:
1. Click "✏️ Edit" on provider card
2. Modify settings
3. Save changes

**Test Provider**:
- Click "🧪 Test" to verify configuration
- Sends test request to provider
- Shows success/failure result
- Updates activity log

**Enable/Disable**:
- Toggle switch on provider card
- Changes persist across refreshes
- Disabled providers not used in failover

**Priority Ordering**:
- Drag and drop providers to reorder
- Lower number = higher priority
- Changes saved automatically

### User Management

**Add User**:
1. Click "➕ Add User"
2. Enter username and password
3. Select role (admin/user)
4. Save

**Change Password**:
1. Click "✏️ Edit" on user
2. Enter new password
3. Save

**Delete User**:
- Click "🗑️ Delete" on user
- Confirm deletion
- Cannot delete last admin user

### API Key Management

**Generate Key**:
1. Click "Generate New API Key"
2. Enter key name/description
3. Copy generated key (shown once)
4. Use in client applications

**View Keys**:
- List all generated keys
- See usage statistics
- Check last used timestamp

**Delete Key**:
- Click delete icon on key
- Confirm deletion
- Key immediately invalidated

### Activity Log

Real-time log of all system events:
- Provider test results
- Configuration changes
- User logins/logouts
- Circuit breaker events
- Cluster synchronization
- External service alerts

**Color Coding**:
- 🟢 Success (green)
- 🔴 Error (red)
- 🟡 Warning (yellow)
- 🔵 Info (blue)

---

## Authentication & Security

### Session-Based Authentication

- Secure session management
- HTTP-only cookies
- 24-hour session expiration
- Bcrypt password hashing

### API Key Authentication

- Generated keys for external applications
- Usage tracking per key
- Easy revocation
- No session required

### Security Best Practices

**Password Storage**:
- Bcrypt with salt rounds
- Never stored in plaintext
- Secure password hashing

**API Key Masking**:
- Keys masked in Web UI (•••••)
- Show/hide toggle for viewing
- Only shown once at generation

**Cluster Authentication**:
- HMAC-SHA256 signatures
- Shared secret between nodes
- Request payload verification

**Session Security**:
- HTTP-only cookies
- Secure flag (when HTTPS)
- CSRF protection

### Default Credentials

**⚠️ IMPORTANT**: Change immediately in production!

```
Username: admin
Password: admin
```

### Changing Default Password

1. Login with default credentials
2. Click "⚙️ Settings"
3. Go to "Users" tab
4. Edit admin user
5. Set new password
6. Save changes
