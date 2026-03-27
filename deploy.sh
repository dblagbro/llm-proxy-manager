#!/bin/bash
# Deploy LLM Proxy to TMRwww01

set -e

# Configuration
REMOTE_HOST="192.168.18.11"
REMOTE_USER="dblagbro"
REMOTE_PASS="Super*120120"
REMOTE_DIR="/opt/llm-proxy"

echo "==================================="
echo "LLM Proxy Deployment Script"
echo "==================================="
echo "Target: ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
echo ""

# Check if sshpass is installed
if ! command -v sshpass &> /dev/null; then
    echo "Installing sshpass..."
    sudo apt-get update && sudo apt-get install -y sshpass
fi

echo "Step 1: Creating remote directory..."
sshpass -p "${REMOTE_PASS}" ssh -o StrictHostKeyChecking=no ${REMOTE_USER}@${REMOTE_HOST} \
    "sudo mkdir -p ${REMOTE_DIR} && sudo chown ${REMOTE_USER}:${REMOTE_USER} ${REMOTE_DIR}"

echo "Step 2: Copying files to remote server..."
sshpass -p "${REMOTE_PASS}" scp -o StrictHostKeyChecking=no -r \
    package.json \
    Dockerfile \
    docker-compose.yml \
    src/ \
    public/ \
    README.md \
    nginx-config.conf \
    ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/

echo "Step 3: Setting up Docker container on remote server..."
sshpass -p "${REMOTE_PASS}" ssh -o StrictHostKeyChecking=no ${REMOTE_USER}@${REMOTE_HOST} << 'ENDSSH'
cd /opt/llm-proxy

# Create necessary directories
mkdir -p config logs

# Stop existing container if running
if docker ps -a | grep -q llm-proxy; then
    echo "Stopping existing container..."
    docker-compose down
fi

# Build and start the container
echo "Building Docker image..."
docker-compose build

echo "Starting container..."
docker-compose up -d

# Wait for container to be healthy
echo "Waiting for container to be ready..."
sleep 5

# Check if container is running
if docker ps | grep -q llm-proxy; then
    echo "✓ Container is running"
    docker ps | grep llm-proxy
else
    echo "✗ Container failed to start"
    docker-compose logs
    exit 1
fi

# Test health endpoint
if curl -sf http://localhost:3100/health > /dev/null; then
    echo "✓ Health check passed"
else
    echo "✗ Health check failed"
    exit 1
fi

echo ""
echo "==================================="
echo "Deployment successful!"
echo "==================================="
echo "Service URL: http://localhost:3100"
echo "Web UI: http://localhost:3100/"
echo "Health: http://localhost:3100/health"
echo ""
echo "View logs: docker-compose -f /opt/llm-proxy/docker-compose.yml logs -f"
echo ""
ENDSSH

echo ""
echo "Step 4: Nginx configuration"
echo "-----------------------------------"
echo "To complete setup, add the following to your nginx configuration:"
echo ""
sshpass -p "${REMOTE_PASS}" ssh -o StrictHostKeyChecking=no ${REMOTE_USER}@${REMOTE_HOST} \
    "cat ${REMOTE_DIR}/nginx-config.conf"
echo ""
echo "Then run: sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "==================================="
echo "Deployment Complete!"
echo "==================================="
echo ""
echo "Next steps:"
echo "1. Add nginx configuration to /etc/nginx/sites-available/www.voipguru.org"
echo "2. Test nginx config: sudo nginx -t"
echo "3. Reload nginx: sudo systemctl reload nginx"
echo "4. Access Web UI: https://www.voipguru.org/llmProxy/"
echo "5. Configure Claude Code CLI to use: https://www.voipguru.org/llmProxy"
echo ""
