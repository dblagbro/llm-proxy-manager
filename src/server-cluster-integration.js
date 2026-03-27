/**
 * Integration code for adding Cluster and Monitor modules to server.js
 * This file contains the code snippets to add at various points in server.js
 */

// ============================================================================
// 1. ADD AFTER EXISTING REQUIRES (after line 10):
// ============================================================================

const ClusterManager = require('./cluster');
const ProviderMonitor = require('./monitor');

// ============================================================================
// 2. ADD AFTER CONFIG INITIALIZATION (after loadConfig() call, around line 120):
// ============================================================================

// Initialize Provider Monitor
const providerMonitor = new ProviderMonitor(logger);

// Initialize Cluster Manager
const clusterManager = new ClusterManager(logger, config);

// Monitor event handlers
providerMonitor.on('circuit.open', ({ provider, reason }) => {
  addActivityLog('warning', `Circuit breaker OPEN for ${provider.name}`, {
    providerId: provider.id,
    reason: reason
  });
});

providerMonitor.on('circuit.closed', ({ provider }) => {
  addActivityLog('success', `Circuit breaker CLOSED for ${provider.name} - recovered`, {
    providerId: provider.id
  });
});

providerMonitor.on('billing.error', ({ provider, error }) => {
  addActivityLog('error', `Billing/quota error detected for ${provider.name}`, {
    providerId: provider.id,
    error: error
  });
});

providerMonitor.on('external.degraded', ({ providerType, status, incidents }) => {
  addActivityLog('warning', `External service ${providerType} reporting ${status}`, {
    incidents: incidents
  });
});

// Cluster event handlers
clusterManager.on('peer.unhealthy', (peer) => {
  addActivityLog('warning', `Cluster peer unhealthy: ${peer.name}`, {
    peerId: peer.id
  });
});

clusterManager.on('peer.healthy', (peer) => {
  addActivityLog('info', `Cluster peer healthy: ${peer.name}`, {
    peerId: peer.id,
    latency: peer.latency
  });
});

clusterManager.on('config.merged', ({ peer, changes }) => {
  addActivityLog('info', `Configuration synchronized from ${peer}`, {
    changes: changes
  });
  saveConfig(); // Save merged config
});

// ============================================================================
// 3. MODIFY selectProvider() FUNCTION (around line 240):
// ============================================================================

function selectProvider() {
  // Filter enabled providers
  let enabledProviders = config.providers.filter(p => p.enabled);

  // Apply circuit breaker filtering
  enabledProviders = enabledProviders.filter(p => {
    const check = providerMonitor.canAttemptProvider(p);
    if (!check.allowed) {
      logger.warn(`Provider ${p.name} blocked: ${check.reason}`);
    }
    return check.allowed;
  });

  if (enabledProviders.length === 0) {
    return null;
  }

  // Sort by priority (lower number = higher priority)
  enabledProviders.sort((a, b) => {
    const priorityA = a.priority || 999;
    const priorityB = b.priority || 999;
    return priorityA - priorityB;
  });

  return enabledProviders[0];
}

// ============================================================================
// 4. ADD TIMEOUT TO PROVIDER REQUESTS (in callProvider function, around line 350):
// ============================================================================

// Add timeout parameter to axios calls:
const timeout = providerMonitor.getProviderTimeout(provider.type);

// Example for Anthropic:
const response = await axios.post(
  'https://api.anthropic.com/v1/messages',
  anthropicRequest,
  {
    headers: {
      'x-api-key': provider.apiKey,
      'Content-Type': 'application/json',
      'anthropic-version': '2023-06-01'
    },
    responseType: stream ? 'stream' : 'json',
    timeout: timeout  // ADD THIS LINE
  }
);

// ============================================================================
// 5. RECORD SUCCESS/FAILURE IN callProvider (around line 400 and 450):
// ============================================================================

// On success (after successful provider call):
providerMonitor.recordSuccess(provider);

// Update stats
if (!config.stats[provider.id]) {
  config.stats[provider.id] = { requests: 0, success: 0, failure: 0, latency: [] };
}
config.stats[provider.id].requests++;
config.stats[provider.id].success++;
config.stats[provider.id].latency.push(Date.now() - requestStart);
config.stats[provider.id].lastSuccess = new Date().toISOString();

// On failure (in catch block):
providerMonitor.recordFailure(provider, error);

// Update stats
if (!config.stats[provider.id]) {
  config.stats[provider.id] = { requests: 0, success: 0, failure: 0, latency: [] };
}
config.stats[provider.id].requests++;
config.stats[provider.id].failure++;
config.stats[provider.id].lastFailure = new Date().toISOString();

// ============================================================================
// 6. ADD CLUSTER ENDPOINTS (before app.listen, around line 1100):
// ============================================================================

// ==================== CLUSTER ENDPOINTS ====================

// Cluster info endpoint
app.get('/cluster/info', (req, res) => {
  if (!clusterManager.enabled) {
    return res.status(503).json({ error: 'Cluster mode disabled' });
  }

  res.json({
    nodeId: clusterManager.nodeId,
    nodeName: clusterManager.nodeName,
    clusterEnabled: true
  });
});

// Cluster health endpoint (receives heartbeats)
app.post('/cluster/heartbeat', (req, res) => {
  if (!clusterManager.enabled) {
    return res.status(503).json({ error: 'Cluster mode disabled' });
  }

  // Verify cluster auth
  const signature = req.headers['x-cluster-auth'];
  const payload = `POST:/cluster/heartbeat:${JSON.stringify(req.body)}`;

  if (!clusterManager.verifySignature(payload, signature)) {
    return res.status(403).json({ error: 'Invalid cluster signature' });
  }

  // Return our health status
  res.json(clusterManager.getLocalHealth());
});

