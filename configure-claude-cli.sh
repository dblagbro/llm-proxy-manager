#!/bin/bash
# Configure Claude Code CLI to use LLM Proxy

echo "==================================="
echo "Claude Code CLI Configuration"
echo "==================================="
echo ""

# Detect shell
SHELL_NAME=$(basename "$SHELL")
if [ "$SHELL_NAME" = "bash" ]; then
    RC_FILE="$HOME/.bashrc"
elif [ "$SHELL_NAME" = "zsh" ]; then
    RC_FILE="$HOME/.zshrc"
else
    RC_FILE="$HOME/.profile"
fi

echo "Detected shell: $SHELL_NAME"
echo "Configuration file: $RC_FILE"
echo ""

# Prompt for proxy URL
echo "Enter LLM Proxy URL:"
echo "  Local: http://localhost:3100"
echo "  Remote: https://www.voipguru.org/llmProxy"
echo ""
read -p "Proxy URL [http://localhost:3100]: " PROXY_URL
PROXY_URL=${PROXY_URL:-http://localhost:3100}

echo ""
echo "Adding configuration to $RC_FILE..."

# Remove existing Claude config if present
sed -i '/# LLM Proxy Configuration/,/# End LLM Proxy Configuration/d' "$RC_FILE"

# Add new configuration
cat >> "$RC_FILE" << EOF

# LLM Proxy Configuration
export ANTHROPIC_BASE_URL="$PROXY_URL"
export ANTHROPIC_API_KEY="proxy-handled"  # Proxy handles API keys
# End LLM Proxy Configuration
EOF

echo "✓ Configuration added to $RC_FILE"
echo ""
echo "To apply changes, run:"
echo "  source $RC_FILE"
echo ""
echo "Or close and reopen your terminal."
echo ""
echo "Test with:"
echo "  cc 'hello world'"
echo ""
