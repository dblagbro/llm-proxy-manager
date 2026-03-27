# Publishing Guide for LLM Proxy Manager

This guide provides step-by-step instructions for publishing the LLM Proxy Manager to GitHub and Docker Hub.

## Prerequisites

- GitHub account
- Docker Hub account
- Git configured locally
- Docker installed and running

## 1. Create GitHub Repository

### Option A: Using GitHub Web Interface

1. Go to https://github.com/new
2. Fill in repository details:
   - **Repository name**: `llm-proxy-manager`
   - **Description**: Production-ready LLM API proxy with multi-provider failover and web-based management
   - **Visibility**: Public
   - **DO NOT** initialize with README, .gitignore, or license (we already have these)
3. Click "Create repository"

### Option B: Using GitHub CLI

```bash
# Install gh CLI if needed
# See: https://cli.github.com/

# Create repository
gh repo create llm-proxy-manager \
  --public \
  --description "Production-ready LLM API proxy with multi-provider failover and web-based management" \
  --source=. \
  --remote=origin \
  --push
```

## 2. Push to GitHub

If you created the repo via web interface, push the code:

```bash
cd /tmp/llm-proxy-clean

# Add GitHub remote (replace USERNAME with your GitHub username)
git remote add origin https://github.com/USERNAME/llm-proxy-manager.git

# Push to GitHub
git branch -M main
git push -u origin main
```

## 3. Update Repository Settings

After pushing, update these files with your actual GitHub username:

### package.json
```json
"repository": {
  "type": "git",
  "url": "https://github.com/USERNAME/llm-proxy-manager.git"
},
"bugs": {
  "url": "https://github.com/USERNAME/llm-proxy-manager/issues"
},
"homepage": "https://github.com/USERNAME/llm-proxy-manager#readme"
```

### README.md
Update the clone URL:
```bash
git clone https://github.com/USERNAME/llm-proxy-manager.git
```

Then commit and push the updates:
```bash
git add package.json README.md
git commit -m "Update repository URLs with actual GitHub username"
git push
```

## 4. Build and Test Docker Image Locally

Before pushing to Docker Hub, test the image locally:

```bash
cd /tmp/llm-proxy-clean

# Build the image
docker build -t llm-proxy-manager:1.0.0 .

# Test the image
docker run -d \
  -p 3100:3000 \
  -e SESSION_SECRET=test-secret \
  --name llm-proxy-test \
  llm-proxy-manager:1.0.0

# Wait a few seconds, then check health
curl http://localhost:3100/health

# Check logs
docker logs llm-proxy-test

# Access web UI
# Open http://localhost:3100 in browser
# Login: admin / admin

# Stop and remove test container
docker stop llm-proxy-test
docker rm llm-proxy-test
```

## 5. Publish to Docker Hub

### Login to Docker Hub

```bash
docker login
# Enter your Docker Hub username and password
```

### Tag and Push Image

```bash
# Tag the image (replace USERNAME with your Docker Hub username)
docker tag llm-proxy-manager:1.0.0 USERNAME/llm-proxy-manager:1.0.0
docker tag llm-proxy-manager:1.0.0 USERNAME/llm-proxy-manager:latest

# Push to Docker Hub
docker push USERNAME/llm-proxy-manager:1.0.0
docker push USERNAME/llm-proxy-manager:latest
```

### Update Docker Hub Repository

1. Go to https://hub.docker.com/repository/docker/USERNAME/llm-proxy-manager
2. Add repository description: "Production-ready LLM API proxy with multi-provider failover and web-based management"
3. Link to GitHub repository
4. Update README with usage instructions (can copy from project README.md)

## 6. Update Documentation with Docker Hub URL

### README.md

Update the Quick Start section:

```bash
# Pull and run from Docker Hub
docker pull USERNAME/llm-proxy-manager:latest

docker run -d \
  -p 3100:3000 \
  -v $(pwd)/config:/app/config \
  -v $(pwd)/logs:/app/logs \
  -e SESSION_SECRET=your-random-secret-here \
  --name llm-proxy \
  USERNAME/llm-proxy-manager:latest
```

### docker-compose.yml

Update the image reference:

```yaml
services:
  llm-proxy:
    image: USERNAME/llm-proxy-manager:latest
    # ... rest of configuration
```

Commit and push these updates:

```bash
git add README.md docker-compose.yml
git commit -m "Update Docker Hub image references"
git push
```

## 7. Create GitHub Release

1. Go to your repository on GitHub
2. Click "Releases" → "Create a new release"
3. Tag version: `v1.0.0`
4. Release title: `v1.0.0 - Initial Release`
5. Description:
```markdown
## Features

- Multi-provider LLM support (Anthropic, Google, OpenAI, Grok, Ollama)
- Automatic failover with priority-based routing
- Web-based management dashboard
- Session-based authentication
- API key management for external applications
- Real-time activity logging and statistics
- Server-Sent Events (SSE) streaming support

## Installation

### Docker
\`\`\`bash
docker pull USERNAME/llm-proxy-manager:1.0.0
\`\`\`

### npm
\`\`\`bash
git clone https://github.com/USERNAME/llm-proxy-manager.git
cd llm-proxy-manager
npm install
npm start
\`\`\`

See [README.md](README.md) for full documentation.
```

6. Click "Publish release"

## 8. Optional: Set Up GitHub Actions for Automated Docker Builds

Create `.github/workflows/docker-publish.yml`:

```yaml
name: Docker Build and Publish

on:
  push:
    tags:
      - 'v*'
  release:
    types: [published]

jobs:
  docker:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v3

      - name: Docker meta
        id: meta
        uses: docker/metadata-action@v4
        with:
          images: USERNAME/llm-proxy-manager
          tags: |
            type=semver,pattern={{version}}
            type=semver,pattern={{major}}.{{minor}}
            type=semver,pattern={{major}}
            type=raw,value=latest,enable={{is_default_branch}}

      - name: Login to Docker Hub
        uses: docker/login-action@v2
        with:
          username: ${{ secrets.DOCKERHUB_USERNAME }}
          password: ${{ secrets.DOCKERHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v4
        with:
          context: .
          push: true
          tags: ${{ steps.meta.outputs.tags }}
          labels: ${{ steps.meta.outputs.labels }}
```

Add Docker Hub credentials to GitHub Secrets:
1. Go to repository Settings → Secrets and variables → Actions
2. Add `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN`

## Verification Checklist

- [ ] Code pushed to GitHub
- [ ] Repository is public
- [ ] README.md displays correctly on GitHub
- [ ] LICENSE file is present
- [ ] Docker image builds successfully
- [ ] Docker image pushed to Docker Hub
- [ ] Docker Hub repository has description
- [ ] Docker image runs and health check passes
- [ ] Web UI accessible at http://localhost:3100
- [ ] Default login works (admin/admin)
- [ ] GitHub release created with v1.0.0 tag
- [ ] All URLs in documentation updated with actual usernames

## Post-Publication

1. **Announce**: Share on relevant platforms (Reddit, Hacker News, etc.)
2. **Monitor**: Watch for issues and pull requests
3. **Document**: Add examples and use cases to wiki
4. **Maintain**: Plan for regular updates and security patches

## Backup Location

A complete backup of the clean source code is available at:
```
/mnt/s/open-source/llm-proxy-manager/
```

This backup includes:
- Complete source code
- Git history with 3 commits
- All documentation
- Optimized Dockerfile
- Configuration templates
