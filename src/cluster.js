/**
 * Cluster Management Module
 * Handles multi-instance synchronization, health checks, and peer communication
 */

const axios = require('axios');
const crypto = require('crypto');
const EventEmitter = require('events');

class ClusterManager extends EventEmitter {
  constructor(logger, config) {
    super();
    this.logger = logger;
    this.config = config;

    // Read cluster config from file first, fallback to env vars
    const clusterConfig = config.cluster || {};
    this.enabled = clusterConfig.enabled || process.env.CLUSTER_ENABLED === 'true';

    this.nodeId = process.env.CLUSTER_NODE_ID || require('os').hostname();
    this.nodeName = process.env.CLUSTER_NODE_NAME || clusterConfig.localName || `LLM Proxy ${this.nodeId}`;
    this.syncSecret = process.env.CLUSTER_SYNC_SECRET || '';

    // Parse peer configuration from config file first, fallback to env var
    this.peers = this.parsePeersFromConfig(clusterConfig.nodes) || this.parsePeers(process.env.CLUSTER_PEERS || '');

    // Cluster state
    this.peerHealth = new Map();
    this.lastSync = new Date();
    this.heartbeatInterval = null;
    this.syncInterval = null;

    this.logger.info(`Cluster mode: ${this.enabled ? 'ENABLED' : 'DISABLED'}`);
    if (this.enabled) {
      this.logger.info(`Node ID: ${this.nodeId}, Peers: ${this.peers.length}`);
    }
  }

  parsePeersFromConfig(nodes) {
    if (!nodes || !Array.isArray(nodes)) return null;

    // Filter only active nodes and convert to peer format
    return nodes
      .filter(node => node.active)
      .map(node => ({
        id: node.name || node.host,
        name: node.name || `LLM Proxy ${node.host}`,
        url: node.ssl
          ? `https://${node.host}${node.path || ''}`
          : `http://${node.host}:${node.port || 3000}`,
        healthy: false,
        lastHeartbeat: null,
        latency: 0,
        providers: 0,
        healthyProviders: 0
      }));
  }

  parsePeers(peerString) {
    if (!peerString) return [];

    // Format: "id1:url1,id2:url2"
    return peerString.split(',').map(peer => {
      const trimmed = peer.trim();
      const colonIndex = trimmed.indexOf(':');
      if (colonIndex === -1) return null;

      const id = trimmed.substring(0, colonIndex);
      const url = trimmed.substring(colonIndex + 1);

      return {
        id: id,
        name: `LLM Proxy ${id}`,
        url: url,
        healthy: false,
        lastHeartbeat: null,
        latency: 0,
        providers: 0,
        healthyProviders: 0
      };
    }).filter(p => p && p.id && p.url);
  }

  async start() {
    if (!this.enabled) return;

    this.logger.info('Starting cluster manager...');

    // Initial peer discovery
    await this.discoverPeers();

    // Run initial sync immediately after discovery
    setTimeout(() => {
      this.syncConfiguration();
    }, 3000);

    // Start heartbeat (every 30 seconds)
    this.heartbeatInterval = setInterval(() => {
      this.sendHeartbeats();
    }, 30000);

    // Start config sync (every 5 minutes)
    this.syncInterval = setInterval(() => {
      this.syncConfiguration();
    }, 300000);

    // Send initial heartbeat
    setTimeout(() => this.sendHeartbeats(), 2000);
  }

  stop() {
    if (this.heartbeatInterval) clearInterval(this.heartbeatInterval);
    if (this.syncInterval) clearInterval(this.syncInterval);
    this.logger.info('Cluster manager stopped');
  }

  async discoverPeers() {
    this.logger.info('Discovering cluster peers...');

    for (const peer of this.peers) {
      try {
        const response = await axios.get(`${peer.url}/cluster/info`, {
          timeout: 5000,
          headers: { 'X-Cluster-Auth': this.generateSignature('GET:/cluster/info') }
        });

        peer.name = response.data.nodeName || peer.name;
        peer.healthy = true; // Mark as healthy if discovery succeeds
        peer.lastHeartbeat = new Date();
        this.logger.info(`Discovered peer: ${peer.name} (${peer.id})`);
      } catch (err) {
        peer.healthy = false;
        this.logger.warn(`Failed to discover peer ${peer.id}: ${err.message}`);
      }
    }
  }

