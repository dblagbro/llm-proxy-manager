#!/usr/bin/env python3
"""
Quick Win Package Applicator for v1.1.0
Applies cost tracking and circuit breaker UI changes
"""

import re
import sys

print("🚀 Applying Quick Win Package changes...")
print()

# Read server.js
print("📖 Reading server.js...")
with open('src/server.js', 'r') as f:
    server_content = f.read()

# Step 3: Add cost tracking to success handler in /v1/messages
print("✏️  Adding cost tracking to request success handler...")

# Find the success handler block (around line 687-705)
success_pattern = r'(config\.stats\[provider\.id\]\.lastSuccess = \{[^}]+\};)'
success_replacement = r'''\1

      // Track costs
      const model = req.body.model || 'claude-sonnet-4-5-20250929';
      const usage = result.usage || {};
      const cost = pricingManager.calculateCost(
        model,
        usage.input_tokens || 0,
        usage.output_tokens || 0
      );

      config.stats[provider.id].totalCost += cost;
      config.stats[provider.id].totalInputTokens += (usage.input_tokens || 0);
      config.stats[provider.id].totalOutputTokens += (usage.output_tokens || 0);'''

server_content = re.sub(success_pattern, success_replacement, server_content, count=1)

# Step 4: Add new API endpoints before /health
print("✏️  Adding new API endpoints...")

health_pattern = r"(// Health check\napp\.get\('/health')"
new_endpoints = r'''// Provider capabilities endpoint
app.get('/api/capabilities/:providerType', (req, res) => {
  const { providerType } = req.params;
  const capabilities = pricingManager.getCapabilities(providerType);
  res.json(capabilities);
});

// Available models endpoint
app.get('/api/models/:providerType', (req, res) => {
  const { providerType } = req.params;
  const models = pricingManager.getModelsForProvider(providerType);
  res.json({ models });
});

// Pricing info endpoint
app.get('/api/pricing/:model', (req, res) => {
  const { model } = req.params;
  const pricing = pricingManager.getPricing(model);

  if (!pricing) {
    return res.status(404).json({ error: 'Model not found' });
  }

  res.json({
    model,
    ...pricing,
    costPer1M: pricingManager.getCostPer1M(model)
  });
});

// Circuit breaker status endpoint
app.get('/api/circuit-status', requireAuth, (req, res) => {
  const statuses = {};

  for (const provider of config.providers) {
    const circuit = providerMonitor.getCircuitState(provider.id);
    statuses[provider.id] = {
      providerName: provider.name,
      state: circuit.state,
      failures: circuit.failures,
      successes: circuit.successes,
      lastFailure: circuit.lastFailure,
      reason: circuit.reason
    };
  }

  res.json(statuses);
});

\1'''

server_content = re.sub(health_pattern, new_endpoints, server_content, count=1)

# Step 5: Update /api/stats endpoint to include cost calculations
print("✏️  Updating /api/stats endpoint...")

stats_pattern = r"app\.get\('/api/stats', \(req, res\) => \{\s+res\.json\(config\.stats\);\s+\}\);"
stats_replacement = '''app.get('/api/stats', (req, res) => {
  // Add cost calculations to stats
  const enhancedStats = {};

  for (const [providerId, stats] of Object.entries(config.stats)) {
    const provider = config.providers.find(p => p.id === providerId);

    enhancedStats[providerId] = {
      ...stats,
      avgLatency: stats.requests > 0 ? stats.totalLatency / stats.requests : 0,
      successRate: stats.requests > 0 ? (stats.successes / stats.requests) * 100 : 0,
      avgCost: stats.requests > 0 ? stats.totalCost / stats.requests : 0,
      costPer1MTokens: provider ? pricingManager.getCostPer1M(provider.model) : null
    };
  }

  res.json(enhancedStats);
});'''

server_content = re.sub(stats_pattern, stats_replacement, server_content, count=1, flags=re.DOTALL)

# Write updated server.js
print("💾 Writing updated server.js...")
with open('src/server.js', 'w') as f:
    f.write(server_content)

print("✅ Server.js updated successfully!")
print()

# Update UI
print("📖 Reading index.html...")
with open('public/index.html', 'r') as f:
    html_content = f.read()

# Step 6: Update version to v1.1.0
print("✏️  Updating version to v1.1.0...")
html_content = re.sub(r'v1\.0\.6', 'v1.1.0', html_content)

print("💾 Writing updated index.html...")
with open('public/index.html', 'w') as f:
    f.write(html_content)

print("✅ Index.html updated successfully!")
print()

print("🎉 Quick Win Package applied successfully!")
print()
print("Next steps:")
print("  1. Test the changes locally")
print("  2. Build Docker image v1.1.0")
print("  3. Deploy to servers")
print()
print("Note: UI enhancements for cost/circuit display will be added in next phase")
