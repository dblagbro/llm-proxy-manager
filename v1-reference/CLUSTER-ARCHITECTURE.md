# LLM Proxy Manager - Cluster Architecture Design

## Overview

This document describes the cluster synchronization architecture for multi-instance deployment of LLM Proxy Manager, providing redundancy-within-redundancy for maximum availability.

## Architecture Goals

1. **Multi-Instance Deployment**: Deploy to multiple servers (TMRwww01, TMRwww02, c1conversations-avaya-01)
2. **Configuration Synchronization**: Share provider configurations, users, and API keys across cluster
3. **Health Monitoring**: Each node reports health and provider status
4. **Cluster Discovery**: Automatic peer detection and registration
5. **Redundancy Layers**:
   - **Layer 1**: Applications fail over between multiple proxy instances
   - **Layer 2**: Each proxy instance fails over between multiple LLM providers
6. **Independent Configuration**: Each node can have unique provider priority ordering

## Cluster Communication Protocol

### 1. Cluster Member Registration

Each node maintains a list of cluster peers and announces itself on startup:

```javascript
clusterConfig: {
  enabled: true,
  nodeId: "tmrwww01",
  nodeName: "TMRwww01 LLM Proxy",
  peers: [
    {
      id: "tmrwww02",
      name: "TMRwww02 LLM Proxy",
      url: "http://tmrwww02:3000",
      priority: 2
    },
    {
      id: "c1conversations-avaya-01",
      name: "C1 Conversations Hub LLM Proxy",
      url: "http://c1conversations-avaya-01:3000",
      priority: 3
    }
  ],
  syncSecret: "shared-secret-for-cluster-auth"
}
```

### 2. Heartbeat and Health Checks

**Heartbeat Interval**: Every 30 seconds
- Each node sends heartbeat to all peers
- Includes: node health, provider status, timestamp
- Peers update their cluster status cache

**Health Check Endpoint**: `GET /cluster/health`

Response:
```json
{
  "nodeId": "tmrwww01",
  "nodeName": "TMRwww01 LLM Proxy",
  "status": "healthy",
  "uptime": 3600000,
  "providers": [
    {
      "id": "anthropic-1",
      "name": "Anthropic Claude #1",
      "enabled": true,
      "healthy": true,
      "priority": 1,
      "lastSuccess": "2026-03-27T01:00:00Z"
    }
  ],
  "stats": {
    "totalRequests": 150,
    "successfulRequests": 148,
    "failedRequests": 2,
    "averageLatency": 1200
  },
  "timestamp": "2026-03-27T01:30:00Z"
}
```

### 3. Configuration Synchronization

**Sync Modes**:
- **Push**: Node pushes config changes to peers
- **Pull**: Node requests latest config from peers
- **Merge**: Combines configurations with conflict resolution

**Synced Data**:
- Users (admin accounts, passwords)
- API Keys (generated client keys)
- Activity Log (optional, configurable)

**NOT Synced** (node-specific):
- Provider configurations (each node has unique ordering)
- Provider statistics (local performance metrics)
- Session data

**Sync Endpoint**: `POST /cluster/sync`

Request:
```json
{
  "sourceNode": "tmrwww01",
  "timestamp": "2026-03-27T01:30:00Z",
  "signature": "hmac-sha256-signature",
  "data": {
    "users": [...],
    "clientApiKeys": [...],
    "activityLog": [...]
  }
}
```

### 4. Conflict Resolution

When configurations diverge:
- **Timestamp wins**: Most recent change takes precedence
- **User merge**: Union of all users (no duplicates by username)
- **API Key merge**: Union of all keys (no duplicates by key ID)
- **Activity log**: Merge and sort by timestamp

### 5. Cluster Status for Client Applications

**Cluster Status Endpoint**: `GET /cluster/status`

Returns list of all cluster members with health status:

