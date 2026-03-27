#!/bin/bash
# Update LLM Proxy on TMRwww01 with streaming support

set -e

REMOTE_HOST="192.168.18.11"
REMOTE_USER="dblagbro"
REMOTE_PASS="Super*120120"
REMOTE_DIR="/opt/llm-proxy"

echo "==================================="
echo "Updating LLM Proxy with SSE Streaming"
echo "==================================="

echo "Step 1: Copying updated server.js..."
sshpass -p "${REMOTE_PASS}" scp -o StrictHostKeyChecking=no \
    src/server.js \
    ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/src/

echo "Step 2: Rebuilding and restarting container..."
sshpass -p "${REMOTE_PASS}" ssh -o StrictHostKeyChecking=no ${REMOTE_USER}@${REMOTE_HOST} << 'ENDSSH'
cd /opt/llm-proxy

# Rebuild the image
echo "Rebuilding Docker image..."
docker-compose build --no-cache

# Restart the container
echo "Restarting container..."
docker-compose down
docker-compose up -d

# Wait for container to be ready
echo "Waiting for container to start..."
sleep 5

# Check if container is running
if docker ps | grep -q llm-proxy; then
    echo "✓ Container is running"
    docker ps | grep llm-proxy
else
    echo "✗ Container failed to start"
    docker-compose logs --tail=50
    exit 1
fi

# Test health endpoint
echo "Testing health endpoint..."
if curl -sf http://localhost:3100/health > /dev/null; then
    echo "✓ Health check passed"
    echo ""
    curl -s http://localhost:3100/health | python3 -m json.tool
else
    echo "✗ Health check failed"
    exit 1
fi

echo ""
echo "==================================="
echo "Update Complete!"
echo "==================================="
echo "SSE Streaming: ENABLED"
echo "Service URL: http://localhost:3100"
echo ""
ENDSSH

echo ""
echo "LLM Proxy updated with streaming support!"
echo "Now accessible at: https://www.voipguru.org/llmProxy/"
echo ""
