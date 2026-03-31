/**
 * SQLite Database Layer for LLM Proxy Manager
 *
 * Drop-in replacement for the flat providers.json config store.
 * Exposes the same `config` object shape and loadConfig/saveConfig
 * interface that server.js already uses, so the rest of the app
 * doesn't need to change at all.
 *
 * Tables:
 *   providers      — one row per provider
 *   users          — one row per user
 *   client_api_keys — one row per API key
 *   stats          — one row per provider (counters)
 *   activity_log   — append-only ring buffer (max 1000 rows)
 *   kv             — catch-all key/value store for cluster, smtp, etc.
 *
 * Enable with env var:  USE_SQLITE=true
 * DB path:              /app/config/llm-proxy.db  (same volume as providers.json)
 */

'use strict';

const BetterSqlite3 = require('better-sqlite3');
const path = require('path');
const crypto = require('crypto');

const DB_PATH = process.env.DB_PATH || '/app/config/llm-proxy.db';
const ACTIVITY_LOG_MAX = 1000;

let db;

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------
const SCHEMA = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS providers (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  type        TEXT NOT NULL,
  api_key     TEXT,
  enabled     INTEGER NOT NULL DEFAULT 1,
  priority    INTEGER NOT NULL DEFAULT 999,
  project_id  TEXT,
  location    TEXT,
  base_url    TEXT,
  model       TEXT,
  circuit_breaker TEXT,   -- JSON blob for per-provider CB overrides
  extra       TEXT        -- JSON blob for any future fields
);

