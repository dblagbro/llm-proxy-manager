#!/bin/bash
# Test LLM Proxy Functionality

set -e

PROXY_URL="https://www.voipguru.org/llmProxy"

echo "==================================="
echo "LLM Proxy Test Suite"
echo "==================================="
echo "Proxy URL: $PROXY_URL"
echo ""

# Test 1: Health Check
echo "Test 1: Health Check"
echo "-----------------------------------"
HEALTH=$(curl -sk "$PROXY_URL/health")
echo "$HEALTH" | python3 -m json.tool
STATUS=$(echo "$HEALTH" | python3 -c "import sys, json; print(json.load(sys.stdin)['status'])")
if [ "$STATUS" = "ok" ]; then
    echo "✓ Health check passed"
else
    echo "✗ Health check failed"
    exit 1
fi
echo ""

# Test 2: API Config
echo "Test 2: API Configuration"
echo "-----------------------------------"
CONFIG=$(curl -sk "$PROXY_URL/api/config")
PROVIDER_COUNT=$(echo "$CONFIG" | python3 -c "import sys, json; print(len(json.load(sys.stdin)['providers']))")
echo "Providers configured: $PROVIDER_COUNT"
echo "$CONFIG" | python3 -c "import sys, json; data=json.load(sys.stdin); [print(f\"  - {p['name']} ({p['type']}): {'Enabled' if p['enabled'] else 'Disabled'}, Priority {p['priority']}\") for p in data['providers']]"
if [ "$PROVIDER_COUNT" -ge 2 ]; then
    echo "✓ Configuration loaded"
else
    echo "✗ Configuration incomplete"
    exit 1
fi
echo ""

# Test 3: Web UI
echo "Test 3: Web UI Access"
echo "-----------------------------------"
UI_RESPONSE=$(curl -sk "$PROXY_URL/" | head -20)
if echo "$UI_RESPONSE" | grep -q "LLM Proxy Manager"; then
    echo "✓ Web UI accessible"
else
    echo "✗ Web UI not accessible"
    exit 1
fi
echo ""

# Test 4: Non-Streaming Request
echo "Test 4: Non-Streaming API Request"
echo "-----------------------------------"
echo "Sending test request (this may take a few seconds)..."
RESPONSE=$(curl -sk "$PROXY_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 100,
    "messages": [
      {"role": "user", "content": "Say hello in exactly 5 words"}
    ],
    "stream": false
  }')

if echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data['content'][0]['text'])" 2>/dev/null; then
    echo "✓ Non-streaming request successful"
    echo "Response preview:"
    echo "$RESPONSE" | python3 -c "import sys, json; data=json.load(sys.stdin); print('  ', data['content'][0]['text'][:100])"
else
    echo "✗ Non-streaming request failed"
    echo "$RESPONSE"
    exit 1
fi
echo ""

# Test 5: Streaming Request (partial test - just verify it starts)
echo "Test 5: Streaming API Request"
echo "-----------------------------------"
echo "Sending streaming test request (checking first chunk)..."
STREAM_START=$(timeout 5 curl -sk "$PROXY_URL/v1/messages" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{
    "model": "claude-sonnet-4-5-20250929",
    "max_tokens": 50,
    "messages": [
      {"role": "user", "content": "Say hi"}
    ],
    "stream": true
  }' | head -30)

if echo "$STREAM_START" | grep -q "event: message_start"; then
    echo "✓ Streaming request successful"
    echo "Stream events received:"
    echo "$STREAM_START" | grep "^event:" | head -5 | sed 's/^/  /'
else
    echo "✗ Streaming request failed or incomplete"
    echo "$STREAM_START"
fi
echo ""

# Test 6: Statistics
echo "Test 6: Statistics Tracking"
echo "-----------------------------------"
STATS=$(curl -sk "$PROXY_URL/api/stats")
echo "$STATS" | python3 -c "
import sys, json
stats = json.load(sys.stdin)
if not stats:
    print('  No statistics yet')
else:
    for provider_id, data in stats.items():
        print(f\"  {provider_id}:\")
        print(f\"    Requests: {data.get('requests', 0)}\")
        print(f\"    Successes: {data.get('successes', 0)}\")
        print(f\"    Failures: {data.get('failures', 0)}\")
" 2>/dev/null || echo "  Statistics available via Web UI"
echo ""

echo "==================================="
echo "Test Results Summary"
echo "==================================="
echo "✓ All core tests passed"
echo ""
echo "Proxy is ready for use!"
echo ""
echo "Next steps:"
echo "  1. Configure Claude Code CLI:"
echo "     export ANTHROPIC_BASE_URL=\"$PROXY_URL\""
echo "     export ANTHROPIC_API_KEY=\"proxy-handled\""
echo ""
echo "  2. Test with Claude Code CLI:"
echo "     cc \"hello world\""
echo ""
echo "  3. Monitor via Web UI:"
echo "     $PROXY_URL/"
echo ""
