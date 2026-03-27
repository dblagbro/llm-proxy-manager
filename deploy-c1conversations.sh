#!/bin/bash
# Deployment script for LLM Proxy Manager on c1conversations-avaya-01
# This is a hub node in the cluster

set -e  # Exit on error

echo "=========================================="
echo "LLM Proxy Manager - C1 Conversations Hub "
echo "=========================================="

# Configuration
NODE_ID="c1conversations-avaya-01"
NODE_NAME="C1 Conversations Hub LLM Proxy"
NODE_URL="http://c1conversations-avaya-01:3000"
INSTALL_DIR="/opt/llm-proxy"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# Prompt for cluster secret
echo -e "${YELLOW}Enter the cluster secret from TMRwww01:${NC}"
read -r CLUSTER_SECRET

if [ -z "$CLUSTER_SECRET" ]; then
    echo -e "${RED}ERROR: Cluster secret is required!${NC}"
    exit 1
fi

echo -e "${GREEN}Step 1: Installing dependencies...${NC}"
# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "Installing Node.js..."
    curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
    sudo apt-get install -y nodejs
fi

echo -e "${GREEN}Step 2: Creating installation directory...${NC}"
sudo mkdir -p "$INSTALL_DIR"
sudo chown $USER:$USER "$INSTALL_DIR"

echo -e "${GREEN}Step 3: Copying application files...${NC}"
rsync -av --exclude=node_modules --exclude=.git --exclude=config --exclude=logs \
    ./ "$INSTALL_DIR/"

cd "$INSTALL_DIR"

echo -e "${GREEN}Step 4: Installing npm dependencies...${NC}"
npm install --production

echo -e "${GREEN}Step 5: Creating configuration...${NC}"

# Create .env file
cat > .env <<EOF
# LLM Proxy Manager - C1 Conversations Hub Configuration
NODE_ENV=production
PORT=3000

# Session Secret
SESSION_SECRET=$(openssl rand -hex 32)

# Circuit Breaker
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_TIMEOUT=60000
CIRCUIT_BREAKER_HALFOPEN=30000
CIRCUIT_BREAKER_SUCCESS=2

# Timeouts
ANTHROPIC_TIMEOUT=30000
GOOGLE_TIMEOUT=30000
OPENAI_TIMEOUT=30000

# Cluster Configuration (Hub Node)
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=$NODE_ID
CLUSTER_NODE_NAME="$NODE_NAME"
CLUSTER_NODE_URL=$NODE_URL
CLUSTER_SYNC_SECRET=$CLUSTER_SECRET
CLUSTER_PEERS=tmrwww01:http://tmrwww01:3000,tmrwww02:http://tmrwww02:3000
CLUSTER_SYNC_ACTIVITY_LOG=false

# SMTP Notifications (Configure with your SMTP details)
SMTP_ENABLED=false
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_SECURE=false
# SMTP_USER=your-email@example.com
# SMTP_PASS=your-app-password
# SMTP_FROM=llm-proxy@c1conversations
# SMTP_TO=admin@example.com
# SMTP_MIN_SEVERITY=WARNING

# Provider API Keys (Add your keys here or will sync from cluster)
# ANTHROPIC_KEY_1=sk-ant-api03-...
# GOOGLE_API_KEY_1=AIzaSy...
# OPENAI_KEY_1=sk-...
EOF

# Create systemd service
echo -e "${GREEN}Step 6: Creating systemd service...${NC}"
sudo tee /etc/systemd/system/llm-proxy.service > /dev/null <<EOF
[Unit]
Description=LLM Proxy Manager
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/node src/server.js
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=llm-proxy

# Environment
Environment=NODE_ENV=production

[Install]
WantedBy=multi-user.target
EOF

echo -e "${GREEN}Step 7: Starting service...${NC}"
sudo systemctl daemon-reload
sudo systemctl enable llm-proxy
sudo systemctl start llm-proxy

echo -e "${GREEN}Step 8: Checking service status...${NC}"
sleep 3
sudo systemctl status llm-proxy --no-pager

echo ""
echo "========================================="
echo -e "${GREEN}Deployment Complete!${NC}"
echo "========================================="
echo ""
echo "Web UI: http://$(hostname):3000"
echo ""
echo -e "${YELLOW}IMPORTANT:${NC}"
echo "- This is the C1 Conversations Hub node"
echo "- Configure providers independently for C1-specific workloads"
echo "- Users and API keys will sync from the cluster"
echo ""
echo "Logs: sudo journalctl -u llm-proxy -f"
echo "Restart: sudo systemctl restart llm-proxy"
echo "Stop: sudo systemctl stop llm-proxy"
echo ""