// Cluster configuration sync endpoint
app.post('/cluster/sync', (req, res) => {
  if (!clusterManager.enabled) {
    return res.status(503).json({ error: 'Cluster mode disabled' });
  }

  // Verify cluster auth
  const signature = req.headers['x-cluster-auth'];
  const payload = `POST:/cluster/sync:${JSON.stringify(req.body)}`;

  if (!clusterManager.verifySignature(payload, signature)) {
    return res.status(403).json({ error: 'Invalid cluster signature' });
  }

  const { sourceNode, data } = req.body;

  logger.info(`Received config sync from: ${sourceNode}`);

  // Merge configuration
  clusterManager.mergeConfiguration(data, sourceNode);
  saveConfig();

  res.json({ success: true, message: 'Configuration synchronized' });
});

// Get cluster configuration (for peers to pull)
app.get('/cluster/config', (req, res) => {
  if (!clusterManager.enabled) {
    return res.status(503).json({ error: 'Cluster mode disabled' });
  }

  // Verify cluster auth
  const signature = req.headers['x-cluster-auth'];
  const payload = `GET:/cluster/config:${JSON.stringify(req.query)}`;

  if (!clusterManager.verifySignature(payload, signature)) {
    return res.status(403).json({ error: 'Invalid cluster signature' });
  }

  res.json({
    success: true,
    config: {
      users: config.users,
      clientApiKeys: config.clientApiKeys,
      activityLog: process.env.CLUSTER_SYNC_ACTIVITY_LOG === 'true'
        ? config.activityLog
        : []
    }
  });
});

// Cluster status endpoint (for client applications)
app.get('/cluster/status', authenticateApiKey, (req, res) => {
  if (!clusterManager.enabled) {
    return res.json({
      clusterEnabled: false,
      localNode: {
        id: clusterManager.nodeId,
        name: clusterManager.nodeName,
        status: 'standalone'
      },
      peers: [],
      totalNodes: 1,
      healthyNodes: 1
    });
  }

  res.json(clusterManager.getClusterStatus());
});

// Provider monitoring status endpoint
app.get('/monitoring/status', requireAuth, (req, res) => {
  res.json(providerMonitor.getMonitoringStatus());
});

// Manual circuit breaker control
app.post('/monitoring/circuit/reset', requireAuth, (req, res) => {
  const { providerId } = req.body;

  if (!providerId) {
    return res.status(400).json({ error: 'providerId required' });
  }

  providerMonitor.manualReset(providerId);

  addActivityLog('info', `Circuit breaker manually reset for provider`, {
    providerId: providerId,
    username: req.session.username
  });

  res.json({ success: true, message: 'Circuit breaker reset' });
});

app.post('/monitoring/circuit/open', requireAuth, (req, res) => {
  const { providerId, reason } = req.body;

  if (!providerId) {
    return res.status(400).json({ error: 'providerId required' });
  }

  providerMonitor.manualOpen(providerId, reason || 'Manual override');

  addActivityLog('warning', `Circuit breaker manually opened for provider`, {
    providerId: providerId,
    reason: reason,
    username: req.session.username
  });

  res.json({ success: true, message: 'Circuit breaker opened' });
});

// Trigger external status check
app.post('/monitoring/check-external', requireAuth, (req, res) => {
  providerMonitor.checkExternalStatus()
    .then(() => {
      res.json({
        success: true,
        message: 'External status check initiated',
        status: providerMonitor.getMonitoringStatus().externalStatus
      });
    })
    .catch(err => {
      res.status(500).json({ error: err.message });
    });
});

// ==================== END CLUSTER ENDPOINTS ====================

// ============================================================================
// 7. START SERVICES IN app.listen callback (around line 1180):
// ============================================================================

app.listen(PORT, '0.0.0.0', () => {
  logger.info(`LLM Proxy Server running on port ${PORT}`);
  logger.info(`Web UI: http://localhost:${PORT}`);
  logger.info(`API endpoint: http://localhost:${PORT}/v1/messages`);

  // Start monitoring and cluster services
  providerMonitor.start();
  clusterManager.start();

  // Graceful shutdown
  process.on('SIGTERM', () => {
    logger.info('SIGTERM received, shutting down gracefully...');
    providerMonitor.stop();
    clusterManager.stop();
    process.exit(0);
  });
});

// ============================================================================
// 8. ADD TO .env.example:
// ============================================================================
/*
# Circuit Breaker Configuration
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_TIMEOUT=60000
CIRCUIT_BREAKER_HALFOPEN=30000
CIRCUIT_BREAKER_SUCCESS=2

# Provider Timeouts (milliseconds)
ANTHROPIC_TIMEOUT=30000
GOOGLE_TIMEOUT=30000
OPENAI_TIMEOUT=30000
GROK_TIMEOUT=30000
OLLAMA_TIMEOUT=60000
VERTEX_TIMEOUT=30000
COMPATIBLE_TIMEOUT=30000

# Cluster Configuration
CLUSTER_ENABLED=false
CLUSTER_NODE_ID=node1
CLUSTER_NODE_NAME="LLM Proxy Node 1"
CLUSTER_NODE_URL=http://localhost:3000
CLUSTER_SYNC_SECRET=change-this-to-a-shared-secret
CLUSTER_PEERS=node2:http://node2:3000,node3:http://node3:3000
CLUSTER_SYNC_ACTIVITY_LOG=true
*/
