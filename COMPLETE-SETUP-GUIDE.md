# LLM Proxy - Complete Setup Guide

**Version**: 1.3.7 | **Updated**: April 2026

## ✅ What's Deployed and Working

### System Status
- **Node 1**: tmrwww01 (192.168.18.11) — https://www.voipguru.org/llmProxy/
- **Node 2**: tmrwww02 — https://www2.voipguru.org/llmProxy
- **Node 3**: GCP (34.170.189.19) — https://c1conversations-avaya-01.avaya.c1cx.com/llmProxy
- **Container**: `llm-proxy-manager` (Docker, `--restart unless-stopped`)
- **Image**: `dblagbro/llm-proxy-manager:1.3.7`
- **Port**: 3000 (all nodes)
- **Status**: ✅ OPERATIONAL

### Core Features
✅ **Client API Key Management** - Generate keys for apps
✅ **Multi-Provider Failover** - 3-pass routing with hold-down circuit breaker
✅ **SSE Streaming** - Full Claude Code CLI support
✅ **Request Translation** - Anthropic ↔ Gemini / OpenAI / Grok / Ollama
✅ **Capability Router** - Skips providers that can't handle tool calls, vision, or long context
✅ **XML Sentinel** - Detects bad model output, fails over automatically
✅ **Usage Tracking** - Per-key and per-provider stats with cost calculation
✅ **Per-Provider Chat Logs** - Human-readable logs viewable in Web UI (📋 Log button)
✅ **Provider Testing** - Test endpoints before using
✅ **Web Management UI** - Full control panel with session timeout, log viewer

---

## 🔑 API Key Management

### How It Works
1. **Generate API Keys** via Web UI or API
2. **Distribute Keys** to your applications
3. **Track Usage** per key in real-time
4. **Revoke/Disable** keys instantly
5. **Monitor** which apps are using which providers

### Generate Your First API Key

**Via Web UI** (Recommended):
1. Go to https://www.voipguru.org/llmProxy/
2. The providers list is shown by default
3. *(API Keys tab to be added - coming soon)*

**Via API**:
```bash
curl -X POST https://www.voipguru.org/llmProxy/api/client-keys \
  -H "Content-Type: application/json" \
  -d '{"name": "My Application Name"}'
```

Response:
```json
{
  "id": "key-1774568317739-5fc3d3ce",
  "key": "llm-proxy-8d9bfc208f9c2ca8012410a9bdb45abdd8d311f28c057426488a6e32da340c25",
  "name": "My Application Name",
  "created": "2026-03-26T23:38:37.739Z",
  "lastUsed": null,
  "requests": 0,
  "enabled": true
}
```

**⚠️ SAVE THE KEY!** It won't be shown again.

###  List All Keys
```bash
curl https://www.voipguru.org/llmProxy/api/client-keys
```

### Revoke a Key
```bash
curl -X DELETE https://www.voipguru.org/llmProxy/api/client-keys/KEY-ID
```

### Disable/Enable a Key
```bash
curl -X PATCH https://www.voipguru.org/llmProxy/api/client-keys/KEY-ID \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}'
```

---

## 🚀 Using the Proxy

### Endpoint
```
POST https://www.voipguru.org/llmProxy/v1/messages
```

### Authentication
Include your API key in **either** header:
- `x-api-key: llm-proxy-YOUR-KEY-HERE`
- `Authorization: Bearer llm-proxy-YOUR-KEY-HERE`

### Example Request
```bash
curl -X POST https://www.voipguru.org/llmProxy/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: llm-proxy-YOUR-KEY-HERE" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello!"}
    ]
  }'
```

---

## 🔧 Configure Your Apps

### Claude Code CLI
```bash
# Add to ~/.bashrc or ~/.zshrc:
export ANTHROPIC_BASE_URL="https://www.voipguru.org/llmProxy"
export ANTHROPIC_API_KEY="llm-proxy-YOUR-KEY-HERE"

# Reload shell
source ~/.bashrc

# Test
cc "hello world"
```

### Python (Anthropic SDK)
```python
import anthropic

client = anthropic.Anthropic(
    api_key="llm-proxy-YOUR-KEY-HERE",
    base_url="https://www.voipguru.org/llmProxy"
)

message = client.messages.create(
    model="claude-sonnet-4-5-20250929",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}]
)

print(message.content[0].text)
```

### JavaScript/Node.js
```javascript
const axios = require('axios');

const response = await axios.post(
  'https://www.voipguru.org/llmProxy/v1/messages',
  {
    model: 'claude-sonnet-4-5-20250929',
    max_tokens: 1024,
    messages: [{ role: 'user', content: 'Hello!' }]
  },
  {
    headers: {
      'Content-Type': 'application/json',
      'x-api-key': 'llm-proxy-YOUR-KEY-HERE'
    }
  }
);

console.log(response.data.content[0].text);
```

### cURL (Any Language)
```bash
curl -X POST https://www.voipguru.org/llmProxy/v1/messages \
  -H "Content-Type: application/json" \
  -H "x-api-key: llm-proxy-YOUR-KEY-HERE" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": false
  }'
```

---

## 📊 Configured Providers

| Priority | Name | Type | Status |
|----------|------|------|--------|
| 1 | Anthropic Claude Code #3 | Anthropic | ✅ Active |
| 2 | C1 Anthropic Claude | Anthropic | ✅ Active |
| 3 | Google Gemini API | Google | ⚠️ Needs model fix |
| 4 | C1 Vertex AI | Google | ⚠️ Needs model fix |