  async sendHeartbeats() {
    for (const peer of this.peers) {
      try {
        const start = Date.now();
        const payload = this.getLocalHealth();

        const response = await axios.post(`${peer.url}/cluster/heartbeat`, payload, {
          timeout: 10000,
          headers: {
            'Content-Type': 'application/json',
            'X-Cluster-Auth': this.generateSignature(`POST:/cluster/heartbeat:${JSON.stringify(payload)}`)
          }
        });

        const latency = Date.now() - start;

        peer.healthy = response.data.status === 'healthy';
        peer.lastHeartbeat = new Date();
        peer.latency = latency;
        peer.providers = response.data.providers?.length || 0;
        peer.healthyProviders = response.data.providers?.filter(p => p.healthy).length || 0;

        this.peerHealth.set(peer.id, {
          healthy: true,
          timestamp: new Date(),
          data: response.data
        });

        this.emit('peer.healthy', peer);
      } catch (err) {
        peer.healthy = false;
        peer.lastHeartbeat = null;

        this.peerHealth.set(peer.id, {
          healthy: false,
          timestamp: new Date(),
          error: err.message
        });

        this.emit('peer.unhealthy', peer);
      }
    }
  }

  getLocalHealth() {
    const uptime = process.uptime() * 1000;

    const providers = this.config.providers || [];
    const stats = this.config.stats || {};

    // Calculate provider health
    const providerHealth = providers.map(p => {
      const providerStats = stats[p.id] || {};
      const total = (providerStats.success || 0) + (providerStats.failure || 0);
      const successRate = total > 0 ? (providerStats.success || 0) / total : 0;

      return {
        id: p.id,
        name: p.name,
        type: p.type,
        enabled: p.enabled,
        healthy: p.enabled && successRate > 0.5,
        priority: p.priority || 99,
        lastSuccess: providerStats.lastSuccess || null
      };
    });

    // Calculate overall stats
    const totalRequests = Object.values(stats).reduce((sum, s) => sum + (s.requests || 0), 0);
    const successfulRequests = Object.values(stats).reduce((sum, s) => sum + (s.success || 0), 0);
    const failedRequests = Object.values(stats).reduce((sum, s) => sum + (s.failure || 0), 0);

    const latencies = Object.values(stats)
      .map(s => s.latency || [])
      .flat()
      .filter(l => l > 0);
    const averageLatency = latencies.length > 0
      ? latencies.reduce((a, b) => a + b, 0) / latencies.length
      : 0;

    return {
      nodeId: this.nodeId,
      nodeName: this.nodeName,
      status: 'healthy',
      uptime: uptime,
      providers: providerHealth,
      stats: {
        totalRequests,
        successfulRequests,
        failedRequests,
        averageLatency: Math.round(averageLatency)
      },
      timestamp: new Date().toISOString()
    };
  }

  getClusterStatus() {
    const localHealth = this.getLocalHealth();

    const peers = this.peers.map(peer => ({
      id: peer.id,
      name: peer.name,
      url: peer.url,
      status: peer.healthy ? 'healthy' : 'unhealthy',
      lastHeartbeat: peer.lastHeartbeat ? peer.lastHeartbeat.toISOString() : null,
      providers: peer.providers,
      healthyProviders: peer.healthyProviders,
      latency: peer.latency
    }));

    const healthyNodes = peers.filter(p => p.status === 'healthy').length + 1; // +1 for local

    return {
      clusterEnabled: this.enabled,
      localNode: {
        id: localHealth.nodeId,
        name: localHealth.nodeName,
        url: this.getLocalUrl(),
        status: localHealth.status,
        providers: localHealth.providers.length,
        healthyProviders: localHealth.providers.filter(p => p.healthy).length
      },
      peers: peers,
      totalNodes: peers.length + 1,
      healthyNodes: healthyNodes,
      lastSync: this.lastSync.toISOString()
    };
  }

  getLocalUrl() {
    // Try to determine our own URL from environment
    const host = process.env.CLUSTER_NODE_URL || `http://${this.nodeId}:${process.env.PORT || 3000}`;
    return host;
  }

  async syncConfiguration() {
    if (!this.enabled) return;

    this.logger.info('Starting configuration sync...');

    for (const peer of this.peers) {
      if (!peer.healthy) continue;

      try {
        // Pull configuration from peer
        const payload = { sourceNode: this.nodeId, timestamp: new Date().toISOString() };
        const response = await axios.get(`${peer.url}/cluster/config`, {
          timeout: 15000,
          headers: {
            'X-Cluster-Auth': this.generateSignature(`GET:/cluster/config:${JSON.stringify(payload)}`)
          },
          params: payload
        });

        if (response.data.success) {
          this.mergeConfiguration(response.data.config, peer.id);
          this.lastSync = new Date();
          this.emit('sync.success', { peer: peer.id });
        }
      } catch (err) {
        this.logger.error(`Sync failed with peer ${peer.id}: ${err.message}`);
        this.emit('sync.failure', { peer: peer.id, error: err.message });
      }
    }
  }

