#!/bin/bash
# Deploy LLM Proxy with API Key Management

set -e

REMOTE_HOST="192.168.18.11"
REMOTE_USER="dblagbro"
REMOTE_PASS="Super*120120"
REMOTE_DIR="/opt/llm-proxy"

echo "==================================="
echo "Deploying LLM Proxy with API Keys"
echo "==================================="

echo "Step 1: Copying updated files..."
sshpass -p "${REMOTE_PASS}" scp -o StrictHostKeyChecking=no \
    src/server.js \
    public/index.html \
    ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/src/server.js &
sshpass -p "${REMOTE_PASS}" scp -o StrictHostKeyChecking=no \
    public/index.html \
    ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/public/ &
wait

echo "Step 2: Restarting container..."
sshpass -p "${REMOTE_PASS}" ssh -o StrictHostKeyChecking=no ${REMOTE_USER}@${REMOTE_HOST} << 'ENDSSH'
cd /opt/llm-proxy

# Rebuild and restart
docker-compose build --no-cache
docker-compose down
docker-compose up -d

sleep 5

# Check status
if docker ps | grep -q llm-proxy; then
    echo "✓ Container is running"
else
    echo "✗ Container failed"
    docker-compose logs --tail=50
    exit 1
fi

# Test health
if curl -sf http://localhost:3100/health > /dev/null; then
    echo "✓ Health check passed"
else
    echo "✗ Health check failed"
    exit 1
fi

echo ""
echo "==================================="
echo "Deployment Complete!"
echo "==================================="
echo ""
echo "New Features:"
echo "  ✓ Client API key management"
echo "  ✓ Per-key usage tracking"
echo "  ✓ Provider testing endpoint"
echo "  ✓ Enhanced Web UI"
echo ""
echo "Access: https://www.voipguru.org/llmProxy/"
echo ""
ENDSSH

echo ""
echo "Next Steps:"
echo "1. Open https://www.voipguru.org/llmProxy/"
echo "2. Click 'Add Provider' to test provider management"
echo "3. Generate an API key for testing"
echo "4. Test with: curl -H 'x-api-key: YOUR-KEY' https://www.voipguru.org/llmProxy/v1/messages ..."
echo ""
