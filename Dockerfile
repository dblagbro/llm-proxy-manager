# Multi-stage build for optimized image size
FROM node:18-alpine AS builder

WORKDIR /app

# Copy dependency files
COPY package*.json ./

# Install all dependencies (including dev dependencies for build)
RUN npm ci --quiet

# Copy application source
COPY src/ ./src/
COPY public/ ./public/

# Production stage
FROM node:18-alpine

# Add metadata
LABEL maintainer="LLM Proxy Manager Contributors"
LABEL description="Production-ready LLM API proxy with multi-provider failover"
LABEL version="1.0.0"

# Create non-root user for security
RUN addgroup -g 1001 -S nodejs && \
    adduser -S nodejs -u 1001

WORKDIR /app

# Copy package files and install production dependencies only
COPY package*.json ./
RUN npm ci --only=production --quiet && \
    npm cache clean --force

# Copy application from builder
COPY --from=builder --chown=nodejs:nodejs /app/src ./src
COPY --from=builder --chown=nodejs:nodejs /app/public ./public

# Create directories for logs and config with proper permissions
RUN mkdir -p /app/logs /app/config && \
    chown -R nodejs:nodejs /app

# Switch to non-root user
USER nodejs

# Expose port
EXPOSE 3000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
  CMD node -e "require('http').get('http://localhost:3000/health', (r) => { process.exit(r.statusCode === 200 ? 0 : 1); })"

# Start server
CMD ["node", "src/server.js"]
