# Docker Hub Publishing Guide

This guide provides step-by-step instructions for building and publishing LLM Proxy Manager to Docker Hub.

## Prerequisites

- Docker installed and running
- Docker Hub account (create at https://hub.docker.com)
- Git repository pushed to GitHub

## Step 1: Test Build Locally

```bash
cd /tmp/llm-proxy-clean

# Build the image
docker build -t llm-proxy-manager:1.0.0 .

# Test the image
docker run -d \
  -p 3000:3000 \
  -e SESSION_SECRET=test-secret \
  --name llm-proxy-test \
  llm-proxy-manager:1.0.0

# Wait a few seconds, then check health
curl http://localhost:3000/health

# Check logs
docker logs llm-proxy-test

# Test Web UI
# Open http://localhost:3000 in browser
# Login: admin / admin

# Stop and remove test container
docker stop llm-proxy-test
docker rm llm-proxy-test
```

## Step 2: Login to Docker Hub

```bash
docker login
# Enter your Docker Hub username and password
```

## Step 3: Tag Images

Replace `yourusername` with your Docker Hub username:

```bash
# Tag with version
docker tag llm-proxy-manager:1.0.0 yourusername/llm-proxy-manager:1.0.0

# Tag as latest
docker tag llm-proxy-manager:1.0.0 yourusername/llm-proxy-manager:latest
```

## Step 4: Push to Docker Hub

```bash
# Push versioned tag
docker push yourusername/llm-proxy-manager:1.0.0

# Push latest tag
docker push yourusername/llm-proxy-manager:latest
```

## Step 5: Create Docker Hub Repository

1. Go to https://hub.docker.com/repositories
2. Click "Create Repository"
3. Fill in details:
   - **Name**: `llm-proxy-manager`
   - **Description**: Production-ready LLM API proxy with multi-provider failover, intelligent monitoring, and cluster mode
   - **Visibility**: Public
4. Click "Create"

## Step 6: Update Repository README

Go to your Docker Hub repository page and add this README:

```markdown
# LLM Proxy Manager

Production-ready LLM API proxy with **multi-provider failover**, **intelligent monitoring**, **cluster mode**, and **web-based management**.

## Quick Start

```bash
docker pull yourusername/llm-proxy-manager:latest

docker run -d \
  -p 3000:3000 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -e SESSION_SECRET=your-random-secret \
  --name llm-proxy \
  yourusername/llm-proxy-manager:latest
```

Access Web UI at http://localhost:3000

**Default Login**: `admin` / `admin` (change immediately!)

## Features

- 🔄 Multi-provider support (Anthropic, Google, OpenAI, Grok, Ollama)
- 🎯 Intelligent failover with circuit breaker protection
- 🌍 Cluster mode for high availability
- 📧 Email notifications for failures
- 🌓 Dark mode Web UI
- 🔐 Secure authentication and API key management

## Docker Compose

```yaml
version: '3.8'
services:
  llm-proxy:
    image: yourusername/llm-proxy-manager:latest
    ports:
      - "3000:3000"
    volumes:
      - ./config:/app/config
      - ./logs:/app/logs
    environment:
      - SESSION_SECRET=your-random-secret-here
      - CLUSTER_ENABLED=false
      # Add your provider API keys
      # - ANTHROPIC_KEY_1=sk-ant-api03-...
      # - GOOGLE_API_KEY_1=AIzaSy...
    restart: unless-stopped
```

## Documentation

- GitHub: https://github.com/yourusername/llm-proxy-manager
- Full Docs: See GitHub repository README.md

## Cluster Deployment

For high-availability cluster deployment:

```bash
# Node 1 (Primary)
docker run -d \
  -p 3000:3000 \
  -e CLUSTER_ENABLED=true \
  -e CLUSTER_NODE_ID=node1 \
  -e CLUSTER_NODE_NAME="Proxy Node 1" \
  -e CLUSTER_SYNC_SECRET=your-shared-secret \
  -e CLUSTER_PEERS=node2:http://node2:3000,node3:http://node3:3000 \
  --name llm-proxy-1 \
  yourusername/llm-proxy-manager:latest
```

Repeat for additional nodes with different `CLUSTER_NODE_ID` and `CLUSTER_PEERS`.

## Support

- GitHub Issues: https://github.com/yourusername/llm-proxy-manager/issues
- Documentation: https://github.com/yourusername/llm-proxy-manager#readme

## License

MIT License - see GitHub repository for details
```

## Step 7: Update docker-compose.yml in GitHub

Update the image reference in your `docker-compose.yml`:

```yaml
services:
  llm-proxy:
    image: yourusername/llm-proxy-manager:latest
    # ... rest of configuration
```

Commit and push:
```bash
git add docker-compose.yml
git commit -m "Update Docker Compose to use Docker Hub image"
git push
```

## Step 8: Update README.md in GitHub

Update the Docker quick start section:

```markdown
## Quick Start

### Docker (Recommended)

```bash
docker pull yourusername/llm-proxy-manager:latest

docker run -d \
  -p 3000:3000 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -e SESSION_SECRET=your-random-secret \
  --name llm-proxy \
  yourusername/llm-proxy-manager:latest
```

Access at http://localhost:3000
Default login: admin / admin
```

## Automated Builds with GitHub Actions (Optional)

Create `.github/workflows/docker-publish.yml`:

```yaml
name: Docker Build and Publish

on:
  push:
    tags:
      - 'v*'
  release:
    types: [published]
  workflow_dispatch:

env:
  REGISTRY: docker.io
  IMAGE_NAME: yourusername/llm-proxy-manager

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Docker Buildx
        uses: docker/setup-buildx-action@v3

      - name: Log in to Docker Hub
        uses: docker/login-action@v3
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_PASSWORD }}

      - name: Extract metadata
        id: meta
        uses: docker/metadata-action@v5
        with:
          images: ${{ env.IMAGE_NAME }}
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=semver,pattern={{major}}
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
          cache-from: type=gha
          cache-to: type=gha,mode=max

      - name: Update Docker Hub description
        uses: peter-evans/dockerhub-description@v3
        with:
          username: ${{ secrets.DOCKER_USERNAME }}
          password: ${{ secrets.DOCKER_PASSWORD }}
          repository: ${{ env.IMAGE_NAME }}
          readme-filepath: ./README.md
```

Add secrets to GitHub repository:
1. Go to Settings → Secrets and variables → Actions
2. Add `DOCKER_USERNAME` (your Docker Hub username)
3. Add `DOCKER_PASSWORD` (Docker Hub access token)

## Testing the Published Image

```bash
# Pull from Docker Hub
docker pull yourusername/llm-proxy-manager:latest

# Run
docker run -d \
  -p 3000:3000 \
  -e SESSION_SECRET=test-secret \
  --name llm-proxy \
  yourusername/llm-proxy-manager:latest

# Test
curl http://localhost:3000/health

# Check logs
docker logs llm-proxy

# Access Web UI
open http://localhost:3000
```

## Multi-Architecture Builds (Optional)

Build for multiple platforms:

```bash
# Create and use a new builder
docker buildx create --name mybuilder --use
docker buildx inspect --bootstrap

# Build for multiple architectures
docker buildx build \
  --platform linux/amd64,linux/arm64,linux/arm/v7 \
  -t yourusername/llm-proxy-manager:1.0.0 \
  -t yourusername/llm-proxy-manager:latest \
  --push \
  .
```

This creates images that work on:
- x86_64 / amd64 (most servers, Intel/AMD)
- ARM64 (Apple Silicon, AWS Graviton)
- ARMv7 (Raspberry Pi)

## Image Size Optimization

The Dockerfile uses multi-stage builds to minimize image size:

```dockerfile
# Build stage (large, includes dev dependencies)
FROM node:18-alpine AS builder
# ... build steps ...

# Production stage (small, only production dependencies)
FROM node:18-alpine
# ... copy only what's needed ...
```

Expected image size: ~150-200MB

## Maintenance

### Updating the Image

```bash
# Make changes to code
# Commit changes to git

# Build new version
docker build -t llm-proxy-manager:1.0.1 .

# Tag
docker tag llm-proxy-manager:1.0.1 yourusername/llm-proxy-manager:1.0.1
docker tag llm-proxy-manager:1.0.1 yourusername/llm-proxy-manager:latest

# Push
docker push yourusername/llm-proxy-manager:1.0.1
docker push yourusername/llm-proxy-manager:latest
```

### Tagging Strategy

- `latest` - Most recent stable release
- `1.0.0` - Specific version
- `1.0` - Minor version (auto-updated for patches)
- `1` - Major version (auto-updated for minor/patches)

## Security Scanning

Scan for vulnerabilities:

```bash
# Using Docker Scout (built-in)
docker scout cves llm-proxy-manager:latest

# Using Trivy
trivy image llm-proxy-manager:latest

# Using Snyk
snyk container test llm-proxy-manager:latest
```

## Troubleshooting

### Build Fails

```bash
# Check Dockerfile syntax
docker build --no-cache -t llm-proxy-manager:latest .

# Check for errors in build logs
docker build -t llm-proxy-manager:latest . 2>&1 | tee build.log
```

### Push Fails

```bash
# Re-authenticate
docker logout
docker login

# Check image exists
docker images | grep llm-proxy-manager

# Try pushing with verbose output
docker push yourusername/llm-proxy-manager:latest -v
```

### Permission Denied

```bash
# Add user to docker group (Linux)
sudo usermod -aG docker $USER
newgrp docker

# Or run with sudo
sudo docker build -t llm-proxy-manager:latest .
```

## Complete Build and Push Script

Create `build-and-push.sh`:

```bash
#!/bin/bash
set -e

VERSION="${1:-1.0.0}"
DOCKER_USERNAME="${2:-yourusername}"

echo "Building LLM Proxy Manager v${VERSION}"

# Build
docker build -t llm-proxy-manager:${VERSION} .

# Test
echo "Testing image..."
docker run -d --name llm-proxy-test -p 3001:3000 \
  -e SESSION_SECRET=test llm-proxy-manager:${VERSION}
sleep 5

if curl -sf http://localhost:3001/health > /dev/null; then
  echo "✓ Health check passed"
else
  echo "✗ Health check failed"
  docker logs llm-proxy-test
  docker stop llm-proxy-test
  docker rm llm-proxy-test
  exit 1
fi

docker stop llm-proxy-test
docker rm llm-proxy-test

# Tag
echo "Tagging images..."
docker tag llm-proxy-manager:${VERSION} ${DOCKER_USERNAME}/llm-proxy-manager:${VERSION}
docker tag llm-proxy-manager:${VERSION} ${DOCKER_USERNAME}/llm-proxy-manager:latest

# Push
echo "Pushing to Docker Hub..."
docker push ${DOCKER_USERNAME}/llm-proxy-manager:${VERSION}
docker push ${DOCKER_USERNAME}/llm-proxy-manager:latest

echo "✓ Successfully pushed to Docker Hub!"
echo "  docker pull ${DOCKER_USERNAME}/llm-proxy-manager:${VERSION}"
echo "  docker pull ${DOCKER_USERNAME}/llm-proxy-manager:latest"
```

Make executable and run:
```bash
chmod +x build-and-push.sh
./build-and-push.sh 1.0.0 yourusername
```

## Verification

After publishing, verify:

1. **Docker Hub**: Image appears at https://hub.docker.com/r/yourusername/llm-proxy-manager
2. **Pull Test**: `docker pull yourusername/llm-proxy-manager:latest` works
3. **Run Test**: Container starts and health check passes
4. **README**: Repository README displays correctly on Docker Hub

## Next Steps

1. Update GitHub README with Docker Hub link
2. Create GitHub release (v1.0.0)
3. Announce on relevant platforms (Reddit, Hacker News, etc.)
4. Monitor Docker Hub for pull statistics
5. Set up vulnerability scanning alerts

---

For questions or issues, see the GitHub repository.