```json
{
  "clusterEnabled": true,
  "localNode": {
    "id": "tmrwww01",
    "name": "TMRwww01 LLM Proxy",
    "url": "http://tmrwww01:3000",
    "status": "healthy",
    "providers": 3,
    "healthyProviders": 2
  },
  "peers": [
    {
      "id": "tmrwww02",
      "name": "TMRwww02 LLM Proxy",
      "url": "http://tmrwww02:3000",
      "status": "healthy",
      "lastHeartbeat": "2026-03-27T01:29:45Z",
      "providers": 3,
      "healthyProviders": 3,
      "latency": 15
    },
    {
      "id": "c1conversations-avaya-01",
      "name": "C1 Conversations Hub LLM Proxy",
      "url": "http://c1conversations-avaya-01:3000",
      "status": "degraded",
      "lastHeartbeat": "2026-03-27T01:28:30Z",
      "providers": 2,
      "healthyProviders": 1,
      "latency": 250
    }
  ],
  "totalNodes": 3,
  "healthyNodes": 2
}
```

## Client Application Integration

Applications using the LLM Proxy should implement their own failover logic:

### Option 1: Ordered List with Health Checks

```javascript
const proxyUrls = [
  "http://tmrwww01:3000/v1/messages",
  "http://tmrwww02:3000/v1/messages",
  "http://c1conversations-avaya-01:3000/v1/messages"
];

async function makeRequest(prompt) {
  for (const url of proxyUrls) {
    try {
      // Check health first
      const health = await fetch(`${url.replace('/v1/messages', '')}/health`);
      if (!health.ok) continue;

      // Make actual request
      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'x-api-key': 'your-api-key',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ model: 'claude-sonnet-4-5-20250929', messages: [prompt] })
      });

      if (response.ok) return response;
    } catch (err) {
      console.warn(`Proxy ${url} failed, trying next...`);
    }
  }
  throw new Error('All proxies unavailable');
}
```

### Option 2: Dynamic Health-Based Selection

```javascript
async function selectBestProxy() {
  const primaryProxy = "http://tmrwww01:3000";

  try {
    const clusterStatus = await fetch(`${primaryProxy}/cluster/status`).then(r => r.json());

    // Find healthiest node
    const allNodes = [clusterStatus.localNode, ...clusterStatus.peers];
    const healthyNodes = allNodes
      .filter(n => n.status === 'healthy')
      .sort((a, b) => b.healthyProviders - a.healthyProviders);

    return healthyNodes[0]?.url || primaryProxy;
  } catch (err) {
    return primaryProxy; // fallback
  }
}
```

## Security Considerations

1. **Cluster Authentication**: All inter-node communication requires shared secret (HMAC signature)
2. **TLS**: In production, use HTTPS for all cluster communication
3. **Firewall**: Restrict cluster endpoints to trusted IPs only
4. **Secret Rotation**: Support for rotating cluster sync secrets without downtime

## Configuration Example

### Node 1: TMRwww01 (Primary)
```yaml
# .env
NODE_ENV=production
PORT=3000
SESSION_SECRET=random-secret-1

# Cluster Configuration
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=tmrwww01
CLUSTER_NODE_NAME="TMRwww01 LLM Proxy"
CLUSTER_SYNC_SECRET=shared-cluster-secret
CLUSTER_PEERS=tmrwww02:http://tmrwww02:3000,c1conversations-avaya-01:http://c1conversations-avaya-01:3000

# Provider Priority for this node
ANTHROPIC_KEY_1=sk-ant-api03-...
GOOGLE_API_KEY_1=AIzaSy...
OPENAI_KEY_1=sk-...
```

### Node 2: TMRwww02 (Secondary)
```yaml
# .env
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=tmrwww02
CLUSTER_NODE_NAME="TMRwww02 LLM Proxy"
CLUSTER_SYNC_SECRET=shared-cluster-secret
CLUSTER_PEERS=tmrwww01:http://tmrwww01:3000,c1conversations-avaya-01:http://c1conversations-avaya-01:3000

# Different provider priority
GOOGLE_API_KEY_1=AIzaSy...
ANTHROPIC_KEY_1=sk-ant-api03-...
OPENAI_KEY_1=sk-...
```

