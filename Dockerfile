FROM node:20-alpine

WORKDIR /app

# Install build tools needed for native modules (better-sqlite3)
RUN apk add --no-cache python3 make g++

# Install dependencies
COPY package*.json ./
RUN npm install --production

# Copy application
COPY src/ ./src/
COPY public/ ./public/

# Create directories for logs and config
RUN mkdir -p /app/logs /app/config

# Expose port
EXPOSE 3000

# Start server
CMD ["node", "src/server.js"]