**Failover Order**: Proxy tries Priority 1 first, then 2, then 3, then 4.

### Known Issue: Google Gemini Model
The Google providers currently have a model name issue (`gemini-1.5-pro` vs `gemini-pro`). To use the proxy now, either:
1. Use only Anthropic providers (disable Google in Web UI)
2. Or fix will come in next update

---

## 🎛️ Web Management UI

**URL**: https://www.voipguru.org/llmProxy/

### Features:
- ✅ View all providers
- ✅ Enable/disable providers
- ✅ Drag-and-drop reordering (priority)
- ✅ Edit provider settings
- ✅ Test individual providers
- ✅ View real-time statistics
- ✅ Per-provider success rates
- ✅ Average latency tracking
- ⏳ API Keys tab (coming soon)

---

## 📈 Monitoring

### Health Check
```bash
curl https://www.voipguru.org/llmProxy/health
```

### View Statistics
```bash
curl https://www.voipguru.org/llmProxy/api/stats
```

### View API Keys
```bash
curl https://www.voipguru.org/llmProxy/api/client-keys
```

### Container Logs
```bash
ssh dblagbro@192.168.18.11
docker logs -f llm-proxy
```

---

## 🔐 Security

### API Key Format
- Prefix: `llm-proxy-`
- Length: 64 hex characters
- Example: `llm-proxy-8d9bfc208f9c2ca8012410a9bdb45abdd8d311f28c057426488a6e32da340c25`

### Protected Endpoints
- `/v1/messages` - Requires API key
- All other endpoints (health, config, stats, key management) - No auth required

### Best Practices
1. **One key per application** - Easier to track and revoke
2. **Descriptive names** - "Production API", "Dev Environment", etc.
3. **Rotate keys** - Delete old keys when apps are decomm'd
4. **Monitor usage** - Check stats regularly
5. **Disable unused keys** - Don't delete if you might reactivate

---

## 🛠️ Management Commands

### Restart Proxy
```bash
ssh dblagbro@192.168.18.11
docker-compose -f /opt/llm-proxy/docker-compose.yml restart
```

### View Logs
```bash
ssh dblagbro@192.168.18.11
docker logs -f llm-proxy
```

### Update Code
```bash
cd ~/llm-proxy
# Make changes to src/server.js or public/index.html
scp src/server.js dblagbro@192.168.18.11:/opt/llm-proxy/src/
ssh dblagbro@192.168.18.11 "docker-compose -f /opt/llm-proxy/docker-compose.yml restart"
```

### Backup Configuration
```bash
ssh dblagbro@192.168.18.11
cp /opt/llm-proxy/config/providers.json /opt/llm-proxy/config/providers.json.backup-$(date +%Y%m%d)
```

---

## 📝 Configuration Files

| File | Location | Purpose |
|------|----------|---------|
| `providers.json` | `/opt/llm-proxy/config/` | Provider settings, API keys, client keys, stats |
| `server.js` | `/opt/llm-proxy/src/` | Main proxy application |
| `index.html` | `/opt/llm-proxy/public/` | Web management UI |
| `docker-compose.yml` | `/opt/llm-proxy/` | Container configuration |
| `combined.log` | `/opt/llm-proxy/logs/` | All requests/responses |
| `error.log` | `/opt/llm-proxy/logs/` | Errors only |

---

## 🎯 Quick Start Checklist

- [x] 1. Proxy deployed and running
- [x] 2. Accessible at https://www.voipguru.org/llmProxy/
- [x] 3. API key management working
- [ ] 4. Generate your first API key
- [ ] 5. Configure Claude Code CLI
- [ ] 6. Test with a request
- [ ] 7. Monitor usage via Web UI
- [ ] 8. Add more applications as needed

---

## 🚧 Known Issues & Roadmap

### Known Issues
1. **Google Gemini model name** - Needs fixing to `gemini-1.5-flash` or similar
2. **API Keys tab in Web UI** - Planned but not yet implemented
3. **Provider stats not showing in UI** - Need to reload/refresh

### Planned Features
- [ ] API Keys management tab in Web UI
- [ ] Rate limiting per client key
- [ ] Cost tracking per provider
- [ ] Email alerts for failures
- [ ] Usage analytics dashboard
- [ ] Webhook notifications
- [ ] Multi-user admin access with roles

---

## 📞 Support

### Troubleshooting
1. **Check health**: `curl https://www.voipguru.org/llmProxy/health`
2. **View logs**: `docker logs llm-proxy`
3. **Test locally**: `curl http://localhost:3100/health` (on TMRwww01)
4. **Check nginx**: `docker logs nginx | grep llmProxy`

### Files
- **Full docs**: `~/llm-proxy/README.md`
- **App integration**: `~/llm-proxy/APP-INTEGRATION.md`
- **Next steps**: `~/llm-proxy/NEXT-STEPS.md`
- **This guide**: `~/llm-proxy/COMPLETE-SETUP-GUIDE.md`

---

## 🎉 Success Criteria

✅ **You're ready to use the proxy when:**
1. Health check returns `{"status":"ok"}`
2. You have a generated API key
3. Test request succeeds (even if providers need fixing)
4. Apps are configured to use the proxy endpoint

**Your LLM Proxy is operational and ready to use!**

---

**Generated**: 2026-03-26
**Version**: 1.0 with Client API Key Management
**Status**: Production Ready (with minor provider config needed)