CREATE TABLE IF NOT EXISTS users (
  id          TEXT PRIMARY KEY,
  username    TEXT NOT NULL UNIQUE,
  password    TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'user',
  email       TEXT,
  theme       TEXT DEFAULT 'dark',
  reset_token TEXT,
  reset_token_expiry INTEGER,
  created     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS client_api_keys (
  id          TEXT PRIMARY KEY,
  key_value   TEXT NOT NULL UNIQUE,
  name        TEXT NOT NULL,
  created     TEXT NOT NULL,
  last_used   TEXT,
  requests    INTEGER NOT NULL DEFAULT 0,
  enabled     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS stats (
  provider_id       TEXT PRIMARY KEY,
  requests          INTEGER NOT NULL DEFAULT 0,
  successes         INTEGER NOT NULL DEFAULT 0,
  failures          INTEGER NOT NULL DEFAULT 0,
  total_latency     INTEGER NOT NULL DEFAULT 0,
  total_cost        REAL    NOT NULL DEFAULT 0,
  total_input_tokens  INTEGER NOT NULL DEFAULT 0,
  total_output_tokens INTEGER NOT NULL DEFAULT 0,
  last_used         TEXT,
  last_error        TEXT,   -- JSON blob {message, timestamp}
  last_success      TEXT    -- JSON blob {timestamp}
);

CREATE TABLE IF NOT EXISTS activity_log (
  id          TEXT PRIMARY KEY,
  timestamp   TEXT NOT NULL,
  type        TEXT NOT NULL,
  message     TEXT NOT NULL,
  details     TEXT          -- JSON blob for extra fields
);

CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL       -- JSON
);
`;

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------
function initDb() {
  db = new BetterSqlite3(DB_PATH, { verbose: null });
  db.exec(SCHEMA);
  return db;
}

// ---------------------------------------------------------------------------
// Providers
// ---------------------------------------------------------------------------
const stmts = {};

function prepareStatements() {
  stmts.getAllProviders = db.prepare('SELECT * FROM providers ORDER BY priority ASC');
  stmts.upsertProvider = db.prepare(`
    INSERT INTO providers (id, name, type, api_key, enabled, priority, project_id, location, base_url, model, circuit_breaker, extra)
    VALUES (@id, @name, @type, @api_key, @enabled, @priority, @project_id, @location, @base_url, @model, @circuit_breaker, @extra)
    ON CONFLICT(id) DO UPDATE SET
      name=excluded.name, type=excluded.type, api_key=excluded.api_key,
      enabled=excluded.enabled, priority=excluded.priority,
      project_id=excluded.project_id, location=excluded.location,
      base_url=excluded.base_url, model=excluded.model,
      circuit_breaker=excluded.circuit_breaker, extra=excluded.extra
  `);
  stmts.deleteProvider = db.prepare('DELETE FROM providers WHERE id = ?');
  stmts.deleteAllProviders = db.prepare('DELETE FROM providers');

  stmts.getAllUsers = db.prepare('SELECT * FROM users ORDER BY created ASC');
  stmts.upsertUser = db.prepare(`
    INSERT INTO users (id, username, password, role, email, theme, reset_token, reset_token_expiry, created)
    VALUES (@id, @username, @password, @role, @email, @theme, @reset_token, @reset_token_expiry, @created)
    ON CONFLICT(id) DO UPDATE SET
      username=excluded.username, password=excluded.password, role=excluded.role,
      email=excluded.email, theme=excluded.theme,
      reset_token=excluded.reset_token, reset_token_expiry=excluded.reset_token_expiry
  `);
  stmts.deleteUser = db.prepare('DELETE FROM users WHERE id = ?');

  stmts.getAllKeys = db.prepare('SELECT * FROM client_api_keys ORDER BY created ASC');
  stmts.upsertKey = db.prepare(`
    INSERT INTO client_api_keys (id, key_value, name, created, last_used, requests, enabled)
    VALUES (@id, @key_value, @name, @created, @last_used, @requests, @enabled)
    ON CONFLICT(id) DO UPDATE SET
      key_value=excluded.key_value, name=excluded.name,
      last_used=excluded.last_used, requests=excluded.requests, enabled=excluded.enabled
  `);
  stmts.deleteKey = db.prepare('DELETE FROM client_api_keys WHERE id = ?');

  stmts.getAllStats = db.prepare('SELECT * FROM stats');
  stmts.upsertStats = db.prepare(`
    INSERT INTO stats (provider_id, requests, successes, failures, total_latency, total_cost,
      total_input_tokens, total_output_tokens, last_used, last_error, last_success)
    VALUES (@provider_id, @requests, @successes, @failures, @total_latency, @total_cost,
      @total_input_tokens, @total_output_tokens, @last_used, @last_error, @last_success)
    ON CONFLICT(provider_id) DO UPDATE SET
      requests=excluded.requests, successes=excluded.successes, failures=excluded.failures,
      total_latency=excluded.total_latency, total_cost=excluded.total_cost,
      total_input_tokens=excluded.total_input_tokens, total_output_tokens=excluded.total_output_tokens,
      last_used=excluded.last_used, last_error=excluded.last_error, last_success=excluded.last_success
  `);
  stmts.deleteStats = db.prepare('DELETE FROM stats WHERE provider_id = ?');

  stmts.getRecentActivity = db.prepare(`SELECT * FROM activity_log ORDER BY timestamp DESC LIMIT ?`);
  stmts.insertActivity = db.prepare(`
    INSERT OR REPLACE INTO activity_log (id, timestamp, type, message, details)
    VALUES (@id, @timestamp, @type, @message, @details)
  `);
  stmts.countActivity = db.prepare('SELECT COUNT(*) as cnt FROM activity_log');
  stmts.pruneActivity = db.prepare(`
    DELETE FROM activity_log WHERE id IN (
      SELECT id FROM activity_log ORDER BY timestamp ASC LIMIT ?
    )
  `);

  stmts.getKv = db.prepare('SELECT value FROM kv WHERE key = ?');
  stmts.setKv = db.prepare('INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)');
}

// ---------------------------------------------------------------------------
// Row ↔ JS object converters
// ---------------------------------------------------------------------------
function rowToProvider(row) {
  const p = {
    id: row.id,
    name: row.name,
    type: row.type,
    apiKey: row.api_key || undefined,
    enabled: row.enabled === 1,
    priority: row.priority,
  };
  if (row.project_id) p.projectId = row.project_id;
  if (row.location)   p.location  = row.location;
  if (row.base_url)   p.baseUrl   = row.base_url;
  if (row.model)      p.model     = row.model;
  if (row.circuit_breaker) {
    try { p.circuitBreaker = JSON.parse(row.circuit_breaker); } catch (_) {}
  }
  if (row.extra) {
    try { Object.assign(p, JSON.parse(row.extra)); } catch (_) {}
  }
  return p;
}

function providerToRow(p) {
  // Put known fields in columns, everything else in extra
  const knownKeys = new Set(['id','name','type','apiKey','enabled','priority','projectId','location','baseUrl','model','circuitBreaker']);
  const extra = {};
  for (const [k, v] of Object.entries(p)) {
    if (!knownKeys.has(k)) extra[k] = v;
  }
  return {
    id: p.id,
    name: p.name,
    type: p.type,
    api_key: p.apiKey || null,
    enabled: p.enabled ? 1 : 0,
    priority: p.priority || 999,
    project_id: p.projectId || null,
    location: p.location || null,
    base_url: p.baseUrl || null,
    model: p.model || null,
    circuit_breaker: p.circuitBreaker ? JSON.stringify(p.circuitBreaker) : null,
    extra: Object.keys(extra).length > 0 ? JSON.stringify(extra) : null,
  };
}

function rowToUser(row) {
  return {
    id: row.id,
    username: row.username,
    password: row.password,
    role: row.role,
    email: row.email || undefined,
    theme: row.theme || 'dark',
    resetToken: row.reset_token || undefined,
    resetTokenExpiry: row.reset_token_expiry || undefined,
    created: row.created,
  };
}

function userToRow(u) {
  return {
    id: u.id,
    username: u.username,
    password: u.password,
    role: u.role || 'user',
    email: u.email || null,
    theme: u.theme || 'dark',
    reset_token: u.resetToken || null,
    reset_token_expiry: u.resetTokenExpiry || null,
    created: u.created || new Date().toISOString(),
  };
}

function rowToKey(row) {
  return {
    id: row.id,
    key: row.key_value,
    name: row.name,
    created: row.created,
    lastUsed: row.last_used || null,
    requests: row.requests || 0,
    enabled: row.enabled === 1,
  };
}

function keyToRow(k) {
  return {
    id: k.id,
    key_value: k.key,
    name: k.name,
    created: k.created || new Date().toISOString(),
    last_used: k.lastUsed || null,
    requests: k.requests || 0,
    enabled: k.enabled !== false ? 1 : 0,
  };
}

function rowToStats(row) {
  let lastError = null, lastSuccess = null;
  try { if (row.last_error) lastError = JSON.parse(row.last_error); } catch (_) {}
  try { if (row.last_success) lastSuccess = JSON.parse(row.last_success); } catch (_) {}
  return {
    requests: row.requests || 0,
    successes: row.successes || 0,
    failures: row.failures || 0,
    totalLatency: row.total_latency || 0,
    totalCost: row.total_cost || 0,
    totalInputTokens: row.total_input_tokens || 0,
    totalOutputTokens: row.total_output_tokens || 0,
    lastUsed: row.last_used || null,
    lastError,
    lastSuccess,
  };
}

function statsToRow(providerId, s) {
  return {
    provider_id: providerId,
    requests: s.requests || 0,
    successes: s.successes || 0,
    failures: s.failures || 0,
    total_latency: s.totalLatency || 0,
    total_cost: s.totalCost || 0,
    total_input_tokens: s.totalInputTokens || 0,
    total_output_tokens: s.totalOutputTokens || 0,
    last_used: s.lastUsed || null,
    last_error: s.lastError ? JSON.stringify(s.lastError) : null,
    last_success: s.lastSuccess ? JSON.stringify(s.lastSuccess) : null,
  };
}

function rowToActivity(row) {
  let details = {};
  try { if (row.details) details = JSON.parse(row.details); } catch (_) {}
  return { id: row.id, timestamp: row.timestamp, type: row.type, message: row.message, ...details };
}

function activityToRow(entry) {
  const { id, timestamp, type, message, ...details } = entry;
  return {
    id,
    timestamp,
    type,
    message,
    details: Object.keys(details).length > 0 ? JSON.stringify(details) : null,
  };
}

// ---------------------------------------------------------------------------
// Load all data from DB into the config object
// ---------------------------------------------------------------------------
function loadFromDb(config) {
  // Providers
  config.providers = stmts.getAllProviders.all().map(rowToProvider);

  // Users
  config.users = stmts.getAllUsers.all().map(rowToUser);

  // Client API keys
  config.clientApiKeys = stmts.getAllKeys.all().map(rowToKey);

  // Stats — clear transient session fields on load
  config.stats = {};
  for (const row of stmts.getAllStats.all()) {
    const s = rowToStats(row);
    s.lastSuccess = null;  // clear stale status on startup
    s.lastError   = null;
    config.stats[row.provider_id] = s;
  }

  // Activity log (newest first, max 1000)
  config.activityLog = stmts.getRecentActivity.all(ACTIVITY_LOG_MAX).map(rowToActivity);

  // Cluster config
  const clusterRow = stmts.getKv.get('cluster');
  config.cluster = clusterRow ? JSON.parse(clusterRow.value) : {};

  // SMTP config
  const smtpRow = stmts.getKv.get('smtp');
  config.smtp = smtpRow ? JSON.parse(smtpRow.value) : {};
}

// ---------------------------------------------------------------------------
// Save all data from config object to DB (full sync)
// Used for bulk saves; hot-path writes use targeted functions below.
// ---------------------------------------------------------------------------
const saveAll = db => db.transaction((config) => {
  // Providers — full replace
  stmts.deleteAllProviders.run();
  for (const p of (config.providers || [])) {
    stmts.upsertProvider.run(providerToRow(p));
  }

  // Users
  const existingUserIds = new Set(stmts.getAllUsers.all().map(r => r.id));
  const newUserIds = new Set((config.users || []).map(u => u.id));
  for (const id of existingUserIds) {
    if (!newUserIds.has(id)) stmts.deleteUser.run(id);
  }
  for (const u of (config.users || [])) {
    stmts.upsertUser.run(userToRow(u));
  }

  // Client API keys
  const existingKeyIds = new Set(stmts.getAllKeys.all().map(r => r.id));
  const newKeyIds = new Set((config.clientApiKeys || []).map(k => k.id));
  for (const id of existingKeyIds) {
    if (!newKeyIds.has(id)) stmts.deleteKey.run(id);
  }
  for (const k of (config.clientApiKeys || [])) {
    stmts.upsertKey.run(keyToRow(k));
  }

  // Stats
  for (const [providerId, s] of Object.entries(config.stats || {})) {
    stmts.upsertStats.run(statsToRow(providerId, s));
  }

  // Activity log — only insert new entries (DB is the authoritative store)
  // For a full save we don't truncate existing entries; we just upsert what's in memory
  for (const entry of (config.activityLog || [])) {
    stmts.insertActivity.run(activityToRow(entry));
  }
  // Prune to ACTIVITY_LOG_MAX
  const { cnt } = stmts.countActivity.get();
  if (cnt > ACTIVITY_LOG_MAX) {
    stmts.pruneActivity.run(cnt - ACTIVITY_LOG_MAX);
  }

  // KV
  stmts.setKv.run('cluster', JSON.stringify(config.cluster || {}));
  stmts.setKv.run('smtp', JSON.stringify(config.smtp || {}));
});

// ---------------------------------------------------------------------------
// Targeted write helpers (for hot-path writes that don't need a full sync)
// ---------------------------------------------------------------------------

function saveProvider(provider) {
  stmts.upsertProvider.run(providerToRow(provider));
}

function deleteProvider(providerId) {
  stmts.deleteProvider.run(providerId);
  stmts.deleteStats.run(providerId);
}

function saveUser(user) {
  stmts.upsertUser.run(userToRow(user));
}

function deleteUser(userId) {
  stmts.deleteUser.run(userId);
}

function saveApiKey(key) {
  stmts.upsertKey.run(keyToRow(key));
}

function deleteApiKey(keyId) {
  stmts.deleteKey.run(keyId);
}

function saveStats(providerId, stats) {
  stmts.upsertStats.run(statsToRow(providerId, stats));
}

function clearStats() {
  db.prepare('DELETE FROM stats').run();
}

function appendActivityLog(entry) {
  stmts.insertActivity.run(activityToRow(entry));
  const { cnt } = stmts.countActivity.get();
  if (cnt > ACTIVITY_LOG_MAX) {
    stmts.pruneActivity.run(cnt - ACTIVITY_LOG_MAX);
  }
}

function saveCluster(clusterConfig) {
  stmts.setKv.run('cluster', JSON.stringify(clusterConfig || {}));
}

function saveSmtp(smtpConfig) {
  stmts.setKv.run('smtp', JSON.stringify(smtpConfig || {}));
}

// ---------------------------------------------------------------------------
// Migration: import from providers.json into SQLite
// ---------------------------------------------------------------------------
function migrateFromJson(jsonPath, logger) {
  const fs = require('fs');
  if (!fs.existsSync(jsonPath)) {
    if (logger) logger.warn(`Migration: ${jsonPath} not found, skipping`);
    return false;
  }

  let jsonConfig;
  try {
    jsonConfig = JSON.parse(fs.readFileSync(jsonPath, 'utf8'));
  } catch (err) {
    if (logger) logger.error(`Migration: failed to parse ${jsonPath}: ${err.message}`);
    return false;
  }

  if (logger) logger.info('Migration: importing providers.json → SQLite...');

  const migrate = db.transaction(() => {
    // Providers
    for (const p of (jsonConfig.providers || [])) {
      stmts.upsertProvider.run(providerToRow(p));
    }
    if (logger) logger.info(`Migration: imported ${(jsonConfig.providers||[]).length} providers`);

    // Users
    for (const u of (jsonConfig.users || [])) {
      stmts.upsertUser.run(userToRow(u));
    }
    if (logger) logger.info(`Migration: imported ${(jsonConfig.users||[]).length} users`);

    // Client API keys
    for (const k of (jsonConfig.clientApiKeys || [])) {
      stmts.upsertKey.run(keyToRow(k));
    }
    if (logger) logger.info(`Migration: imported ${(jsonConfig.clientApiKeys||[]).length} API keys`);

    // Stats
    for (const [pid, s] of Object.entries(jsonConfig.stats || {})) {
      stmts.upsertStats.run(statsToRow(pid, s));
    }
    if (logger) logger.info(`Migration: imported ${Object.keys(jsonConfig.stats||{}).length} stats records`);

    // Activity log
    for (const entry of (jsonConfig.activityLog || [])) {
      stmts.insertActivity.run(activityToRow(entry));
    }
    if (logger) logger.info(`Migration: imported ${(jsonConfig.activityLog||[]).length} activity log entries`);

    // Cluster config
    if (jsonConfig.cluster) {
      stmts.setKv.run('cluster', JSON.stringify(jsonConfig.cluster));
    }

    // SMTP config
    if (jsonConfig.smtp) {
      stmts.setKv.run('smtp', JSON.stringify(jsonConfig.smtp));
    }
  });

  try {
    migrate();
    if (logger) logger.info('Migration: providers.json → SQLite complete');
    return true;
  } catch (err) {
    if (logger) logger.error(`Migration: transaction failed: ${err.message}`);
    return false;
  }
}

// ---------------------------------------------------------------------------
// Check if DB already has data (to skip re-migration)
// ---------------------------------------------------------------------------
function isDbPopulated() {
  const row = db.prepare('SELECT COUNT(*) as cnt FROM providers').get();
  return row.cnt > 0;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------
module.exports = {
  /**
   * Open the database, run migrations, return an object with
   * the same interface as the JSON-based config store.
   */
  open(logger) {
    initDb();
    prepareStatements();

    // Auto-migrate from providers.json if DB is empty
    const jsonPath = process.env.CONFIG_PATH || '/app/config/providers.json';
    if (!isDbPopulated()) {
      migrateFromJson(jsonPath, logger);
    }

    return {
      // Transactions
      saveAll: (config) => saveAll(db)(config),

      // Targeted saves (preferred for hot paths)
      saveProvider,
      deleteProvider,
      saveUser,
      deleteUser,
      saveApiKey,
      deleteApiKey,
      saveStats,
      clearStats,
      appendActivityLog,
      saveCluster,
      saveSmtp,

      // Load everything into config object
      loadAll: (config) => loadFromDb(config),

      // Manual migration trigger
      migrateFromJson: (jsonPath) => migrateFromJson(jsonPath, logger),
      isDbPopulated,

      // Raw DB handle for advanced use
      db,
    };
  },
};