  mergeConfiguration(remoteConfig, peerId) {
    this.logger.info(`Merging configuration from peer: ${peerId}`);

    let changes = 0;

    // Merge users (union, no duplicates by username)
    if (remoteConfig.users) {
      const localUsernames = new Set(this.config.users.map(u => u.username));

      for (const user of remoteConfig.users) {
        if (!localUsernames.has(user.username)) {
          this.config.users.push(user);
          changes++;
          this.logger.info(`Added user from peer: ${user.username}`);
        }
      }
    }

    // Merge API keys (union, no duplicates by key)
    if (remoteConfig.clientApiKeys) {
      const localKeys = new Set(this.config.clientApiKeys.map(k => k.key));

      for (const apiKey of remoteConfig.clientApiKeys) {
        if (!localKeys.has(apiKey.key)) {
          this.config.clientApiKeys.push(apiKey);
          changes++;
          this.logger.info(`Added API key from peer: ${apiKey.name}`);
        }
      }
    }

    // Merge providers (union, no duplicates by ID)
    if (remoteConfig.providers) {
      const localProviderIds = new Set(this.config.providers.map(p => p.id));

      for (const provider of remoteConfig.providers) {
        if (!localProviderIds.has(provider.id)) {
          this.config.providers.push(provider);
          changes++;
          this.logger.info(`Added provider from peer: ${provider.name}`);
        }
      }
    }

    // Merge activity log (optional, can be disabled)
    if (process.env.CLUSTER_SYNC_ACTIVITY_LOG === 'true' && remoteConfig.activityLog) {
      const localLogIds = new Set(this.config.activityLog.map(l => l.id));

      for (const logEntry of remoteConfig.activityLog) {
        if (!localLogIds.has(logEntry.id)) {
          this.config.activityLog.push(logEntry);
          changes++;
        }
      }

      // Sort activity log by timestamp
      this.config.activityLog.sort((a, b) =>
        new Date(b.timestamp) - new Date(a.timestamp)
      );

      // Keep only last 1000 entries
      if (this.config.activityLog.length > 1000) {
        this.config.activityLog = this.config.activityLog.slice(0, 1000);
      }
    }

    if (changes > 0) {
      this.emit('config.merged', { peer: peerId, changes });
      this.logger.info(`Configuration merged: ${changes} changes from ${peerId}`);
    }
  }

  pushConfiguration(targetPeerId) {
    // Push our config to specific peer (called when we make local changes)
    const peer = this.peers.find(p => p.id === targetPeerId);
    if (!peer || !peer.healthy) return;

    const payload = {
      sourceNode: this.nodeId,
      timestamp: new Date().toISOString(),
      data: {
        users: this.config.users,
        clientApiKeys: this.config.clientApiKeys,
        providers: this.config.providers,
        activityLog: process.env.CLUSTER_SYNC_ACTIVITY_LOG === 'true'
          ? this.config.activityLog
          : []
      }
    };

    axios.post(`${peer.url}/cluster/sync`, payload, {
      timeout: 15000,
      headers: {
        'Content-Type': 'application/json',
        'X-Cluster-Auth': this.generateSignature(`POST:/cluster/sync:${JSON.stringify(payload)}`)
      }
    })
    .then(() => {
      this.logger.info(`Config pushed to peer: ${targetPeerId}`);
      this.emit('push.success', { peer: targetPeerId });
    })
    .catch(err => {
      this.logger.error(`Config push failed to ${targetPeerId}: ${err.message}`);
      this.emit('push.failure', { peer: targetPeerId, error: err.message });
    });
  }

  broadcastConfiguration() {
    // Push config to all healthy peers
    for (const peer of this.peers) {
      if (peer.healthy) {
        this.pushConfiguration(peer.id);
      }
    }
  }

  generateSignature(payload) {
    if (!this.syncSecret) return '';

    const hmac = crypto.createHmac('sha256', this.syncSecret);
    hmac.update(payload);
    return hmac.digest('hex');
  }

  verifySignature(payload, signature) {
    if (!this.syncSecret) return true; // No auth required if no secret set

    const expected = this.generateSignature(payload);
    return crypto.timingSafeEqual(
      Buffer.from(expected, 'hex'),
      Buffer.from(signature, 'hex')
    );
  }

  // Express middleware for cluster authentication
  authenticateCluster(req, res, next) {
    if (!this.enabled) {
      return res.status(503).json({ error: 'Cluster mode disabled' });
    }

    const signature = req.headers['x-cluster-auth'];
    if (!signature) {
      return res.status(401).json({ error: 'Missing cluster authentication' });
    }

    const payload = `${req.method}:${req.path}:${req.method === 'POST' ? JSON.stringify(req.body) : ''}`;

    if (!this.verifySignature(payload, signature)) {
      return res.status(403).json({ error: 'Invalid cluster signature' });
    }

    next();
  }
}

module.exports = ClusterManager;