### Node 3: c1conversations-avaya-01 (Hub)
```yaml
# .env
CLUSTER_ENABLED=true
CLUSTER_NODE_ID=c1conversations-avaya-01
CLUSTER_NODE_NAME="C1 Conversations Hub LLM Proxy"
CLUSTER_SYNC_SECRET=shared-cluster-secret
CLUSTER_PEERS=tmrwww01:http://tmrwww01:3000,tmrwww02:http://tmrwww02:3000

# Yet another provider priority
OPENAI_KEY_1=sk-...
GOOGLE_API_KEY_1=AIzaSy...
ANTHROPIC_KEY_1=sk-ant-api03-...
```

## Deployment Topology

```
┌─────────────────────────────────────────────────────────────────┐
│                    Client Applications                           │
│           (Custom failover order: 1→2→3 or 1→3→2)               │
└────────────┬──────────────┬──────────────┬───────────────────────┘
             │              │              │
             ▼              ▼              ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │  TMRwww01  │  │  TMRwww02  │  │ C1-Avaya-01│
    │LLM Proxy #1│  │LLM Proxy #2│  │LLM Proxy #3│
    └────────────┘  └────────────┘  └────────────┘
         │  ◄──── Cluster Sync ────►  │       │
         │  ◄──── Health Checks ──────┘       │
         └──────── Config Merge ──────────────┘
         │              │              │
         ▼              ▼              ▼
    ┌────────────┐  ┌────────────┐  ┌────────────┐
    │Provider A  │  │Provider B  │  │Provider C  │
    │Priority: 1 │  │Priority: 1 │  │Priority: 1 │
    ├────────────┤  ├────────────┤  ├────────────┤
    │Provider B  │  │Provider A  │  │Provider A  │
    │Priority: 2 │  │Priority: 2 │  │Priority: 2 │
    ├────────────┤  ├────────────┤  ├────────────┤
    │Provider C  │  │Provider C  │  │Provider B  │
    │Priority: 3 │  │Priority: 3 │  │Priority: 3 │
    └────────────┘  └────────────┘  └────────────┘
```

## Monitoring and Observability

### Metrics to Track

1. **Cluster Health**:
   - Number of healthy nodes
   - Last successful sync timestamp
   - Sync failures count

2. **Per-Node Metrics**:
   - Uptime
   - Total requests served
   - Provider success/failure rates
   - Average response latency

3. **Cluster-Wide Metrics**:
   - Total capacity (sum of healthy providers across nodes)
   - Geographic distribution
   - Load distribution

### Activity Log Entries

Cluster-specific activity log entries:
- `cluster.node.joined` - Peer node connected
- `cluster.node.left` - Peer node disconnected
- `cluster.sync.success` - Configuration synchronized
- `cluster.sync.failure` - Sync failed (with reason)
- `cluster.health.degraded` - Node health degraded
- `cluster.health.recovered` - Node health recovered

## Failure Scenarios and Recovery

### Scenario 1: Single Node Failure
- Clients automatically fail over to remaining nodes
- Cluster continues operating with reduced capacity
- When node recovers, it pulls latest config from peers

### Scenario 2: Network Partition
- Nodes continue serving requests with local config
- When partition heals, configs are merged using timestamp resolution
- Activity logs merged to prevent data loss

### Scenario 3: Complete Cluster Failure
- Each node operates independently
- No config sync until at least one peer is available
- Human intervention may be needed to resolve conflicts

### Scenario 4: Split Brain
- Each partition continues independently
- On merge, timestamp-based conflict resolution applies
- Manual review of activity log recommended

## Future Enhancements

1. **Distributed Consensus**: Use Raft or similar for stronger consistency
2. **Load Balancing**: Built-in load balancer for even distribution
3. **Geographic Awareness**: Route to nearest healthy node
4. **Provider Pool Sharing**: Share API keys across cluster (with rate limiting)
5. **Metrics Aggregation**: Centralized metrics dashboard for entire cluster
6. **Auto-Scaling**: Spin up new nodes based on load
