const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const axios = require('axios');
const fs = require('fs');
const crypto = require('crypto');
const winston = require('winston');
const bcrypt = require('bcrypt');
const session = require('express-session');
const cookieParser = require('cookie-parser');
const nodemailer = require('nodemailer');
const ClusterManager = require('./cluster');
const ProviderHoldDown = require('./monitor');
const NotificationManager = require('./notifications');
const PricingManager = require('./pricing');

// ─── CoT Session Store (Redis-backed, in-memory fallback) ───────────────────
const COT_SESSION_TTL_SEC = 30 * 60; // 30 minutes (Redis uses seconds)
const COT_SESSION_TTL_MS = COT_SESSION_TTL_SEC * 1000;
const COT_SESSION_MAX_ANALYSES = 3;
const COT_SESSION_KEY_PREFIX = 'llmproxy:cot:';

let _redisClient = null;
let _redisAvailable = false;
const _cotSessionsFallback = new Map(); // fallback when Redis unavailable

// Initialize Redis if REDIS_URL is set
if (process.env.REDIS_URL) {
  const Redis = require('ioredis');
  _redisClient = new Redis(process.env.REDIS_URL, {
    lazyConnect: true,
    maxRetriesPerRequest: 1,
    connectTimeout: 5000,
    enableOfflineQueue: false,
  });
  _redisClient.on('ready', () => { _redisAvailable = true; });
  _redisClient.on('error', () => { _redisAvailable = false; });
  _redisClient.on('close', () => { _redisAvailable = false; });
  _redisClient.connect().catch(() => {}); // non-fatal; fallback kicks in
}

// Fallback cleanup interval (only needed when Redis not available)
setInterval(() => {
  if (_redisAvailable) return;
  const cutoff = Date.now() - COT_SESSION_TTL_MS;
  for (const [k, v] of _cotSessionsFallback) { if (v.ts < cutoff) _cotSessionsFallback.delete(k); }
}, 5 * 60 * 1000);

async function getSessionAnalyses(sessionId) {
  if (!sessionId) return [];
  if (_redisAvailable) {
    try {
      const raw = await _redisClient.get(COT_SESSION_KEY_PREFIX + sessionId);
      if (!raw) return [];
      return JSON.parse(raw).analyses || [];
    } catch (_) { /* fall through to in-memory */ }
  }
  const s = _cotSessionsFallback.get(sessionId);
  if (!s || Date.now() - s.ts > COT_SESSION_TTL_MS) return [];
  return s.analyses;
}

async function saveSessionAnalysis(sessionId, analysis) {
  if (!sessionId || !analysis) return;
  if (_redisAvailable) {
    try {
      const raw = await _redisClient.get(COT_SESSION_KEY_PREFIX + sessionId);
      const existing = raw ? (JSON.parse(raw).analyses || []) : [];
      const analyses = [...existing.slice(-(COT_SESSION_MAX_ANALYSES - 1)), analysis];
      await _redisClient.set(
        COT_SESSION_KEY_PREFIX + sessionId,
        JSON.stringify({ analyses }),
        'EX', COT_SESSION_TTL_SEC
      );
      return;
    } catch (_) { /* fall through to in-memory */ }
  }
  const s = _cotSessionsFallback.get(sessionId) || { analyses: [], ts: 0 };
  s.analyses = [...s.analyses.slice(-(COT_SESSION_MAX_ANALYSES - 1)), analysis];
  s.ts = Date.now();
  _cotSessionsFallback.set(sessionId, s);
}

const app = express();
const PORT = process.env.PORT || 3000;

// Logger setup
const logger = winston.createLogger({
  level: 'info',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.json()
  ),
  transports: [
    new winston.transports.File({ filename: '/app/logs/error.log', level: 'error', maxsize: 500 * 1024 * 1024, maxFiles: 5, tailable: true }),
    new winston.transports.File({ filename: '/app/logs/combined.log', maxsize: 500 * 1024 * 1024, maxFiles: 5, tailable: true }),
    new winston.transports.Console({ format: winston.format.simple() })
  ]
});

// Per-provider loggers with 50MB rotation
const providerLoggers = {};
const providerChatLoggers = {};

function getProviderLogger(providerName) {
  if (!providerLoggers[providerName]) {
    providerLoggers[providerName] = winston.createLogger({
      level: 'info',
      format: winston.format.combine(
        winston.format.timestamp(),
        winston.format.json()
      ),
      transports: [
        new winston.transports.File({
          filename: `/app/logs/provider-${providerName}.log`,
          maxsize: 500 * 1024 * 1024, // 500MB
          maxFiles: 5,
          tailable: true
        }),
        new winston.transports.Console({
          format: winston.format.simple(),
          level: 'error' // Only log errors to console for providers
        })
      ]
    });
  }
  return providerLoggers[providerName];
}

// Per-provider human-readable chat loggers
function getProviderChatLogger(providerName) {
  if (!providerChatLoggers[providerName]) {
    const safeName = providerName.replace(/[^a-zA-Z0-9_-]/g, '_');
    providerChatLoggers[providerName] = winston.createLogger({
      level: 'info',
      format: winston.format.printf(({ message }) => message),
      transports: [
        new winston.transports.File({
          filename: `/app/logs/chat-${safeName}.log`,
          maxsize: 500 * 1024 * 1024, // 500MB
          maxFiles: 5,
          tailable: true
        })
      ]
    });
  }
  return providerChatLoggers[providerName];
}

function emitChatLogLines(providerName, text) {
  const safeName = providerName.replace(/[^a-zA-Z0-9_-]/g, '_');
  if (chatLogSubscribers[safeName] && chatLogSubscribers[safeName].size > 0) {
    for (const line of text.split('\n')) {
      notifyChatLogSubscribers(safeName, line);
    }
  }
}

function logChatRequest(providerName, pass, model, messages, req) {
  const chatLog = getProviderChatLogger(providerName);
  const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
  const sep = '─'.repeat(60);
  const lines = [`\n[${ts}] ── REQUEST → ${providerName} (pass ${pass}, model: ${model}) ──`, sep];
  if (req) {
    const ip = req.headers['x-forwarded-for']?.split(',')[0]?.trim() || req.headers['x-real-ip'] || req.ip || 'unknown';
    const keyName = req.clientKey?.name || '(unnamed key)';
    const reqId = req.requestId || '-';
    lines.push(`  source-ip: ${ip}  key: "${keyName}"  req-id: ${reqId}`);
  }
  for (const msg of (messages || [])) {
    const role = (msg.role || 'unknown').toUpperCase();
    let text = '';
    if (typeof msg.content === 'string') {
      text = msg.content;
    } else if (Array.isArray(msg.content)) {
      text = msg.content.map(b => {
        if (b.type === 'text') return b.text || '';
        if (b.type === 'image') return '[IMAGE]';
        if (b.type === 'tool_use') return `[TOOL_USE id=${b.id} name=${b.name}]\n${JSON.stringify(b.input, null, 2)}`;
        if (b.type === 'tool_result') return `[TOOL_RESULT tool_use_id=${b.tool_use_id}]\n${Array.isArray(b.content) ? b.content.map(c => c.text || '').join('') : b.content || ''}`;
        return `[${b.type}]`;
      }).join('\n');
    }
    lines.push(`[${role}]\n${text}`);
  }
  lines.push(sep);
  const text = lines.join('\n');
  chatLog.info(text);
  emitChatLogLines(providerName, text);
}

function logChatResponse(providerName, model, result, latencyMs, cost) {
  const chatLog = getProviderChatLogger(providerName);
  const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
  const sep = '─'.repeat(60);
  const usage = result?.usage || {};
  const statsLine = `latency=${latencyMs}ms  model=${model}  tokens=in:${usage.input_tokens || 0}/out:${usage.output_tokens || 0}  cost=$${(cost || 0).toFixed(6)}`;
  let text = '';
  if (Array.isArray(result?.content)) {
    text = result.content.map(b => {
      if (b.type === 'text') return b.text || '';
      if (b.type === 'tool_use') return `[TOOL_USE id=${b.id} name=${b.name}]\n${JSON.stringify(b.input, null, 2)}`;
      return `[${b.type}]`;
    }).join('\n');
  }
  const lines = [
    `[${ts}] ── RESPONSE ← ${providerName} ──`,
    `[ASSISTANT]\n${text}`,
    sep,
    statsLine,
    sep
  ];
  const out = lines.join('\n');
  chatLog.info(out);
  emitChatLogLines(providerName, out);
}

function logChatFailover(providerName, reason, pass) {
  const chatLog = getProviderChatLogger(providerName);
  const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
  const line = `[${ts}] ✗ FAILOVER from ${providerName} (pass ${pass}): ${reason}`;
  chatLog.info(line);
  emitChatLogLines(providerName, line);
}

function logChatStreamResponse(providerName, model, textBuffer, latencyMs, outputTokens) {
  const chatLog = getProviderChatLogger(providerName);
  const ts = new Date().toISOString().replace('T', ' ').replace(/\.\d{3}Z$/, ' UTC');
  const sep = '─'.repeat(60);
  const cost = pricingManagerRef ? pricingManagerRef.calculateCost(model, 0, outputTokens) : 0;
  const statsLine = `latency=${latencyMs}ms  model=${model}  tokens=out:${outputTokens}  cost=$${cost.toFixed(6)}  [streamed]`;
  const lines = [
    `[${ts}] ── RESPONSE ← ${providerName} (streamed) ──`,
    `[ASSISTANT]\n${textBuffer || '(streamed — text not captured)'}`,
    sep,
    statsLine,
    sep
  ];
  const out = lines.join('\n');
  chatLog.info(out);
  emitChatLogLines(providerName, out);
}

// Ref set after pricingManager is created (avoids forward reference)
let pricingManagerRef = null;

// ── Analytics: hourly time-series ring buffer (7 days = 168 buckets) ─────────
const ANALYTICS_BUCKETS = 168; // 7 days of hourly buckets
const analyticsSeries = {}; // providerId → array of { hour, requests, successes, failures, cost, inputTokens, outputTokens, totalLatency }

function getHourKey() {
  const d = new Date();
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,'0')}-${String(d.getUTCDate()).padStart(2,'0')}T${String(d.getUTCHours()).padStart(2,'0')}`;
}

function recordAnalyticsTick(providerId, { success, cost, inputTokens, outputTokens, latencyMs }) {
  if (!analyticsSeries[providerId]) analyticsSeries[providerId] = [];
  const hour = getHourKey();
  let bucket = analyticsSeries[providerId].find(b => b.hour === hour);
  if (!bucket) {
    bucket = { hour, requests: 0, successes: 0, failures: 0, cost: 0, inputTokens: 0, outputTokens: 0, totalLatency: 0 };
    analyticsSeries[providerId].push(bucket);
    // Keep only last ANALYTICS_BUCKETS
    if (analyticsSeries[providerId].length > ANALYTICS_BUCKETS) {
      analyticsSeries[providerId].shift();
    }
  }
  bucket.requests++;
  if (success) bucket.successes++; else bucket.failures++;
  bucket.cost += cost || 0;
  bucket.inputTokens += inputTokens || 0;
  bucket.outputTokens += outputTokens || 0;
  bucket.totalLatency += latencyMs || 0;
}

// ── SSE: chat log live-tail subscribers ──────────────────────────────────────
const chatLogSubscribers = {}; // safeName → Set of res objects

function notifyChatLogSubscribers(safeName, line) {
  const subs = chatLogSubscribers[safeName];
  if (!subs || subs.size === 0) return;
  const data = `data: ${JSON.stringify(line)}\n\n`;
  for (const res of subs) {
    try { res.write(data); } catch (_) {}
  }
}

// ── Layer 4a: Context Window Auto-Truncation ──────────────────────────────────
// Trims oldest non-system messages until the estimated token count fits within maxTokens.
// Always preserves the system prompt and the most recent user message.
function truncateMessagesToFit(messages, maxTokens) {
  if (!messages || messages.length === 0) return messages;

  const estimate = (msgs) => Math.ceil(JSON.stringify(msgs).length / 4);
  if (estimate(messages) <= maxTokens) return messages;

  // Separate system messages from conversation
  const system = messages.filter(m => m.role === 'system');
  const convo   = messages.filter(m => m.role !== 'system');

  // Always keep last message (most recent user turn)
  let trimmed = [...convo];
  while (trimmed.length > 1 && estimate([...system, ...trimmed]) > maxTokens) {
    trimmed.shift(); // remove oldest non-system message
  }

  const result = [...system, ...trimmed];
  logger.info(`Context truncation: reduced from ${messages.length} → ${result.length} messages to fit ${maxTokens} token window`);
  return result;
}

// ── Layer 4b-4d: Error Classification ────────────────────────────────────────
// Returns error category used to decide hold-down and retry behaviour.
function classifyProviderError(error) {
  const status = error.response?.status;
  const msg    = (error.message || '').toLowerCase();
  const data   = error.response?.data;
  let dataStr = '';
  try { dataStr = JSON.stringify(data || '').toLowerCase(); } catch (_) { dataStr = String(data || '').toLowerCase(); }

  // Context length exceeded — don't hold down, but log clearly
  if (
    status === 400 &&
    (dataStr.includes('context') || dataStr.includes('token') || dataStr.includes('length') ||
     dataStr.includes('too long') || dataStr.includes('maximum') || msg.includes('context'))
  ) return 'context_exceeded';

  // Permanent auth / not-found errors — no hold-down, don't retry
  if (status === 401 || status === 403) return 'auth_error';
  if (status === 404) return 'not_found';

  // Client schema / validation error — no hold-down, may still retry other providers
  if (status === 400 || status === 422) return 'client_error';

  // Rate limit — transient, do hold-down
  if (status === 429) return 'rate_limit';

  // Transient server / overload errors — hold-down
  if (status === 500 || status === 502 || status === 503 || status === 529) return 'transient';

  // Latency timeout (our own)
  if (msg.includes('latency exceeded')) return 'timeout';

  // Network / ECONNREFUSED / ETIMEDOUT
  if (error.code === 'ECONNREFUSED' || error.code === 'ETIMEDOUT' || error.code === 'ENOTFOUND') return 'network';

  return 'unknown';
}

// Initialize pricing manager
const pricingManager = new PricingManager();
pricingManagerRef = pricingManager;
logger.info('Pricing manager initialized');

// Middleware
app.set('trust proxy', true); // trust X-Forwarded-For from nginx
app.use(cors());
app.use(bodyParser.json({ limit: '10mb' }));
app.use(express.static('public'));

// Session configuration
app.use(cookieParser());
// ── SQLite-backed session store (survives container restarts) ─────────────────
// Implements the express-session Store interface using better-sqlite3.
// Sessions are stored in the main config SQLite DB under the 'sessions' table.
const SessionStore = require('express-session').Store;
class SqliteSessionStore extends SessionStore {
  constructor(dbPath) {
    super();
    try {
      const Database = require('better-sqlite3');
      this._db = new Database(dbPath);
      this._db.exec(`CREATE TABLE IF NOT EXISTS sessions (
        sid TEXT PRIMARY KEY,
        sess TEXT NOT NULL,
        expires INTEGER NOT NULL
      )`);
      // Prune expired sessions every 10 minutes
      setInterval(() => {
        try { this._db.prepare('DELETE FROM sessions WHERE expires < ?').run(Date.now()); } catch (_) {}
      }, 10 * 60 * 1000);
    } catch (err) {
      logger.warn('SqliteSessionStore init failed — falling back to MemoryStore:', err.message);
      this._db = null;
    }
  }
  get(sid, cb) {
    if (!this._db) return cb();
    try {
      const row = this._db.prepare('SELECT sess, expires FROM sessions WHERE sid=?').get(sid);
      if (!row || row.expires < Date.now()) return cb();
      cb(null, JSON.parse(row.sess));
    } catch (e) { cb(e); }
  }
  set(sid, sess, cb) {
    if (!this._db) return cb && cb();
    try {
      const expires = sess.cookie?.expires ? new Date(sess.cookie.expires).getTime() : Date.now() + 86400000;
      this._db.prepare('INSERT OR REPLACE INTO sessions (sid,sess,expires) VALUES (?,?,?)').run(sid, JSON.stringify(sess), expires);
      cb && cb();
    } catch (e) { cb && cb(e); }
  }
  destroy(sid, cb) {
    if (!this._db) return cb && cb();
    try {
      this._db.prepare('DELETE FROM sessions WHERE sid=?').run(sid);
      cb && cb();
    } catch (e) { cb && cb(e); }
  }
}

const _sessionStore = new SqliteSessionStore(
  process.env.SQLITE_DB_PATH || '/app/config/llm-proxy.db'
);

app.use(session({
  secret: process.env.SESSION_SECRET || 'llm-proxy-secret-change-in-production',
  resave: false,
  saveUninitialized: false,
  store: _sessionStore,
  cookie: {
    secure: false, // Set to true if using HTTPS
    httpOnly: true,
    maxAge: 24 * 60 * 60 * 1000 // 24 hours
  }
}));

// ── Layer 5: Request Correlation IDs ─────────────────────────────────────────
// Attach a unique request ID to every request for end-to-end tracing in logs.
app.use((req, res, next) => {
  req.requestId = req.headers['x-request-id'] || crypto.randomBytes(8).toString('hex');
  res.setHeader('x-request-id', req.requestId);
  next();
});

// ── Layer 5: Active Session Registry ─────────────────────────────────────────
// Tracks all authenticated sessions in memory: sessionId → metadata.
// Sessions are registered at login and removed at logout or expiry.
const activeSessions = new Map();

function registerSession(sessionId, user, req) {
  activeSessions.set(sessionId, {
    sessionId,
    userId: user.id,
    username: user.username,
    role: user.role,
    ip: req.ip || req.connection?.remoteAddress || 'unknown',
    userAgent: req.headers['user-agent'] || 'unknown',
    loginTime: new Date().toISOString(),
    lastActive: new Date().toISOString()
  });
}

function touchSession(sessionId) {
  const s = activeSessions.get(sessionId);
  if (s) s.lastActive = new Date().toISOString();
}

function revokeSession(sessionId) {
  activeSessions.delete(sessionId);
}

// Update last-active on every authenticated request and auto-extend cookie
app.use((req, res, next) => {
  if (req.session?.user && req.session.id) {
    touchSession(req.session.id);
    // Auto-extend session cookie to configured timeout on every request
    const timeoutMs = (parseInt(config?.smtp?.sessionTimeoutMinutes) || 480) * 60 * 1000;
    req.session.cookie.maxAge = timeoutMs;
  }
  next();
});

// Config file path
const CONFIG_PATH = process.env.CONFIG_PATH || '/app/config/providers.json';

// SQLite feature flag — set USE_SQLITE=true in env to activate
const USE_SQLITE = process.env.USE_SQLITE === 'true';
let sqliteDb = null;

const { inferCapabilitiesFromModelName } = require('./database');

if (USE_SQLITE) {
  try {
    sqliteDb = require('./database').open(logger);
    logger.info('SQLite database layer initialized');
  } catch (err) {
    logger.error('Failed to initialize SQLite — falling back to JSON store:', err.message);
    sqliteDb = null;
  }
}

// Default configuration - minimal example providers
// Providers can be added/configured through the Web UI or environment variables
let config = {
  providers: [],
  stats: {},
  clientApiKeys: [],
  users: [],
  activityLog: [],
  cluster: {},
  smtp: {}
};

// Load/Save config functions
function loadConfig() {
  if (USE_SQLITE && sqliteDb) {
    try {
      sqliteDb.loadAll(config);
      logger.info('Configuration loaded from SQLite');
    } catch (err) {
      logger.error('Error loading config from SQLite:', err);
    }
    return;
  }

  // JSON fallback (original behaviour)
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = fs.readFileSync(CONFIG_PATH, 'utf8');
      config = JSON.parse(data);

      // Initialize activityLog if it doesn't exist
      if (!config.activityLog) {
        config.activityLog = [];
      }

      // Clear per-session transient status fields so providers don't show
      // stale red/green status from the previous session on login
      if (config.stats) {
        Object.values(config.stats).forEach(s => {
          s.lastSuccess = null;
          s.lastError = null;
        });
      }

      logger.info('Configuration loaded from file');
    } else {
      if (!USE_SQLITE) saveConfig();
    }
  } catch (error) {
    logger.error('Error loading config:', error);
  }
}

function saveConfig() {
  if (USE_SQLITE && sqliteDb) {
    try {
      sqliteDb.saveAll(config);
      logger.debug('Configuration saved to SQLite');
    } catch (err) {
      logger.error('Error saving config to SQLite:', err);
    }
    return;
  }

  // JSON fallback (original behaviour)
  try {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
    logger.info('Configuration saved');
  } catch (error) {
    logger.error('Error saving config:', error);
  }
}

// Targeted save helpers — used by hot-path code to avoid a full saveAll()
// In JSON mode these just call saveConfig(). In SQLite mode they write only the changed row.
function saveProviderRecord(provider) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveProvider(provider); return; }
  saveConfig();
}
function deleteProviderRecord(providerId) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.deleteProvider(providerId); return; }
  saveConfig();
}
function saveUserRecord(user) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveUser(user); return; }
  saveConfig();
}
function deleteUserRecord(userId) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.deleteUser(userId); return; }
  saveConfig();
}
function saveApiKeyRecord(key) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveApiKey(key); return; }
  saveConfig();
}
function deleteApiKeyRecord(keyId) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.deleteApiKey(keyId); return; }
  saveConfig();
}
function saveStatsRecord(providerId) {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveStats(providerId, config.stats[providerId]); return; }
  // JSON: periodic save done by caller
}
function saveClusterRecord() {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveCluster(config.cluster); return; }
  saveConfig();
}
function saveSmtpRecord() {
  if (USE_SQLITE && sqliteDb) { sqliteDb.saveSmtp(config.smtp); return; }
  saveConfig();
}

function addActivityLog(type, message, details = {}) {
  if (!config.activityLog) {
    config.activityLog = [];
  }

  const entry = {
    id: `log-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`,
    timestamp: new Date().toISOString(),
    type,  // 'success', 'error', 'info', 'warning'
    message,
    ...details
  };

  config.activityLog.unshift(entry);  // Add to beginning (in-memory for UI)

  if (USE_SQLITE && sqliteDb) {
    // Write directly to DB — no periodic batching needed
    try { sqliteDb.appendActivityLog(entry); } catch (err) { logger.error('SQLite activity log write failed:', err.message); }
    // Keep in-memory list bounded for UI responses
    if (config.activityLog.length > 100) {
      config.activityLog = config.activityLog.slice(0, 100);
    }
    return;
  }

  // JSON fallback
  // Keep only last 100 entries
  if (config.activityLog.length > 100) {
    config.activityLog = config.activityLog.slice(0, 100);
  }

  // Save config periodically (every 10 entries) — SQLite mode writes per-entry above
  if (!USE_SQLITE && config.activityLog.length % 10 === 0) {
    saveConfig();
  }
}

function initStats(providerId) {
  if (!config.stats[providerId]) {
    config.stats[providerId] = {
      requests: 0,
      successes: 0,
      failures: 0,
      totalLatency: 0,
      totalCost: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      lastUsed: null,
      lastError: null,
      lastSuccess: null
    };
  } else {
    // Ensure new fields exist for existing stats
    if (!config.stats[providerId].lastSuccess) {
      config.stats[providerId].lastSuccess = null;
    }
    if (config.stats[providerId].totalCost === undefined) {
      config.stats[providerId].totalCost = 0;
    }
    if (config.stats[providerId].totalInputTokens === undefined) {
      config.stats[providerId].totalInputTokens = 0;
    }
    if (config.stats[providerId].totalOutputTokens === undefined) {
      config.stats[providerId].totalOutputTokens = 0;
    }
  }
}

// Authentication middleware for Web UI
function requireAuth(req, res, next) {
  if (req.session && req.session.user) {
    return next();
  }
  res.status(401).json({ error: 'Authentication required' });
}

// Initialize default admin user
async function initializeUsers() {
  if (!config.users) {
    config.users = [];
  }

  // Create default admin if no users exist
  if (config.users.length === 0) {
    const defaultPassword = process.env.DEFAULT_ADMIN_PASSWORD || 'Super*120120';
    const hashedPassword = await bcrypt.hash(defaultPassword, 10);
    const defaultAdmin = {
      id: 'user-admin',
      username: 'admin',
      password: hashedPassword,
      role: 'admin',
      created: new Date().toISOString()
    };
    config.users.push(defaultAdmin);
    saveUserRecord(defaultAdmin);
    logger.info('Default admin user created with configured password.');
  }
}

// API Key validation middleware
function validateApiKey(req, res, next) {
  // Exempt certain paths from API key validation
  const exemptPaths = ['/health', '/api/config', '/api/stats', '/api/client-keys', '/api/test-provider', '/api/scan-provider-models'];
  const isExempt = exemptPaths.some(path => req.path === path || req.path.startsWith(path + '/')) || req.path === '/';

  if (isExempt) {
    return next();
  }

  const apiKey = req.headers['x-api-key'] ||
                 req.headers.authorization?.replace('Bearer ', '');

  if (!apiKey) {
    logger.warn('Request without API key', { path: req.path, ip: req.ip });
    return res.status(401).json({
      type: 'error',
      error: {
        type: 'authentication_error',
        message: 'API key required. Include x-api-key header or Authorization: Bearer header.'
      }
    });
  }

  if (!config.clientApiKeys) {
    config.clientApiKeys = [];
  }

  const clientKey = config.clientApiKeys.find(k => k.key === apiKey && k.enabled);

  if (!clientKey) {
    logger.warn('Invalid or disabled API key attempt', { key: apiKey.slice(0, 20) + '...', ip: req.ip });
    return res.status(401).json({
      type: 'error',
      error: {
        type: 'authentication_error',
        message: 'Invalid or disabled API key'
      }
    });
  }

  // ── Rate limiting / quota check (only when quotaEnabled === true) ────────────
  if (clientKey.quotaEnabled) {
    const now = Date.now();
    const minKey = Math.floor(now / 60000); // current minute bucket
    const dayKey = new Date().toISOString().slice(0, 10); // YYYY-MM-DD

    // Per-minute window
    if (clientKey.quotaRpm > 0) {
      if (!clientKey._rpm || clientKey._rpm.bucket !== minKey) {
        clientKey._rpm = { bucket: minKey, count: 0 };
      }
      if (clientKey._rpm.count >= clientKey.quotaRpm) {
        logger.warn('Rate limit exceeded (RPM)', { keyName: clientKey.name, limit: clientKey.quotaRpm });
        return res.status(429).json({
          type: 'error',
          error: { type: 'rate_limit_error', message: `Rate limit exceeded: max ${clientKey.quotaRpm} requests per minute for this key.` }
        });
      }
      clientKey._rpm.count++;
    }

    // Per-day window
    if (clientKey.quotaRpd > 0) {
      if (!clientKey._rpd || clientKey._rpd.bucket !== dayKey) {
        clientKey._rpd = { bucket: dayKey, count: 0 };
      }
      if (clientKey._rpd.count >= clientKey.quotaRpd) {
        logger.warn('Rate limit exceeded (RPD)', { keyName: clientKey.name, limit: clientKey.quotaRpd });
        return res.status(429).json({
          type: 'error',
          error: { type: 'rate_limit_error', message: `Quota exceeded: max ${clientKey.quotaRpd} requests per day for this key.` }
        });
      }
      clientKey._rpd.count++;
    }
  }

  // Attach client key info to request
  req.clientKey = clientKey;
  clientKey.lastUsed = new Date().toISOString();

  logger.info('API key validated', { keyName: clientKey.name, keyId: clientKey.id });
  next();
}

// Generate a unique tool-use ID
function generateToolId() {
  return 'toolu_' + crypto.randomBytes(6).toString('hex');
}

// Look up a tool name from message history by tool_use_id
function lookupToolName(messages, toolUseId) {
  for (const msg of messages) {
    const content = Array.isArray(msg.content) ? msg.content : [];
    for (const block of content) {
      if (block.type === 'tool_use' && block.id === toolUseId) {
        return block.name;
      }
    }
  }
  return 'unknown';
}

// Convert Anthropic content array to Gemini parts array
function buildGeminiParts(contentArray, messages) {
  if (typeof contentArray === 'string') return [{ text: contentArray }];
  if (!Array.isArray(contentArray)) return [{ text: '' }];

  const parts = [];
  for (const block of contentArray) {
    if (block.type === 'text') {
      parts.push({ text: block.text });
    } else if (block.type === 'tool_use') {
      parts.push({ functionCall: { name: block.name, args: block.input || {} } });
    } else if (block.type === 'tool_result') {
      const toolName = lookupToolName(messages, block.tool_use_id);
      const resultText = Array.isArray(block.content)
        ? block.content.filter(c => c.type === 'text').map(c => c.text).join('\n')
        : (block.content || '');
      parts.push({ functionResponse: { name: toolName, response: { result: resultText } } });
    }
  }
  return parts.length > 0 ? parts : [{ text: '' }];
}

// Remove JSON Schema fields that Gemini does not accept
function sanitizeSchema(schema) {
  if (!schema || typeof schema !== 'object') return schema;
  if (Array.isArray(schema)) return schema.map(sanitizeSchema);

  const REMOVE = new Set([
    '$schema', 'additionalProperties', 'propertyNames', '$defs', '$ref',
    'allOf', 'anyOf', 'oneOf', 'if', 'then', 'else', 'const', 'examples',
    'contentEncoding', 'contentMediaType', 'unevaluatedProperties',
    'prefixItems', 'contains', 'patternProperties',
    'exclusiveMinimum', 'exclusiveMaximum', 'default', 'format', 'pattern'
  ]);

  const out = {};
  for (const [k, v] of Object.entries(schema)) {
    if (REMOVE.has(k)) continue;
    if (k === 'type' && Array.isArray(v)) {
      // Flatten ["integer","null"] → "integer" (Gemini only accepts a single type string)
      out[k] = v.find(t => t !== 'null') || v[0];
    } else if (k === 'properties' && v && typeof v === 'object') {
      const props = {};
      for (const [pk, pv] of Object.entries(v)) {
        props[pk] = sanitizeSchema(pv);
      }
      out[k] = props;
    } else if (k === 'items') {
      out[k] = sanitizeSchema(v);
    } else {
      out[k] = v;
    }
  }
  return out;
}

// ── 1c: Turn Validator ────────────────────────────────────────────────────────
// Validates Gemini-bound contents array for structural correctness.
// Returns an array of warning strings (empty = valid).
function validateGeminiTurns(contents) {
  const warnings = [];
  if (!contents || contents.length === 0) return warnings;

  // First turn must be user
  if (contents[0].role !== 'user') {
    warnings.push(`First turn is role="${contents[0].role}", expected "user"`);
  }

  // Strict alternation check
  for (let i = 1; i < contents.length; i++) {
    if (contents[i].role === contents[i - 1].role) {
      warnings.push(`Consecutive same-role turns at index ${i - 1}/${i} (role="${contents[i].role}")`);
    }
  }

  // No empty parts arrays
  for (let i = 0; i < contents.length; i++) {
    if (!contents[i].parts || contents[i].parts.length === 0) {
      warnings.push(`Turn ${i} (role="${contents[i].role}") has empty parts array`);
    }
  }

  return warnings;
}

// ── Model allow-list check ────────────────────────────────────────────────────
// Returns true if the requested model is allowed for this provider.
// If the provider has no enabledModels list, all models are allowed.
function isModelAllowedForProvider(provider, requestedModel) {
  if (!provider.enabledModels || provider.enabledModels.length === 0) return true;
  if (!requestedModel) return true;
  return provider.enabledModels.includes(requestedModel);
}

// ── 1e: XML / Bad-Model Sentinel ──────────────────────────────────────────────
// Scans a response body string for known bad-model output patterns.
// Returns true if the response looks like garbage that should not be forwarded.
const XML_SENTINEL_PATTERNS = [
  /<execute_bash[\s>]/i,
  /<thought[\s>]/i,
  /<function_calls[\s>]/i,
  /<invoke[\s>]/i,
  /^functionCall\s*\{/m,
  /<parameter>[\s\S]*?<\/antml:parameter>/
];

function isBadModelOutput(text) {
  if (typeof text !== 'string') return false;
  return XML_SENTINEL_PATTERNS.some(re => re.test(text));
}

// ── 1f: Prompt-Based Tool Calling (PBTC) ─────────────────────────────────────
// For non-Anthropic providers that don't reliably produce native function-call
// outputs, the proxy intercepts tool-bearing requests and:
//   1. Strips the `tools` array from the request
//   2. Injects a system-prompt section teaching the model a <tool_call> text format
//   3. Converts any tool_use / tool_result blocks in message history to plain text
//   4. After the provider responds, parses <tool_call> blocks from the text and
//      emits a proper Anthropic-format response with tool_use content blocks
//
// This prevents the coordinator daemon from ever seeing bad-model XML stubs and
// falling to FALLBACK TEXT-ONLY MODE — the proxy transparently bridges the gap.

const PBTC_TAG_OPEN  = '<tool_call>';
const PBTC_TAG_CLOSE = '</tool_call>';

// Alternate tag pairs that some models (e.g. Gemini) use instead of <tool_call>
// The proxy accepts all of these and normalises them to tool_use blocks.
const PBTC_ALT_TAGS = [
  { open: '<tool_code>',   close: '</tool_code>'   },
  { open: '<function_call>', close: '</function_call>' },
  { open: '<tool_use>',    close: '</tool_use>'    },
];

/** Decide whether to use prompt-based tool calling for this provider+request. */
function shouldUsePromptToolCalling(provider, request) {
  if (!request.tools || request.tools.length === 0) return false;
  if (provider.type === 'anthropic') return false;   // native tool calling works
  if (provider.excludeFromToolRequests) return false; // hard-excluded — skip entirely
  return true;  // all other providers with tools → PBTC
}

/** Build the system-prompt section that teaches the model the <tool_call> format. */
function buildPbtcSystemSection(tools) {
  const toolList = (tools || []).map(t => {
    const props = t.input_schema?.properties || {};
    const required = new Set(t.input_schema?.required || []);
    const params = Object.entries(props).map(([k, v]) => {
      const req = required.has(k) ? '' : ' (optional)';
      const typ = v.type || 'string';
      const desc = v.description ? ` — ${v.description}` : '';
      return `    • ${k} (${typ}${req})${desc}`;
    }).join('\n') || '    (no parameters)';
    return `  **${t.name}**: ${t.description || ''}\n${params}`;
  }).join('\n\n');

  return `## Tool Use

You have access to the following tools. To call a tool, output a JSON object wrapped in ONE of these tag formats on its own line (the system accepts all of them equivalently):

${PBTC_TAG_OPEN}
{"name": "ToolName", "input": {"param": "value"}}
${PBTC_TAG_CLOSE}

or equivalently:

<tool_code>
{"name": "ToolName", "input": {"param": "value"}}
</tool_code>

Rules:
- Output ONLY ONE tool call per response.
- The tag + JSON must appear on their own lines with no other content on the same lines.
- After the closing tag you MUST stop generating — the system will run the tool and send you the result.
- If you need no tools, respond with plain text only (no tags).
- Use the EXACT tool names listed below — do not invent new names.
- The JSON must have a "name" key (tool name) and an "input" key (object of parameters).

### Available Tools

${toolList}`;
}

/** Pre-process an Anthropic request for prompt-based tool calling.
 *  Returns a new request object with tools stripped and injected into system prompt. */
function pbtcPreprocess(originalRequest) {
  const req = JSON.parse(JSON.stringify(originalRequest)); // deep copy

  // Inject tool descriptions into system prompt
  const toolSection = buildPbtcSystemSection(req.tools || []);
  if (typeof req.system === 'string') {
    req.system = req.system + '\n\n' + toolSection;
  } else if (Array.isArray(req.system)) {
    req.system = [
      ...req.system,
      { type: 'text', text: toolSection }
    ];
  } else {
    req.system = toolSection;
  }

  // Convert tool_use / tool_result blocks in message history to plain text
  req.messages = (req.messages || []).flatMap(msg => {
    if (typeof msg.content === 'string') return [msg];
    if (!Array.isArray(msg.content)) return [msg];

    const hasToolBlocks = msg.content.some(
      c => c.type === 'tool_use' || c.type === 'tool_result'
    );
    if (!hasToolBlocks) return [msg];

    // Split into one message per content block type to keep roles correct
    const assistantParts = [];
    const userParts = [];

    for (const block of msg.content) {
      if (block.type === 'text') {
        (msg.role === 'assistant' ? assistantParts : userParts).push(block.text);
      } else if (block.type === 'tool_use') {
        assistantParts.push(
          `${PBTC_TAG_OPEN}\n${JSON.stringify({ name: block.name, input: block.input })}\n${PBTC_TAG_CLOSE}`
        );
      } else if (block.type === 'tool_result') {
        const content = typeof block.content === 'string'
          ? block.content
          : (Array.isArray(block.content) ? block.content.map(c => c.text || '').join('') : '');
        userParts.push(`[Tool result]:\n${content}`);
      }
    }

    const out = [];
    if (assistantParts.length) out.push({ role: 'assistant', content: assistantParts.join('\n\n') });
    if (userParts.length)      out.push({ role: 'user',      content: userParts.join('\n\n') });
    return out.length ? out : [{ role: msg.role, content: '' }];
  });

  // Strip native tool definitions — model will use text format instead
  delete req.tools;
  delete req.tool_choice;

  return req;
}

/** Find the earliest occurrence of any PBTC open tag in `text`.
 *  Returns { start, tagOpen, tagClose } or null if none found. */
function findNextPbtcTag(text) {
  // Build list of all tag pairs to search
  const allTags = [
    { open: PBTC_TAG_OPEN, close: PBTC_TAG_CLOSE },
    ...PBTC_ALT_TAGS
  ];
  let best = null;
  for (const tag of allTags) {
    const idx = text.indexOf(tag.open);
    if (idx !== -1 && (best === null || idx < best.start)) {
      best = { start: idx, tagOpen: tag.open, tagClose: tag.close };
    }
  }
  return best;
}

/** Parse <tool_call> / <tool_code> / <function_call> blocks from a provider text response.
 *  Recognises primary <tool_call> tag AND provider-specific alternate tags (e.g. Gemini's
 *  <tool_code>). Returns { textBlocks: [{type:'text',text}], toolBlocks: [{type:'tool_use',...}] } */
function pbtcParseResponse(text) {
  const textBlocks = [];
  const toolBlocks = [];
  let remaining = text;
  let callIndex = 0;

  while (true) {
    const found = findNextPbtcTag(remaining);
    if (!found) break;

    const { start, tagOpen, tagClose } = found;

    const pre = remaining.slice(0, start).trim();
    if (pre) textBlocks.push({ type: 'text', text: pre });

    const end = remaining.indexOf(tagClose, start + tagOpen.length);
    if (end === -1) {
      // Unclosed tag — treat rest as text
      textBlocks.push({ type: 'text', text: remaining.slice(start) });
      remaining = '';
      break;
    }

    const jsonStr = remaining.slice(start + tagOpen.length, end).trim();
    remaining = remaining.slice(end + tagClose.length);

    try {
      const parsed = JSON.parse(jsonStr);
      const name  = String(parsed.name || '').trim();
      const input = parsed.input || parsed.parameters || parsed.args || {};
      if (name) {
        if (tagOpen !== PBTC_TAG_OPEN) {
          logger.debug(`PBTC: parsed tool call via alternate tag ${tagOpen}: ${name}`);
        }
        toolBlocks.push({
          type:  'tool_use',
          id:    `toolu_pbtc_${Date.now()}_${callIndex++}`,
          name,
          input
        });
      }
    } catch (e) {
      logger.debug(`PBTC: failed to parse ${tagOpen} JSON: ${jsonStr.slice(0, 100)}`);
    }
  }

  const tail = remaining.trim();
  if (tail) textBlocks.push({ type: 'text', text: tail });

  return { textBlocks, toolBlocks };
}

/** Post-process an Anthropic-format response to replace text tool_call blocks
 *  with proper tool_use content blocks. Returns the modified response. */
function pbtcPostprocess(anthropicResponse, originalTools) {
  const content = anthropicResponse.content || [];
  const newContent = [];
  let hasToolUse = false;

  for (const block of content) {
    if (block.type !== 'text') {
      newContent.push(block);
      continue;
    }
    const { textBlocks, toolBlocks } = pbtcParseResponse(block.text || '');
    for (const tb of textBlocks) if (tb.text) newContent.push(tb);
    for (const tc of toolBlocks) {
      hasToolUse = true;
      newContent.push(tc);
    }
  }

  if (!hasToolUse) return anthropicResponse; // no tool calls — return unchanged

  if (newContent.length === 0) newContent.push({ type: 'text', text: '' });

  logger.info(`PBTC: converted ${newContent.filter(b => b.type === 'tool_use').length} text tool_call(s) to tool_use blocks`);
  return {
    ...anthropicResponse,
    content:     newContent,
    stop_reason: 'tool_use'
  };
}

/** Emit a synthetic Anthropic SSE stream for a PBTC response (non-native streaming). */
async function pbtcEmitStream(res, pbtcResult, originalModel) {
  const msgId = `msg_pbtc_${Date.now()}`;
  const emit = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

  emit('message_start', {
    type: 'message_start',
    message: { id: msgId, type: 'message', role: 'assistant', content: [],
               model: originalModel || 'unknown', stop_reason: null, usage: { input_tokens: 0, output_tokens: 0 } }
  });

  let blockIdx = 0;
  for (const block of pbtcResult.content) {
    if (block.type === 'text') {
      emit('content_block_start', { type: 'content_block_start', index: blockIdx, content_block: { type: 'text', text: '' } });
      emit('content_block_delta', { type: 'content_block_delta', index: blockIdx, delta: { type: 'text_delta', text: block.text } });
      emit('content_block_stop', { type: 'content_block_stop', index: blockIdx });
    } else if (block.type === 'tool_use') {
      emit('content_block_start', { type: 'content_block_start', index: blockIdx,
        content_block: { type: 'tool_use', id: block.id, name: block.name, input: {} } });
      emit('content_block_delta', { type: 'content_block_delta', index: blockIdx,
        delta: { type: 'input_json_delta', partial_json: JSON.stringify(block.input) } });
      emit('content_block_stop', { type: 'content_block_stop', index: blockIdx });
    }
    blockIdx++;
  }

  emit('message_delta', { type: 'message_delta',
    delta: { stop_reason: pbtcResult.stop_reason || 'end_turn', stop_sequence: null },
    usage: { output_tokens: 0 } });
  emit('message_stop', { type: 'message_stop' });
}

// ── 1g: Prompt-Based Reasoning Chain (PBRC) ───────────────────────────────────
// For providers that don't natively support extended thinking / reasoning, the proxy
// intercepts requests with `thinking: {type:"enabled"}` and:
//   1. Strips the `thinking` parameter (providers would reject it)
//   2. Injects a system-prompt section instructing the model to reason step-by-step
//      inside <thinking>…</thinking> tags before answering
//   3. Parses those <thinking> blocks from the response and converts them to proper
//      Anthropic extended-thinking content blocks {type:"thinking"}
//   4. For streaming: collects full non-streaming response, then emits synthetic SSE
//
// Symmetrical to PBTC — but for reasoning instead of tool calling.

const PBRC_THINK_OPEN  = '<thinking>';
const PBRC_THINK_CLOSE = '</thinking>';

/** Return true when reasoning simulation should be engaged for this provider+request. */
function shouldSimulateReasoning(provider, request) {
  if (!request.thinking || request.thinking.type !== 'enabled') return false;
  if (provider.type === 'anthropic') return false;                                       // native extended thinking
  if (provider.type === 'openai' && /^o[1-9]/i.test(provider.model || '')) return false; // o-series: native reasoning
  if (provider.type === 'google' && (provider.model || '').includes('gemini-2.5')) return false; // Gemini 2.5+
  return true; // all other providers → PBRC
}

/** Build system-prompt section that teaches the model to emit <thinking> tags. */
function buildPbrcSystemSection() {
  return `## Reasoning / Thinking

Before responding, reason through the problem completely. Output your ENTIRE thought \
process between ${PBRC_THINK_OPEN} and ${PBRC_THINK_CLOSE} tags, then give your final answer.

Required format:
${PBRC_THINK_OPEN}
[Step-by-step reasoning, analysis, edge cases, self-checks — be thorough and honest]
${PBRC_THINK_CLOSE}

[Your final response, based on the reasoning above]

Rules:
- ALWAYS open with a ${PBRC_THINK_OPEN} block — never skip it.
- The thinking section is a private scratchpad; explore freely before committing.
- Content AFTER the ${PBRC_THINK_CLOSE} tag is your actual answer to the user.`;
}

/** Pre-process a request for PBRC: strip thinking param, inject reasoning instruction,
 *  convert any existing thinking blocks in history to text. */
function pbrcPreprocess(originalRequest) {
  const req = JSON.parse(JSON.stringify(originalRequest));
  delete req.thinking; // remove — provider won't recognise it

  const section = buildPbrcSystemSection();
  if (typeof req.system === 'string') {
    req.system = section + '\n\n' + req.system;
  } else if (Array.isArray(req.system)) {
    req.system = [{ type: 'text', text: section }, ...req.system];
  } else {
    req.system = section;
  }

  // Convert existing thinking blocks in message history → tagged text
  req.messages = (req.messages || []).map(msg => {
    if (!Array.isArray(msg.content)) return msg;
    if (!msg.content.some(c => c.type === 'thinking')) return msg;
    return {
      ...msg,
      content: msg.content.map(c =>
        c.type === 'thinking'
          ? { type: 'text', text: `${PBRC_THINK_OPEN}\n${c.thinking || ''}\n${PBRC_THINK_CLOSE}` }
          : c
      )
    };
  });

  return req;
}

/** Parse <thinking>…</thinking> from provider text. Returns { thinkingBlocks, textContent }. */
function pbrcParseResponse(text) {
  const thinkingBlocks = [];
  const textParts = [];
  let remaining = text;

  while (true) {
    const start = remaining.indexOf(PBRC_THINK_OPEN);
    if (start === -1) break;
    const pre = remaining.slice(0, start).trim();
    if (pre) textParts.push(pre);
    const end = remaining.indexOf(PBRC_THINK_CLOSE, start + PBRC_THINK_OPEN.length);
    if (end === -1) {
      // Unclosed — everything from here is thinking
      thinkingBlocks.push(remaining.slice(start + PBRC_THINK_OPEN.length).trim());
      remaining = '';
      break;
    }
    const t = remaining.slice(start + PBRC_THINK_OPEN.length, end).trim();
    if (t) thinkingBlocks.push(t);
    remaining = remaining.slice(end + PBRC_THINK_CLOSE.length);
  }
  const tail = remaining.trim();
  if (tail) textParts.push(tail);
  return { thinkingBlocks, textContent: textParts.join('\n\n').trim() };
}

/** Post-process Anthropic-format response: convert text <thinking> blocks → thinking content blocks. */
function pbrcPostprocess(anthropicResponse) {
  const content = anthropicResponse.content || [];
  const newContent = [];
  let thinkingCount = 0;

  for (const block of content) {
    if (block.type !== 'text') { newContent.push(block); continue; }
    const { thinkingBlocks, textContent } = pbrcParseResponse(block.text || '');
    for (const t of thinkingBlocks) {
      thinkingCount++;
      newContent.push({ type: 'thinking', thinking: t, signature: `pbrc-sim-${Date.now()}` });
    }
    if (textContent) newContent.push({ type: 'text', text: textContent });
  }

  if (thinkingCount === 0) return anthropicResponse;
  logger.info(`PBRC: extracted ${thinkingCount} simulated thinking block(s)`);
  return { ...anthropicResponse, content: newContent };
}

/** Emit synthetic Anthropic SSE stream for a PBRC response. */
async function pbrcEmitStream(res, pbrcResult, originalModel) {
  const msgId = `msg_pbrc_${Date.now()}`;
  const emit = (event, data) => res.write(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);

  emit('message_start', {
    type: 'message_start',
    message: { id: msgId, type: 'message', role: 'assistant', content: [],
               model: originalModel || 'unknown', stop_reason: null, usage: { input_tokens: 0, output_tokens: 0 } }
  });

  let blockIdx = 0;
  for (const block of pbrcResult.content) {
    if (block.type === 'thinking') {
      emit('content_block_start', { type: 'content_block_start', index: blockIdx,
        content_block: { type: 'thinking', thinking: '' } });
      emit('content_block_delta', { type: 'content_block_delta', index: blockIdx,
        delta: { type: 'thinking_delta', thinking: block.thinking } });
      emit('content_block_stop', { type: 'content_block_stop', index: blockIdx });
    } else if (block.type === 'text') {
      emit('content_block_start', { type: 'content_block_start', index: blockIdx,
        content_block: { type: 'text', text: '' } });
      emit('content_block_delta', { type: 'content_block_delta', index: blockIdx,
        delta: { type: 'text_delta', text: block.text } });
      emit('content_block_stop', { type: 'content_block_stop', index: blockIdx });
    }
    blockIdx++;
  }

  emit('message_delta', { type: 'message_delta',
    delta: { stop_reason: pbrcResult.stop_reason || 'end_turn', stop_sequence: null },
    usage: { output_tokens: 0 } });
  emit('message_stop', { type: 'message_stop' });
}

// ── 1h: Cross-Provider Feature Emulation helpers ──────────────────────────────
// Utility functions for emulating provider capabilities that aren't natively supported.

/** Strip image content blocks for providers that don't support vision.
 *  Replaces image blocks with a text placeholder so the provider doesn't error. */
function stripImagesForTextOnlyProvider(request) {
  if (!hasImageContent(request.messages)) return request;
  const req = JSON.parse(JSON.stringify(request));
  req.messages = req.messages.map(msg => {
    if (!Array.isArray(msg.content)) return msg;
    if (!msg.content.some(c => c.type === 'image' || c.type === 'image_url')) return msg;
    const newContent = msg.content.map(c => {
      if (c.type === 'image' || c.type === 'image_url') {
        return { type: 'text', text: '[Image content — not supported by this provider]' };
      }
      return c;
    });
    return { ...msg, content: newContent };
  });
  return req;
}

/** Apply all applicable emulation layers to a request for a given provider.
 *  Returns { requestBody, usePbtc, usePbrc } so callers know which post-processing to apply. */
function applyProviderEmulation(provider, originalRequestBody) {
  let requestBody = originalRequestBody;
  let usePbtc = false;
  let usePbrc = false;

  // Vision stripping for text-only providers
  const caps = PROVIDER_CAPS[provider.type] || {};
  if (!caps.vision && hasImageContent(requestBody.messages)) {
    requestBody = stripImagesForTextOnlyProvider(requestBody);
    logger.info(`Vision-strip: provider ${provider.name} is text-only — images replaced with text placeholders`);
  }

  // PBTC — tool calling emulation
  if (shouldUsePromptToolCalling(provider, requestBody)) {
    usePbtc = true;
    requestBody = pbtcPreprocess(requestBody);
    logger.info(`PBTC: active for ${provider.name} — tools converted to prompt instructions`);
  }

  // PBRC — reasoning emulation
  if (shouldSimulateReasoning(provider, requestBody)) {
    usePbrc = true;
    requestBody = pbrcPreprocess(requestBody);
    logger.info(`PBRC: active for ${provider.name} — reasoning simulated via <thinking> tags`);
  }

  return { requestBody, usePbtc, usePbrc };
}

// ── 2: Capability-Aware Router ────────────────────────────────────────────────
// nativeReasoning: true = model natively supports extended_thinking / reasoning tokens
// toolCalling: true = natively produces tool_use blocks (PBTC handles false case)
// vision: true = accepts image content blocks (strip-images handles false case)
const PROVIDER_CAPS = {
  anthropic:          { toolCalling: true,  vision: true,  nativeReasoning: true,  contextWindow: 200000  },
  google:             { toolCalling: true,  vision: true,  nativeReasoning: false, contextWindow: 1000000 },
  vertex:             { toolCalling: true,  vision: true,  nativeReasoning: false, contextWindow: 1000000 },
  openai:             { toolCalling: true,  vision: true,  nativeReasoning: false, contextWindow: 128000  },
  grok:               { toolCalling: true,  vision: false, nativeReasoning: false, contextWindow: 131072  },
  ollama:             { toolCalling: false, vision: false, nativeReasoning: false, contextWindow: 8192    },
  'openai-compatible':{ toolCalling: true,  vision: false, nativeReasoning: false, contextWindow: 128000  }
};

function estimateTokens(request) {
  // Rough estimate: 4 chars ≈ 1 token
  try {
    return Math.ceil(JSON.stringify(request.messages || []).length / 4);
  } catch (_) { return 0; }
}

function hasImageContent(messages) {
  if (!messages) return false;
  return messages.some(m =>
    Array.isArray(m.content) && m.content.some(b => b.type === 'image' || b.type === 'image_url')
  );
}

function capabilityFilter(providers, request) {
  const hasTools  = request.tools?.length > 0;
  const hasVision = hasImageContent(request.messages);
  const tokens    = estimateTokens(request);

  const capable = providers.filter(p => {
    const caps = PROVIDER_CAPS[p.type];
    if (!caps) return true; // unknown type — don't filter out
    if (hasTools  && !caps.toolCalling && !shouldUsePromptToolCalling(p, request)) return false;
    if (hasTools  && p.excludeFromToolRequests) return false; // admin hard-excluded (no PBTC fallback)
    if (hasVision && !caps.vision)         return false;
    if (tokens > caps.contextWindow)       return false;
    return true;
  });

  // Fall back to full list if capability filtering eliminates everything
  return capable.length > 0 ? capable : providers;
}

// ── LMRH: LLM Model Routing Hint Protocol (draft-blagbrough-lmrh-00) ─────────
//
// Parses the LLM-Hint request header (RFC 8941-style key=value pairs) and uses
// capability profiles stored in model_capabilities to score and rank providers.
// The header is proxy-internal only — never forwarded to backend providers.

function parseLmrhHint(headerValue) {
  if (!headerValue) return null;
  const hint = {
    task: null, latency: null, cost: null,
    safetyMin: null, safetyMax: null,
    region: null, contextLength: null,
    modality: [], providerHint: null,
    version: 1, hard: new Set(), raw: headerValue,
  };
  // Split on commas, parse each token as key=value[;require]
  for (const token of headerValue.split(',')) {
    const t = token.trim();
    if (!t) continue;
    const isHard = /;require\b/i.test(t);
    const kv = t.replace(/;require\b.*$/i, '').trim();
    const eqIdx = kv.indexOf('=');
    if (eqIdx < 0) { if (kv === 'v') continue; }
    const key = kv.slice(0, eqIdx < 0 ? undefined : eqIdx).trim().toLowerCase();
    const val = eqIdx >= 0 ? kv.slice(eqIdx + 1).trim().replace(/^"|"$/g, '') : null;
    if (!key || key === 'v') { if (key === 'v' && val) hint.version = parseInt(val) || 1; continue; }
    switch (key) {
      case 'task':           hint.task = val; break;
      case 'latency':        hint.latency = val; break;
      case 'cost':           hint.cost = val; break;
      case 'safety-min':     hint.safetyMin = parseInt(val); break;
      case 'safety-max':     hint.safetyMax = parseInt(val); break;
      case 'region':         hint.region = val; break;
      case 'context-length': hint.contextLength = parseInt(val); break;
      case 'modality':       hint.modality.push(val); break;
      case 'provider-hint':  hint.providerHint = val; break;
    }
    if (isHard) hint.hard.add(key);
  }
  return hint;
}

// Affinity scoring weights (soft constraints only — hard constraints are pass/fail)
const LMRH_WEIGHTS = { task: 10, safety: 8, region: 6, latency: 4, cost: 3, context: 2, modality: 5 };
const LATENCY_ORDER  = { low: 0, medium: 1, high: 2 };
const COST_ORDER     = { economy: 0, standard: 1, premium: 2 };

function scoreModelAgainstHint(caps, hint) {
  const failedHard = [];
  let score = 0;

  // --- Hard constraint checks (fail-fast) ---
  if (hint.safetyMin !== null && hint.hard.has('safety-min') && (caps.safety ?? 2) < hint.safetyMin)
    failedHard.push('safety-min');
  if (hint.safetyMax !== null && hint.hard.has('safety-max') && (caps.safety ?? 2) > hint.safetyMax)
    failedHard.push('safety-max');
  if (hint.contextLength !== null && hint.hard.has('context-length') && (caps.context_length ?? 8192) < hint.contextLength)
    failedHard.push('context-length');
  if (hint.region && hint.hard.has('region') && Array.isArray(caps.region) && !caps.region.includes(hint.region))
    failedHard.push('region');
  if (hint.providerHint && hint.hard.has('provider-hint'))
    {} // enforced at provider level, not model level
  for (const mod of hint.modality) {
    if (hint.hard.has('modality') && Array.isArray(caps.modality) && !caps.modality.includes(mod))
      failedHard.push('modality');
  }
  if (failedHard.length) return { score: -Infinity, failedHard };

  // --- Soft scoring ---
  if (hint.task && Array.isArray(caps.task) && caps.task.includes(hint.task))
    score += LMRH_WEIGHTS.task;
  if (hint.safetyMin !== null && (caps.safety ?? 2) >= hint.safetyMin)
    score += LMRH_WEIGHTS.safety;
  if (hint.safetyMax !== null && (caps.safety ?? 2) <= hint.safetyMax)
    score += LMRH_WEIGHTS.safety;
  if (hint.latency && caps.latency === hint.latency)
    score += LMRH_WEIGHTS.latency;
  else if (hint.latency && caps.latency) {
    // Partial credit: adjacent latency tier
    const diff = Math.abs((LATENCY_ORDER[caps.latency] ?? 1) - (LATENCY_ORDER[hint.latency] ?? 1));
    if (diff === 1) score += Math.floor(LMRH_WEIGHTS.latency / 2);
  }
  if (hint.cost && caps.cost === hint.cost)
    score += LMRH_WEIGHTS.cost;
  if (hint.region && Array.isArray(caps.region) && caps.region.includes(hint.region))
    score += LMRH_WEIGHTS.region;
  if (hint.contextLength && (caps.context_length ?? 8192) >= hint.contextLength)
    score += LMRH_WEIGHTS.context;
  for (const mod of hint.modality) {
    if (Array.isArray(caps.modality) && caps.modality.includes(mod))
      score += LMRH_WEIGHTS.modality;
  }

  return { score, failedHard: [] };
}

// Returns a re-ranked provider list with the best-scoring model attached per provider.
// Providers whose best model fails a hard constraint are removed.
// Returns { rankedProviders, unmetAffinities[], selectedModel per provider stored in p._lmrhModel }
function rankProvidersWithHint(providers, hint, sqliteDb, requestModel) {
  const scored = [];
  const unmetAffinities = new Set();

  for (const provider of providers) {
    // If provider-hint is hard and doesn't match, skip
    if (hint.providerHint && hint.hard.has('provider-hint') &&
        provider.name.toLowerCase() !== hint.providerHint.toLowerCase()) continue;

    // Get all capability profiles for this provider
    let modelCaps = [];
    if (sqliteDb) {
      try { modelCaps = sqliteDb.listModelCapabilities(provider.id); } catch (_) {}
    }

    // If no profiles yet, use the provider's default model with inferred caps
    if (modelCaps.length === 0) {
      // Fall back to request model, then provider default model, then provider type
      const modelToInfer = provider.model || requestModel ||
        ({ anthropic: 'claude-sonnet', google: 'gemini-1.5-pro', openai: 'gpt-4o',
           grok: 'grok-1', ollama: 'llama3', 'openai-compatible': 'gpt-4o' }[provider.type] || 'unknown');
      modelCaps = [{ model_id: modelToInfer, ...inferCapabilitiesFromModelName(modelToInfer) }];
    }

    let bestScore = -Infinity;
    let bestModel = provider.model;
    let bestCaps  = null;

    for (const caps of modelCaps) {
      const { score, failedHard } = scoreModelAgainstHint(caps, hint);
      if (failedHard.length) continue; // hard fail — skip this model
      if (score > bestScore) { bestScore = score; bestModel = caps.model_id; bestCaps = caps; }
    }

    if (bestScore === -Infinity) continue; // all models hard-failed for this provider

    // Collect soft unmet affinities for LLM-Capability header
    if (hint.task && bestCaps && Array.isArray(bestCaps.task) && !bestCaps.task.includes(hint.task))
      unmetAffinities.add('task');
    if (hint.latency && bestCaps && bestCaps.latency !== hint.latency)
      unmetAffinities.add('latency');
    if (hint.cost && bestCaps && bestCaps.cost !== hint.cost)
      unmetAffinities.add('cost');

    scored.push({ provider, model: bestModel, caps: bestCaps, score: bestScore });
  }

  // Sort descending by score, preserve original priority order for ties
  scored.sort((a, b) => b.score - a.score || a.provider.priority - b.provider.priority);

  // Attach selected model onto provider object for downstream use
  for (const s of scored) s.provider._lmrhModel = s.model;

  return {
    rankedProviders: scored.map(s => s.provider),
    unmetAffinities: [...unmetAffinities],
    topCaps: scored[0]?.caps || null,
  };
}

// Build LLM-Capability response header value
function buildLmrhCapabilityHeader(provider, modelId, caps, unmet, cotEngaged) {
  if (!provider) return null;
  const parts = [
    `v=1`,
    `provider=${provider.name.toLowerCase().replace(/\s+/g, '-')}`,
    `model=${modelId || provider.model || 'unknown'}`,
  ];
  if (caps) {
    if (Array.isArray(caps.task) && caps.task[0]) parts.push(`task=${caps.task[0]}`);
    if (caps.safety != null)         parts.push(`safety=${caps.safety}`);
    if (caps.latency)                parts.push(`latency=${caps.latency}`);
    if (caps.cost)                   parts.push(`cost=${caps.cost}`);
    if (caps.context_length)         parts.push(`context-length=${caps.context_length}`);
    if (Array.isArray(caps.region) && caps.region[0]) parts.push(`region=${caps.region[0]}`);
  }
  if (unmet && unmet.length) parts.push(`unmet=${unmet.join(' ')}`);
  if (cotEngaged) parts.push(`cot-engaged=?1`);
  return parts.join(', ');
}

// Translate Anthropic request to Google Gemini format (with tool support)
function translateToGemini(anthropicRequest) {
  const messages = anthropicRequest.messages || [];

  // Build Gemini contents, merging consecutive same-role turns (Gemini requires strict alternation)
  const rawContents = messages.map(msg => ({
    role: msg.role === 'assistant' ? 'model' : 'user',
    parts: buildGeminiParts(msg.content, messages)
  }));

  // Merge consecutive same-role turns
  const contents = [];
  for (const turn of rawContents) {
    if (contents.length > 0 && contents[contents.length - 1].role === turn.role) {
      contents[contents.length - 1].parts.push(...turn.parts);
    } else {
      contents.push({ role: turn.role, parts: [...turn.parts] });
    }
  }

  // Translate Anthropic tools to Gemini functionDeclarations
  const tools = anthropicRequest.tools;
  const geminiTools = tools && tools.length > 0
    ? [{ functionDeclarations: tools.map(t => ({
        name: t.name,
        description: t.description || '',
        parameters: sanitizeSchema(t.input_schema) || { type: 'object', properties: {} }
      })) }]
    : undefined;

  const result = {
    contents,
    generationConfig: {
      temperature: anthropicRequest.temperature || 1.0,
      maxOutputTokens: anthropicRequest.max_tokens || 4096,
      topP: anthropicRequest.top_p || 1.0,
    }
  };

  if (geminiTools) {
    result.tools = geminiTools;
    result.toolConfig = { functionCallingConfig: { mode: 'AUTO' } };
  }

  if (anthropicRequest.system) {
    const sysText = Array.isArray(anthropicRequest.system)
      ? anthropicRequest.system.filter(s => s.type === 'text').map(s => s.text || '').join('\n')
      : anthropicRequest.system;
    result.systemInstruction = { parts: [{ text: sysText }] };
  }

  if (result.systemInstruction && geminiTools) {
    result.systemInstruction.parts[0].text +=
      '\n\nIMPORTANT: Use the provided function tools to complete the task. ' +
      'Do not describe what you would do — call the tools directly.';
  }

  // 1c: Validate turn structure and log any warnings
  const turnWarnings = validateGeminiTurns(result.contents);
  if (turnWarnings.length > 0) {
    logger.warn('Gemini turn validation warnings', { warnings: turnWarnings });
  }

  return result;
}

// Translate Google Gemini response to Anthropic format (non-streaming, with tool_use support)
function translateFromGemini(geminiResponse, model) {
  const candidate = geminiResponse.candidates?.[0];
  const parts = candidate?.content?.parts || [];

  const contentBlocks = [];
  let hasToolUse = false;

  for (const part of parts) {
    if (part.text) {
      contentBlocks.push({ type: 'text', text: part.text });
    } else if (part.functionCall) {
      hasToolUse = true;
      contentBlocks.push({
        type: 'tool_use',
        id: generateToolId(),
        name: part.functionCall.name,
        input: part.functionCall.args || {}
      });
    }
  }

  if (contentBlocks.length === 0) {
    contentBlocks.push({ type: 'text', text: '' });
  }

  const finishReason = candidate?.finishReason;
  let stop_reason;
  if (hasToolUse) {
    stop_reason = 'tool_use';
  } else if (finishReason === 'STOP') {
    stop_reason = 'end_turn';
  } else {
    stop_reason = 'max_tokens';
  }

  return {
    id: `msg_${Date.now()}`,
    type: 'message',
    role: 'assistant',
    content: contentBlocks,
    model: model || 'gemini-2.5-flash',
    stop_reason,
    usage: {
      input_tokens: geminiResponse.usageMetadata?.promptTokenCount || 0,
      output_tokens: geminiResponse.usageMetadata?.candidatesTokenCount || 0
    }
  };
}

// Convert Anthropic messages to OpenAI chat format (with tool support)
function translateToOpenAI(anthropicRequest) {
  const messages = [];

  // System prompt
  if (anthropicRequest.system) {
    messages.push({ role: 'system', content: anthropicRequest.system });
  }

  for (const msg of anthropicRequest.messages) {
    if (typeof msg.content === 'string') {
      messages.push({ role: msg.role, content: msg.content });
      continue;
    }
    if (!Array.isArray(msg.content)) {
      messages.push({ role: msg.role, content: '' });
      continue;
    }

    // Check if this message contains tool_result blocks (becomes role: 'tool')
    const toolResults = msg.content.filter(c => c.type === 'tool_result');
    if (toolResults.length > 0) {
      for (const tr of toolResults) {
        const resultContent = Array.isArray(tr.content)
          ? tr.content.filter(c => c.type === 'text').map(c => c.text).join('\n')
          : (tr.content || '');
        messages.push({ role: 'tool', tool_call_id: tr.tool_use_id, content: resultContent });
      }
      continue;
    }

    // Assistant message — may have text + tool_use blocks
    if (msg.role === 'assistant') {
      const textParts = msg.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
      const toolUses = msg.content.filter(c => c.type === 'tool_use');

      const oaiMsg = { role: 'assistant', content: textParts || null };
      if (toolUses.length > 0) {
        oaiMsg.tool_calls = toolUses.map(tu => ({
          id: tu.id,
          type: 'function',
          function: { name: tu.name, arguments: JSON.stringify(tu.input || {}) }
        }));
      }
      messages.push(oaiMsg);
      continue;
    }

    // User message — text only (tool_results handled above)
    const textContent = msg.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
    messages.push({ role: msg.role, content: textContent });
  }

  // Translate Anthropic tools to OpenAI tools
  const tools = anthropicRequest.tools;
  const oaiTools = tools && tools.length > 0
    ? tools.map(t => ({ type: 'function', function: { name: t.name, description: t.description, parameters: t.input_schema } }))
    : undefined;

  return { messages, tools: oaiTools };
}

// ── OpenAI ↔ Anthropic format converters for /v1/chat/completions endpoint ────

// Convert an OpenAI /v1/chat/completions request body to Anthropic /v1/messages format.
// This is the inverse of translateToOpenAI().
function convertOpenAIRequestToAnthropic(oaiBody) {
  let systemText = '';
  const messages = [];

  for (const msg of (oaiBody.messages || [])) {
    if (msg.role === 'system') {
      systemText += (systemText ? '\n' : '') + (typeof msg.content === 'string' ? msg.content : '');
      continue;
    }

    // Build content array
    let content;
    if (typeof msg.content === 'string') {
      content = msg.content;
    } else if (Array.isArray(msg.content)) {
      content = msg.content.map(part => {
        if (part.type === 'text') return { type: 'text', text: part.text };
        if (part.type === 'image_url') {
          const url = part.image_url?.url || '';
          if (url.startsWith('data:')) {
            const [meta, data] = url.split(',');
            const mediaType = meta.replace('data:', '').replace(';base64', '') || 'image/jpeg';
            return { type: 'image', source: { type: 'base64', media_type: mediaType, data } };
          }
          return { type: 'image', source: { type: 'url', url } };
        }
        return { type: 'text', text: JSON.stringify(part) };
      });
    } else {
      content = '';
    }

    // Tool responses
    if (msg.role === 'tool') {
      messages.push({
        role: 'user',
        content: [{ type: 'tool_result', tool_use_id: msg.tool_call_id, content: msg.content || '' }]
      });
      continue;
    }

    // Assistant with tool_calls
    if (msg.role === 'assistant' && msg.tool_calls?.length > 0) {
      const blocks = [];
      if (msg.content) blocks.push({ type: 'text', text: msg.content });
      for (const tc of msg.tool_calls) {
        let input = {};
        try { input = JSON.parse(tc.function?.arguments || '{}'); } catch (_) {}
        blocks.push({ type: 'tool_use', id: tc.id, name: tc.function?.name, input });
      }
      messages.push({ role: 'assistant', content: blocks });
      continue;
    }

    messages.push({ role: msg.role, content });
  }

  // OpenAI tools → Anthropic tools
  const tools = oaiBody.tools?.map(t => ({
    name: t.function?.name,
    description: t.function?.description,
    input_schema: t.function?.parameters || { type: 'object', properties: {} }
  }));

  const result = {
    model:       oaiBody.model,
    max_tokens:  oaiBody.max_tokens || 4096,
    messages,
    temperature: oaiBody.temperature,
    top_p:       oaiBody.top_p,
    stream:      oaiBody.stream,
  };
  if (systemText) result.system = systemText;
  if (tools?.length) result.tools = tools;
  return result;
}

// Convert a complete Anthropic /v1/messages response to OpenAI /v1/chat/completions format.
// Thinking blocks are exposed as a non-standard `reasoning_content` field on the message
// (mirrors OpenAI o1's convention) so callers can access simulated/native reasoning.
function convertAnthropicResponseToOpenAI(anthropicResp, model) {
  const content = (anthropicResp.content || []);
  const textParts = content.filter(c => c.type === 'text').map(c => c.text).join('');
  const thinkingParts = content.filter(c => c.type === 'thinking').map(c => c.thinking || '').join('\n\n');
  const toolCalls = content.filter(c => c.type === 'tool_use').map(tu => ({
    id: tu.id,
    type: 'function',
    function: { name: tu.name, arguments: JSON.stringify(tu.input || {}) }
  }));

  const message = { role: 'assistant', content: textParts || null };
  if (toolCalls.length) message.tool_calls = toolCalls;
  if (thinkingParts) message.reasoning_content = thinkingParts; // non-standard extension

  return {
    id:      anthropicResp.id || `chatcmpl-${Date.now()}`,
    object:  'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model:   anthropicResp.model || model || 'unknown',
    choices: [{
      index:         0,
      message,
      finish_reason: anthropicResp.stop_reason === 'end_turn' ? 'stop'
                   : anthropicResp.stop_reason === 'tool_use' ? 'tool_calls'
                   : (anthropicResp.stop_reason || 'stop'),
      logprobs: null
    }],
    usage: {
      prompt_tokens:     anthropicResp.usage?.input_tokens  || 0,
      completion_tokens: anthropicResp.usage?.output_tokens || 0,
      total_tokens: (anthropicResp.usage?.input_tokens || 0) + (anthropicResp.usage?.output_tokens || 0)
    }
  };
}

// Stream a provider call (Anthropic format internally) and re-emit as OpenAI SSE chunks.
// provider already has ._lmrhModel set if LMRH selected a specific model.
async function streamAsOpenAI(provider, anthropicRequest, res) {
  const chatId = `chatcmpl-${Date.now()}`;
  const model  = anthropicRequest.model || provider.model || 'unknown';

  const emitChunk = (delta, finishReason = null) => {
    const chunk = {
      id: chatId, object: 'chat.completion.chunk', created: Math.floor(Date.now() / 1000), model,
      choices: [{ index: 0, delta, finish_reason: finishReason, logprobs: null }]
    };
    res.write(`data: ${JSON.stringify(chunk)}\n\n`);
  };

  // Emit role delta first
  emitChunk({ role: 'assistant', content: '' });

  // Use a fake res that collects Anthropic SSE events and converts to OpenAI chunks
  let textAccum = '';
  let toolCallMap = {}; // index → {id, name, argsBuffer}
  let toolCallIndex = 0;

  const fakeRes = {
    headersSent: false,
    setHeader: () => {},
    write: (data) => {
      if (typeof data !== 'string') return;
      for (const line of data.split('\n')) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (!raw || raw === '[DONE]') continue;
        let ev; try { ev = JSON.parse(raw); } catch (_) { continue; }

        if (ev.type === 'content_block_delta') {
          const delta = ev.delta;
          if (delta?.type === 'text_delta') {
            textAccum += delta.text;
            emitChunk({ content: delta.text });
          } else if (delta?.type === 'thinking_delta') {
            // Anthropic thinking delta → OpenAI reasoning_content delta (non-standard extension)
            emitChunk({ reasoning_content: delta.thinking || '' });
          } else if (delta?.type === 'input_json_delta') {
            const idx = ev.index ?? toolCallIndex;
            if (!toolCallMap[idx]) toolCallMap[idx] = { id: '', name: '', argsBuffer: '' };
            toolCallMap[idx].argsBuffer += (delta.partial_json || '');
            emitChunk({ tool_calls: [{ index: idx, function: { arguments: delta.partial_json || '' } }] });
          }
        } else if (ev.type === 'content_block_start' && ev.content_block?.type === 'tool_use') {
          const idx = ev.index ?? toolCallIndex++;
          toolCallMap[idx] = { id: ev.content_block.id, name: ev.content_block.name, argsBuffer: '' };
          emitChunk({ tool_calls: [{ index: idx, id: ev.content_block.id, type: 'function', function: { name: ev.content_block.name, arguments: '' } }] });
        } else if (ev.type === 'message_delta' && ev.delta?.stop_reason) {
          const finishReason = ev.delta.stop_reason === 'end_turn' ? 'stop'
                             : ev.delta.stop_reason === 'tool_use' ? 'tool_calls' : 'stop';
          emitChunk({}, finishReason);
        }
      }
    },
    end: () => {
      res.write('data: [DONE]\n\n');
    },
    status: (code) => ({ json: (obj) => { logger.error('Stream error from provider', obj); } })
  };

  // Dispatch through existing streaming infrastructure
  try {
    switch (provider.type) {
      case 'anthropic':         await streamAnthropic(provider, anthropicRequest, fakeRes); break;
      case 'google':            await streamGemini(provider, anthropicRequest, fakeRes); break;
      case 'openai':            await streamOpenAI(provider, anthropicRequest, fakeRes); break;
      case 'openai-compatible': await streamOpenAICompatible(provider, anthropicRequest, fakeRes); break;
      case 'grok':              await streamOpenAI(provider, anthropicRequest, fakeRes, { overrideBaseUrl: 'https://api.x.ai' }); break;
      default: throw new Error(`Unsupported provider type for streaming: ${provider.type}`);
    }
  } catch (err) {
    emitChunk({ content: `\n[Streaming error: ${err.message}]` }, 'stop');
    res.write('data: [DONE]\n\n');
    throw err;
  }
}

// Convert OpenAI response to Anthropic format (with tool_calls support)
function translateFromOpenAI(openAIResponse, model) {
  const choice = openAIResponse.choices?.[0];
  const msg = choice?.message;

  const contentBlocks = [];
  let hasToolUse = false;

  if (msg?.content) {
    contentBlocks.push({ type: 'text', text: msg.content });
  }
  if (msg?.tool_calls?.length > 0) {
    hasToolUse = true;
    for (const tc of msg.tool_calls) {
      if (!tc.function) continue;
      let inputArgs = {};
      try { inputArgs = JSON.parse(tc.function?.arguments || '{}'); } catch (_) {}
      contentBlocks.push({
        type: 'tool_use',
        id: tc.id,
        name: tc.function?.name || 'unknown_tool',
        input: inputArgs
      });
    }
  }

  if (contentBlocks.length === 0) {
    contentBlocks.push({ type: 'text', text: '' });
  }

  const finishReason = choice?.finish_reason;
  let stop_reason;
  if (hasToolUse || finishReason === 'tool_calls') {
    stop_reason = 'tool_use';
  } else if (finishReason === 'stop') {
    stop_reason = 'end_turn';
  } else {
    stop_reason = 'max_tokens';
  }

  return {
    id: openAIResponse.id || `msg_${Date.now()}`,
    type: 'message',
    role: 'assistant',
    content: contentBlocks,
    model: openAIResponse.model || model,
    stop_reason,
    usage: {
      input_tokens: openAIResponse.usage?.prompt_tokens || 0,
      output_tokens: openAIResponse.usage?.completion_tokens || 0
    }
  };
}

// ── Layer 3: Conductor/Worker Parallel Provider Racing ───────────────────────
// When CONDUCTOR_MODE is enabled, dispatch the top N providers simultaneously
// (non-streaming only) and return whichever responds first. Failed workers are
// ignored as long as at least one succeeds.
//
// CONDUCTOR_WORKERS env var controls how many providers race (default: 2).
// Streaming requests always use sequential failover (can't race SSE streams).
const CONDUCTOR_MODE    = process.env.CONDUCTOR_MODE === 'true';
const CONDUCTOR_WORKERS = Math.max(1, parseInt(process.env.CONDUCTOR_WORKERS) || 2);

async function raceProvidersParallel(providers, requestBody, maxLatencyMs) {
  const workers = providers.slice(0, CONDUCTOR_WORKERS);
  logger.info(`Conductor: racing ${workers.map(p => p.name).join(' vs ')}`);

  return new Promise((resolve, reject) => {
    let settled = false;
    let remaining = workers.length;

    workers.forEach(provider => {
      const caps = PROVIDER_CAPS[provider.type];
      const contextWindow = caps?.contextWindow || 8192;
      const body = { ...requestBody, messages: truncateMessagesToFit(requestBody.messages, Math.floor(contextWindow * 0.85)) };

      const call = (() => {
        switch (provider.type) {
          case 'anthropic':         return callAnthropic(provider, body);
          case 'google':            return callGemini(provider, body);
          case 'vertex':            return callVertex(provider, body);
          case 'grok':              return callGrok(provider, body);
          case 'ollama':            return callOllama(provider, body);
          case 'openai':            return callOpenAI(provider, body);
          case 'openai-compatible': return callOpenAICompatible(provider, body);
          default: return Promise.reject(new Error(`Unsupported type: ${provider.type}`));
        }
      })();

      const raceCall = maxLatencyMs > 0
        ? Promise.race([call, new Promise((_, r) => setTimeout(() => r(new Error(`Latency exceeded ${maxLatencyMs}ms`)), maxLatencyMs))])
        : call;

      raceCall.then(result => {
        if (settled) return;
        // XML sentinel check
        const text = result?.content?.map(b => b.text || '').join('') || '';
        if (isBadModelOutput(text)) {
          logger.warn(`Conductor: XML sentinel on ${provider.name} — ignoring this worker`);
          remaining--;
          if (remaining === 0 && !settled) reject(new Error('All conductor workers failed'));
          return;
        }
        settled = true;
        logger.info(`Conductor: winner is ${provider.name}`);
        resolve({ result, provider });
      }).catch(err => {
        logger.warn(`Conductor worker ${provider.name} failed: ${err.message}`);
        remaining--;
        if (remaining === 0 && !settled) reject(new Error('All conductor workers failed'));
      });
    });
  });
}

// ── Layer 1d: Streaming First-Chunk Buffer ────────────────────────────────────
// Wraps a streaming call to buffer all SSE writes until the first data chunk
// arrives from the provider. This keeps headers unsent long enough for the
// latency guard (Promise.race) to still be able to failover if the provider
// hangs at the connection stage. Once any chunk arrives we flush the buffer
// and switch to direct passthrough for the remainder of the stream.
function makeBufferedRes(res) {
  let flushed = false;
  const buffer = [];

  const bufferedRes = {
    setHeader: (k, v) => { if (!flushed) buffer.push({ type: 'header', k, v }); else res.setHeader(k, v); },
    write: (chunk) => {
      if (!flushed) {
        // First write — flush headers then buffered writes
        flushed = true;
        for (const item of buffer) {
          if (item.type === 'header') res.setHeader(item.k, item.v);
        }
        buffer.length = 0;
        res.write(chunk);
      } else {
        res.write(chunk);
      }
    },
    end: (...args) => res.end(...args),
    // Expose flush state for the routing layer
    get headersFlushed() { return flushed; }
  };
  return bufferedRes;
}

// Stream Anthropic API responses
async function streamAnthropic(provider, request, res) {
  const response = await axios.post(
    'https://api.anthropic.com/v1/messages',
    {
      model: request.model || 'claude-sonnet-4-5-20250929',
      max_tokens: request.max_tokens || 4096,
      messages: request.messages,
      system: request.system,
      tools: request.tools,
      temperature: request.temperature,
      top_p: request.top_p,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': provider.apiKey,
        'anthropic-version': '2023-06-01'
      },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  // Pipe the SSE stream directly to client
  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}

// Stream Google Gemini API responses (translate to Anthropic SSE format, with tool_use support)
async function streamGemini(provider, request, res, opts = {}) {
  const geminiRequest = translateToGemini(request);
  const model = request.model?.includes('gemini') ? request.model : 'gemini-2.5-flash';
  if (request._geminiThinkingBudget) {
    geminiRequest.generationConfig = geminiRequest.generationConfig || {};
    geminiRequest.generationConfig.thinkingConfig = { thinkingBudget: request._geminiThinkingBudget };
  }

  const response = await axios.post(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:streamGenerateContent?key=${provider.apiKey}&alt=sse`,
    geminiRequest,
    {
      headers: { 'Content-Type': 'application/json' },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  const messageId = `msg_${Date.now()}`;
  const streamStartTime = Date.now();

  // SSE emit helper
  const emit = (eventName, data) => {
    res.write(`event: ${eventName}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  if (!opts.skipMessageStart) {
    emit('message_start', {
      type: 'message_start',
      message: { id: messageId, type: 'message', role: 'assistant', content: [], model, usage: { input_tokens: 0, output_tokens: 0 } }
    });
  }

  let blockIndex = opts.blockIndexOffset || 0;
  let textBlockOpen = false;
  let thinkingBlockOpen = false;
  let hasToolUse = false;
  let outputTokens = 0;
  let textAccum = '';
  let thinkingPhase = 'IDLE';

  let lineBuffer = '';

  const processChunk = (data) => {
    const parts = data.candidates?.[0]?.content?.parts || [];
    for (const part of parts) {
      if (part.thought === true) {
        if (thinkingPhase === 'TEXT') continue;
        if (thinkingPhase === 'IDLE') {
          emit('content_block_start', { type: 'content_block_start', index: blockIndex, content_block: { type: 'thinking', thinking: '' } });
          thinkingBlockOpen = true;
          thinkingPhase = 'THINKING';
        }
        if (part.text) emit('content_block_delta', { type: 'content_block_delta', index: blockIndex, delta: { type: 'thinking_delta', thinking: part.text } });
        continue;
      }
      if (part.text) {
        if (thinkingBlockOpen) {
          emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
          blockIndex++;
          thinkingBlockOpen = false;
          thinkingPhase = 'TEXT';
        }
        if (!textBlockOpen) {
          emit('content_block_start', { type: 'content_block_start', index: blockIndex, content_block: { type: 'text', text: '' } });
          textBlockOpen = true;
          if (thinkingPhase === 'IDLE') thinkingPhase = 'TEXT';
        }
        outputTokens += part.text.length;
        textAccum += part.text;
        emit('content_block_delta', { type: 'content_block_delta', index: blockIndex, delta: { type: 'text_delta', text: part.text } });
      } else if (part.functionCall) {
        if (thinkingBlockOpen) {
          emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
          blockIndex++;
          thinkingBlockOpen = false;
        }
        if (textBlockOpen) {
          emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
          blockIndex++;
          textBlockOpen = false;
        }
        hasToolUse = true;
        const toolId = generateToolId();
        const argsJson = JSON.stringify(part.functionCall.args || {});
        emit('content_block_start', { type: 'content_block_start', index: blockIndex, content_block: { type: 'tool_use', id: toolId, name: part.functionCall.name, input: {} } });
        emit('content_block_delta', { type: 'content_block_delta', index: blockIndex, delta: { type: 'input_json_delta', partial_json: argsJson } });
        emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
        blockIndex++;
      }
    }
    // Accumulate usage metadata if present
    if (data.usageMetadata?.candidatesTokenCount) {
      outputTokens = data.usageMetadata.candidatesTokenCount;
    }
  };

  response.data.on('data', (chunk) => {
    lineBuffer += chunk.toString();
    const lines = lineBuffer.split('\n');
    lineBuffer = lines.pop(); // Keep incomplete last line

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          processChunk(data);
        } catch (e) {
          // Skip unparseable lines (e.g. "[DONE]" or empty)
        }
      }
    }
  });

  return new Promise((resolve, reject) => {
    response.data.on('end', () => {
      if (thinkingBlockOpen) {
        emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
        blockIndex++;
      }
      if (textBlockOpen) {
        emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
      }

      emit('message_delta', {
        type: 'message_delta',
        delta: { stop_reason: hasToolUse ? 'tool_use' : 'end_turn', stop_sequence: null },
        usage: { output_tokens: outputTokens }
      });
      emit('message_stop', { type: 'message_stop' });

      logChatStreamResponse(provider.name, model, textAccum, Date.now() - streamStartTime, outputTokens);
      resolve();
    });

    response.data.on('error', reject);
  });
}

// Stream OpenAI API responses (translate to Anthropic SSE format, with tool_use support)
async function streamOpenAI(provider, request, res, opts = {}) {
  const { messages: convertedMessages, tools: oaiTools } = translateToOpenAI(request);

  const body = {
    model: provider.model || 'gpt-4o-mini',
    messages: convertedMessages,
    max_tokens: request.max_tokens || 4096,
    temperature: request.temperature,
    top_p: request.top_p,
    stream: true
  };
  if (oaiTools) body.tools = oaiTools;
  if (request._reasoningEffort) body.reasoning_effort = request._reasoningEffort;

  const apiUrl = opts.overrideBaseUrl
    ? `${opts.overrideBaseUrl}/v1/chat/completions`
    : 'https://api.openai.com/v1/chat/completions';

  const response = await axios.post(
    apiUrl,
    body,
    {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${provider.apiKey}` },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  const messageId = `msg_${Date.now()}`;
  const streamStartTime = Date.now();
  const model = request.model || provider.model || 'gpt-4o-mini';

  const emit = (eventName, data) => {
    res.write(`event: ${eventName}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };

  if (!opts.skipMessageStart) {
    emit('message_start', {
      type: 'message_start',
      message: { id: messageId, type: 'message', role: 'assistant', content: [], model, usage: { input_tokens: 0, output_tokens: 0 } }
    });
  }

  // Track state for text and tool_call blocks
  let blockIndex = opts.blockIndexOffset || 0;
  let textBlockOpen = false;
  let hasToolUse = false;
  let outputTokens = 0;
  let textAccum = ''; // for chat log

  // tool_calls accumulator: index → {id, name, argsBuffer}
  const toolCallAccum = {};
  let lineBuffer = '';

  const processSSELine = (line) => {
    if (!line.startsWith('data: ')) return;
    const payload = line.slice(6).trim();
    if (payload === '[DONE]') return;

    let data;
    try { data = JSON.parse(payload); } catch (_) { return; }

    const delta = data.choices?.[0]?.delta;
    const finishReason = data.choices?.[0]?.finish_reason;

    if (data.usage?.completion_tokens) outputTokens = data.usage.completion_tokens;

    if (!delta) return;

    // Text content
    if (delta.content) {
      if (!textBlockOpen) {
        emit('content_block_start', { type: 'content_block_start', index: blockIndex, content_block: { type: 'text', text: '' } });
        textBlockOpen = true;
      }
      textAccum += delta.content;
      emit('content_block_delta', { type: 'content_block_delta', index: blockIndex, delta: { type: 'text_delta', text: delta.content } });
    }

    // Tool calls (streamed incrementally by OpenAI)
    if (delta.tool_calls) {
      // Close text block first if open
      if (textBlockOpen) {
        emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
        blockIndex++;
        textBlockOpen = false;
      }
      for (const tc of delta.tool_calls) {
        const tcIdx = tc.index;
        if (!toolCallAccum[tcIdx]) {
          // blockBaseForTools is fixed at the time the first tool_call appears,
          // so all tool blocks get consistent indices regardless of later blockIndex changes
          toolCallAccum[tcIdx] = { id: tc.id || generateToolId(), name: '', argsBuffer: '', emittedStart: false, blockOffset: blockIndex };
        }
        const accum = toolCallAccum[tcIdx];
        if (tc.id && !accum.id) accum.id = tc.id;
        // Name only arrives in the first chunk — assign, don't append
        if (tc.function?.name && !accum.name) accum.name = tc.function.name;
        if (tc.function?.arguments) accum.argsBuffer += tc.function.arguments;

        // Emit start block once we have the name
        if (!accum.emittedStart && accum.name) {
          hasToolUse = true;
          const absIdx = accum.blockOffset + tcIdx;
          emit('content_block_start', { type: 'content_block_start', index: absIdx, content_block: { type: 'tool_use', id: accum.id, name: accum.name, input: {} } });
          accum.emittedStart = true;
          accum.absIdx = absIdx;
        }
        // Stream args delta
        if (tc.function?.arguments && accum.emittedStart) {
          emit('content_block_delta', { type: 'content_block_delta', index: accum.absIdx, delta: { type: 'input_json_delta', partial_json: tc.function.arguments } });
        }
      }
    }
  };

  response.data.on('data', (chunk) => {
    lineBuffer += chunk.toString();
    const lines = lineBuffer.split('\n');
    lineBuffer = lines.pop();
    for (const line of lines) processSSELine(line.trim());
  });

  return new Promise((resolve, reject) => {
    response.data.on('end', () => {
      // Close text block if open
      if (textBlockOpen) {
        emit('content_block_stop', { type: 'content_block_stop', index: blockIndex });
        blockIndex++;
      }
      // Close any open tool_call blocks (use the stored absolute index)
      for (const accum of Object.values(toolCallAccum)) {
        if (accum.emittedStart) {
          emit('content_block_stop', { type: 'content_block_stop', index: accum.absIdx });
        }
      }
      emit('message_delta', {
        type: 'message_delta',
        delta: { stop_reason: hasToolUse ? 'tool_use' : 'end_turn', stop_sequence: null },
        usage: { output_tokens: outputTokens }
      });
      emit('message_stop', { type: 'message_stop' });
      logChatStreamResponse(provider.name, model, textAccum, Date.now() - streamStartTime, outputTokens);
      resolve();
    });
    response.data.on('error', reject);
  });
}

// Stream Grok API responses
async function streamGrok(provider, request, res) {
  const response = await axios.post(
    'https://api.x.ai/v1/chat/completions',
    {
      model: provider.model || 'grok-beta',
      messages: request.messages,
      max_tokens: request.max_tokens || 4096,
      temperature: request.temperature,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}

// Stream Ollama API responses
async function streamOllama(provider, request, res) {
  const baseUrl = provider.baseUrl || 'http://localhost:11434';
  const response = await axios.post(
    `${baseUrl}/api/chat`,
    {
      model: request.model || provider.model || 'llama2',
      messages: request.messages,
      stream: true
    },
    {
      headers: { 'Content-Type': 'application/json' },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}

// Stream OpenAI-Compatible API responses
async function streamOpenAICompatible(provider, request, res) {
  const baseUrl = provider.baseUrl || 'http://localhost:8080';
  const response = await axios.post(
    `${baseUrl}/v1/chat/completions`,
    {
      model: provider.model || 'gpt-3.5-turbo',
      messages: request.messages,
      max_tokens: request.max_tokens || 4096,
      temperature: request.temperature,
      stream: true
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      responseType: 'stream',
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// Provider Augmentation Layer (claude-code key type)
// ─────────────────────────────────────────────────────────────────────────────

// ── CoT pipeline constants ────────────────────────────────────────────────────
const COT_MAX_ITERATIONS     = 1;   // refinement rounds after initial draft (1 = 4 calls total)
const COT_QUALITY_THRESHOLD  = 6;   // score < this triggers a refinement pass (1-10 scale)
const COT_CRITIQUE_MAX_TOKENS = 200;
const COT_TEXT_CHUNK_SIZE    = 80;  // chars per text_delta chunk when simulating streaming

const COT_PRE_ANALYSIS_PROMPT = `You are analyzing a task before responding. Think briefly through:
1. What service, component, or system is involved?
2. What are the 2-3 most likely root causes based on the description?
3. What diagnostic steps would confirm or rule them out?
4. Is there a config file pattern issue (e.g., *-Sample.json exists but the real config is missing)?
5. Should this be investigated locally before delegating to peer systems?
Respond in under 250 words. This analysis will be prepended to improve your main response.`;

const COT_REASONING_INJECTION = `\n\n## Diagnostic Reasoning\nFollow: OBSERVE → HYPOTHESIZE → TEST → FIX.\nConfig rule: if *-Sample.*, *-Example.*, or *-Template.* config exists but the plain version does not, the real config is MISSING — that is the root cause. Copy it and edit with correct values.\nDelegation rule: run at least 3 local diagnostic commands before delegating to peer systems.`;

const COT_CRITIQUE_PROMPT = `You are a quality evaluator for AI responses.
Given the original task and a draft response, rate the response quality.
Respond EXACTLY in this format — no other text:
SCORE: <number 1-10>
GAPS: <one sentence describing the key issue, or "none" if the response is complete and accurate>`;

const COT_REFINE_SYSTEM = `You are improving an AI response based on a quality critique.
Produce a complete, improved response that directly addresses the identified gaps.
Be specific, accurate, and concise. Do not reference the critique in your response.`;

// ── Helpers ───────────────────────────────────────────────────────────────────

function getAugmentationMode(provider, clientKey, lmrhHint, modelCaps) {
  // LMRH Phase 4: task=reasoning hint on a non-native-reasoning model → engage CoT
  if (lmrhHint?.task === 'reasoning' && modelCaps?.native_reasoning === false) {
    return 'cot-pipeline';
  }
  if (!clientKey || clientKey.keyType !== 'claude-code') return 'passthrough';
  const type = provider.type;
  const model = (provider.model || '').toLowerCase();
  if (type === 'anthropic') return 'passthrough';
  if (type === 'openai' && /^o[1-4]/.test(model)) return 'native-o-series';
  if (type === 'google' && model.includes('gemini-2.5')) return 'native-gemini-thinking';
  return 'cot-pipeline';
}

function extractLastUserText(messages) {
  const userMsgs = (messages || []).filter(m => m.role === 'user');
  if (!userMsgs.length) return '';
  const last = userMsgs[userMsgs.length - 1];
  if (typeof last.content === 'string') return last.content.slice(0, 2000);
  if (Array.isArray(last.content)) return last.content.filter(b => b.type === 'text').map(b => b.text || '').join('\n').slice(0, 2000);
  return '';
}

function extractResponseText(result) {
  if (!result) return '';
  if (Array.isArray(result.content)) return result.content.filter(b => b.type === 'text').map(b => b.text || '').join('');
  if (typeof result.content === 'string') return result.content;
  return '';
}

function buildAugmentedRequest(request, preAnalysis) {
  const origSystem = typeof request.system === 'string' ? request.system
    : Array.isArray(request.system) ? request.system.filter(s => s.type === 'text').map(s => s.text).join('\n')
    : '';
  const augmentedSystem = origSystem + COT_REASONING_INJECTION;
  if (!preAnalysis) return { ...request, system: augmentedSystem };
  const messages = [...(request.messages || [])];
  if (messages.length > 0) {
    const last = messages[messages.length - 1];
    if (last.role === 'user') {
      const origContent = typeof last.content === 'string' ? last.content
        : Array.isArray(last.content) ? last.content.filter(b => b.type === 'text').map(b => b.text).join('\n')
        : '';
      messages[messages.length - 1] = { ...last, content: `[Pre-analysis: ${preAnalysis}]\n\n${origContent}` };
    }
  }
  return { ...request, system: augmentedSystem, messages };
}

// Route a non-streaming call to the correct provider function.
async function callProviderSync(provider, request) {
  switch (provider.type) {
    case 'anthropic':         return callAnthropic(provider, request);
    case 'google':            return callGemini(provider, request);
    case 'grok':              return callGrok(provider, request);
    case 'ollama':            return callOllama(provider, request);
    case 'openai':            return callOpenAI(provider, request);
    case 'openai-compatible': return callOpenAICompatible(provider, request);
    default:                  return callOpenAICompatible(provider, request);
  }
}

// Emit a complete thinking block (start → single delta → stop).
function emitThinkingBlock(emit, index, text) {
  emit('content_block_start', { type: 'content_block_start', index, content_block: { type: 'thinking', thinking: '' } });
  emit('content_block_delta', { type: 'content_block_delta', index, delta: { type: 'thinking_delta', thinking: text } });
  emit('content_block_stop', { type: 'content_block_stop', index });
}

// Emit a text block as simulated streaming chunks, then close the message.
function emitTextBlock(emit, index, text) {
  emit('content_block_start', { type: 'content_block_start', index, content_block: { type: 'text', text: '' } });
  for (let i = 0; i < text.length; i += COT_TEXT_CHUNK_SIZE) {
    emit('content_block_delta', { type: 'content_block_delta', index, delta: { type: 'text_delta', text: text.slice(i, i + COT_TEXT_CHUNK_SIZE) } });
  }
  emit('content_block_stop', { type: 'content_block_stop', index });
  emit('message_delta', { type: 'message_delta', delta: { stop_reason: 'end_turn', stop_sequence: null }, usage: { output_tokens: Math.ceil(text.length / 4) } });
  emit('message_stop', { type: 'message_stop' });
}

// ── Non-streaming CoT pipeline (used for non-streaming requests) ──────────────
async function cotPipeline(provider, request) {
  const userText = extractLastUserText(request.messages) || 'Analyze the task.';

  // Pass 0: pre-analysis
  let preAnalysis = '';
  try {
    const preResult = await callProviderSync(provider, {
      model: request.model, max_tokens: 400, temperature: 0.3,
      messages: [{ role: 'user', content: userText }],
      system: COT_PRE_ANALYSIS_PROMPT,
    });
    preAnalysis = extractResponseText(preResult).slice(0, 500);
  } catch (e) {
    logger.warn(`CoT pre-analysis skipped for ${provider.name}: ${e.message}`);
  }

  // Pass 1: initial draft
  const augmented = buildAugmentedRequest(request, preAnalysis);
  let draft = '';
  try {
    const draftResult = await callProviderSync(provider, augmented);
    draft = extractResponseText(draftResult);
  } catch (e) {
    logger.warn(`CoT initial draft failed for ${provider.name}: ${e.message}`);
    return { content: [{ type: 'text', text: '' }] };
  }

  // Iterative refinement
  let finalAnswer = draft;
  for (let iter = 0; iter < COT_MAX_ITERATIONS; iter++) {
    try {
      const critiqueResult = await callProviderSync(provider, {
        model: request.model, max_tokens: COT_CRITIQUE_MAX_TOKENS, temperature: 0.2,
        messages: [{ role: 'user', content: `Task: ${userText.slice(0, 1000)}\n\nResponse:\n${finalAnswer.slice(0, 2000)}` }],
        system: COT_CRITIQUE_PROMPT,
      });
      const critiqueText = extractResponseText(critiqueResult);
      const scoreMatch = critiqueText.match(/SCORE:\s*(\d+)/i);
      const gapsMatch  = critiqueText.match(/GAPS:\s*(.+)/i);
      const score = scoreMatch ? parseInt(scoreMatch[1], 10) : 10;
      const gaps  = gapsMatch ? gapsMatch[1].trim() : 'none';
      if (score >= COT_QUALITY_THRESHOLD || gaps.toLowerCase() === 'none') break;
      const refineResult = await callProviderSync(provider, {
        model: request.model, max_tokens: request.max_tokens || 4096,
        temperature: request.temperature || 0.7,
        system: COT_REFINE_SYSTEM,
        messages: [{ role: 'user', content: `Original task: ${userText.slice(0, 1000)}\n\nDraft:\n${finalAnswer.slice(0, 2000)}\n\nCritique: ${gaps}\n\nImproved response:` }],
      });
      const refined = extractResponseText(refineResult);
      if (refined) finalAnswer = refined;
    } catch (e) {
      logger.warn(`CoT refinement skipped (iter ${iter}) for ${provider.name}: ${e.message}`);
      break;
    }
  }

  return { content: [{ type: 'text', text: finalAnswer }] };
}

// ── Streaming CoT pipeline with iterative refinement ─────────────────────────
async function streamCotPipeline(provider, request, res, httpReq) {
  const sessionId  = httpReq.headers['x-session-id'] || null;
  const messageId  = `msg_${Date.now()}`;
  const model      = request.model || provider.model || 'unknown';
  const emit = (eventName, data) => {
    res.write(`event: ${eventName}\n`);
    res.write(`data: ${JSON.stringify(data)}\n\n`);
  };
  const userText = extractLastUserText(request.messages) || 'Analyze the task.';
  let blockIndex = 0;

  emit('message_start', { type: 'message_start', message: { id: messageId, type: 'message', role: 'assistant', content: [], model, usage: { input_tokens: 0, output_tokens: 0 } } });

  // ── Pass 0: Planning / pre-analysis ────────────────────────────────────────
  let preAnalysis = '';
  try {
    const priorAnalyses = await getSessionAnalyses(sessionId);
    let analysisInput = userText;
    if (priorAnalyses.length > 0)
      analysisInput = `Prior investigation context:\n${priorAnalyses.join('\n---\n')}\n\nCurrent task:\n${userText}`;
    const preResult = await callProviderSync(provider, {
      model: request.model, max_tokens: 400, temperature: 0.3,
      messages: [{ role: 'user', content: analysisInput }],
      system: COT_PRE_ANALYSIS_PROMPT,
    });
    preAnalysis = extractResponseText(preResult).slice(0, 500);
  } catch (e) {
    logger.warn(`CoT pre-analysis failed for ${provider.name}: ${e.message}`);
  }
  if (preAnalysis)
    emitThinkingBlock(emit, blockIndex++, `## Planning\n${preAnalysis}`);

  // ── Pass 1: Initial draft (non-streaming — needed for critique) ─────────────
  let draft = '';
  try {
    const draftResult = await callProviderSync(provider, buildAugmentedRequest(request, preAnalysis));
    draft = extractResponseText(draftResult);
  } catch (e) {
    logger.warn(`CoT initial draft failed for ${provider.name}: ${e.message}`);
  }
  if (!draft) {
    emitTextBlock(emit, blockIndex, '(No response from provider)');
    return;
  }

  // ── Iterative refinement loop ───────────────────────────────────────────────
  let finalAnswer = draft;
  for (let iter = 0; iter < COT_MAX_ITERATIONS; iter++) {
    let score = 10;
    let gaps  = 'none';
    try {
      const critiqueResult = await callProviderSync(provider, {
        model: request.model, max_tokens: COT_CRITIQUE_MAX_TOKENS, temperature: 0.2,
        messages: [{ role: 'user', content: `Task: ${userText.slice(0, 1000)}\n\nResponse to evaluate:\n${finalAnswer.slice(0, 2000)}` }],
        system: COT_CRITIQUE_PROMPT,
      });
      const critiqueText = extractResponseText(critiqueResult);
      const scoreMatch = critiqueText.match(/SCORE:\s*(\d+)/i);
      const gapsMatch  = critiqueText.match(/GAPS:\s*(.+)/i);
      if (scoreMatch) score = parseInt(scoreMatch[1], 10);
      if (gapsMatch)  gaps  = gapsMatch[1].trim();
      emitThinkingBlock(emit, blockIndex++, `## Quality Check (pass ${iter + 1})\nScore: ${score}/10\nGaps: ${gaps}`);
    } catch (e) {
      logger.warn(`CoT critique failed (iter ${iter}) for ${provider.name}: ${e.message}`);
      break;
    }

    if (score >= COT_QUALITY_THRESHOLD || gaps.toLowerCase() === 'none') break;

    try {
      const refineResult = await callProviderSync(provider, {
        model: request.model,
        max_tokens: request.max_tokens || 4096,
        temperature: request.temperature || 0.7,
        system: COT_REFINE_SYSTEM,
        messages: [{ role: 'user', content: `Original task: ${userText.slice(0, 1000)}\n\nDraft response:\n${finalAnswer.slice(0, 2000)}\n\nCritique: ${gaps}\n\nProvide a complete improved response:` }],
      });
      const refined = extractResponseText(refineResult);
      if (refined) {
        emitThinkingBlock(emit, blockIndex++, `## Refinement (pass ${iter + 1})\n${refined.slice(0, 300)}${refined.length > 300 ? '…' : ''}`);
        finalAnswer = refined;
      }
    } catch (e) {
      logger.warn(`CoT refinement failed (iter ${iter}) for ${provider.name}: ${e.message}`);
      break;
    }
  }

  // ── Save session analysis for multi-turn context ────────────────────────────
  if (preAnalysis) await saveSessionAnalysis(sessionId, preAnalysis).catch(() => {});

  // ── Emit final answer as simulated streaming text ───────────────────────────
  emitTextBlock(emit, blockIndex, finalAnswer);
}

async function dispatchProviderCall(provider, request, clientKey, lmrhHint, modelCaps) {
  const mode = getAugmentationMode(provider, clientKey, lmrhHint, modelCaps);
  if (mode === 'cot-pipeline') {
    if (lmrhHint?.task === 'reasoning' && modelCaps?.native_reasoning === false) {
      logger.info(`LMRH CoT auto-engaged: task=reasoning on non-native-reasoning model ${provider._lmrhModel || provider.model || 'unknown'} via ${provider.name}`);
    }
    return cotPipeline(provider, request);
  }
  let augmented = request;
  if (mode === 'native-o-series') augmented = { ...request, _reasoningEffort: 'high' };
  else if (mode === 'native-gemini-thinking') {
    const budget = (provider.model || '').toLowerCase().includes('pro') ? 8000 : 5000;
    augmented = { ...request, _geminiThinkingBudget: budget };
  }
  switch (provider.type) {
    case 'anthropic':         return callAnthropic(provider, augmented);
    case 'google':            return callGemini(provider, augmented);
    case 'grok':              return callGrok(provider, augmented);
    case 'ollama':            return callOllama(provider, augmented);
    case 'openai':            return callOpenAI(provider, augmented);
    case 'openai-compatible': return callOpenAICompatible(provider, augmented);
    default: throw new Error(`Unsupported provider type: ${provider.type}`);
  }
}

// Non-streaming calls (original functionality)
async function callAnthropic(provider, request) {
  const response = await axios.post(
    'https://api.anthropic.com/v1/messages',
    {
      model: request.model || 'claude-sonnet-4-5-20250929',
      max_tokens: request.max_tokens || 4096,
      messages: request.messages,
      system: request.system,
      tools: request.tools,
      temperature: request.temperature,
      top_p: request.top_p,
      stream: false
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'x-api-key': provider.apiKey,
        'anthropic-version': '2023-06-01'
      },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  return response.data;
}

async function callGemini(provider, request) {
  const geminiRequest = translateToGemini(request);
  const model = request.model?.includes('gemini') ? request.model : 'gemini-2.5-flash';

  const response = await axios.post(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${provider.apiKey}`,
    geminiRequest,
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  return translateFromGemini(response.data, model);
}

// Vertex AI (Google Cloud)
// Note: Vertex AI typically requires OAuth 2.0 tokens, not API keys
// For simple API key usage, use "Google Generative AI (Gemini)" instead
async function callVertex(provider, request) {
  const geminiRequest = translateToGemini(request);
  const model = provider.model || 'gemini-2.5-flash';
  const location = provider.location || 'us-central1';
  const projectId = provider.projectId;

  if (!projectId) {
    throw new Error('Vertex AI requires Project ID. For simple API key access, use "Google Generative AI (Gemini)" instead.');
  }

  if (!provider.apiKey) {
    throw new Error('Vertex AI requires an OAuth 2.0 access token. For API key access, use "Google Generative AI (Gemini)" instead.');
  }

  // Try the Vertex AI endpoint with the provided credentials
  // This will work with OAuth tokens or service account credentials
  const response = await axios.post(
    `https://${location}-aiplatform.googleapis.com/v1/projects/${projectId}/locations/${location}/publishers/google/models/${model}:generateContent`,
    geminiRequest,
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  return translateFromGemini(response.data, model);
}

// Grok (xAI)
async function callGrok(provider, request) {
  const response = await axios.post(
    'https://api.x.ai/v1/chat/completions',
    {
      model: provider.model || 'grok-beta',
      max_tokens: request.max_tokens || 4096,
      messages: request.messages,
      temperature: request.temperature,
      top_p: request.top_p,
      stream: false
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  // Convert OpenAI format to Anthropic format
  const choice = response.data.choices[0];
  return {
    id: response.data.id,
    type: 'message',
    role: 'assistant',
    content: [{ type: 'text', text: choice.message.content }],
    model: response.data.model,
    stop_reason: choice.finish_reason === 'stop' ? 'end_turn' : choice.finish_reason,
    usage: {
      input_tokens: response.data.usage?.prompt_tokens || 0,
      output_tokens: response.data.usage?.completion_tokens || 0
    }
  };
}

// Ollama (self-hosted)
async function callOllama(provider, request) {
  const baseUrl = provider.baseUrl || 'http://localhost:11434';
  const model = provider.model || 'llama2';

  const response = await axios.post(
    `${baseUrl}/api/generate`,
    {
      model: model,
      prompt: request.messages.map(m => m.content).join('\n\n'),
      stream: false
    },
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  return {
    id: `msg_${Date.now()}`,
    type: 'message',
    role: 'assistant',
    content: [{ type: 'text', text: response.data.response }],
    model: model,
    stop_reason: 'end_turn',
    usage: {
      input_tokens: 0,
      output_tokens: 0
    }
  };
}

// OpenAI (official API)
async function callOpenAI(provider, request) {
  const { messages: convertedMessages, tools: oaiTools } = translateToOpenAI(request);

  const body = {
    model: provider.model || 'gpt-4o-mini',
    max_tokens: request.max_tokens || 4096,
    messages: convertedMessages,
    temperature: request.temperature,
    top_p: request.top_p,
    stream: false
  };
  if (oaiTools) body.tools = oaiTools;

  const response = await axios.post(
    'https://api.openai.com/v1/chat/completions',
    body,
    {
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${provider.apiKey}` },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  return translateFromOpenAI(response.data, request.model || provider.model || 'gpt-4o-mini');
}

// OpenAI-compatible API (for 3rd party services)
async function callOpenAICompatible(provider, request) {
  const baseUrl = provider.baseUrl || 'https://api.openai.com/v1';

  const response = await axios.post(
    `${baseUrl}/chat/completions`,
    {
      model: provider.model || 'gpt-3.5-turbo',
      max_tokens: request.max_tokens || 4096,
      messages: request.messages,
      temperature: request.temperature,
      top_p: request.top_p,
      stream: false
    },
    {
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${provider.apiKey}`
      },
      timeout: provider.type === 'ollama' ? 60000 : 30000
    }
  );

  // Convert OpenAI format to Anthropic format
  const choice = response.data.choices[0];
  return {
    id: response.data.id,
    type: 'message',
    role: 'assistant',
    content: [{ type: 'text', text: choice.message.content }],
    model: response.data.model,
    stop_reason: choice.finish_reason === 'stop' ? 'end_turn' : choice.finish_reason,
    usage: {
      input_tokens: response.data.usage?.prompt_tokens || 0,
      output_tokens: response.data.usage?.completion_tokens || 0
    }
  };
}

// ── OpenAI-format chat completions endpoint (/v1/chat/completions) ─────────────
// Accepts OpenAI API requests, routes via LMRH + provider priority,
// dispatches to any provider (translating format), returns OpenAI format.
app.post('/v1/chat/completions', validateApiKey, async (req, res) => {
  const startTime  = Date.now();
  const isStreaming = req.body.stream === true;

  // Convert OpenAI request to internal Anthropic format for routing/dispatch
  const anthropicBody = convertOpenAIRequestToAnthropic(req.body);

  // Build provider list (same logic as /v1/messages)
  const sortedProviders = config.providers
    .filter(p => p.enabled && p.apiKey)
    .sort((a, b) => a.priority - b.priority);
  const availableProviders = sortedProviders.filter(p => !providerMonitor.isInHoldDown(p));

  if (availableProviders.length === 0)
    return res.status(503).json({ error: 'No providers available' });

  // LMRH routing
  const lmrhHint = parseLmrhHint(req.headers['llm-hint'] || req.headers['LLM-Hint']);
  let routingProviders = availableProviders;
  if (lmrhHint) {
    const { rankedProviders, unmetAffinities } =
      rankProvidersWithHint(availableProviders, lmrhHint, USE_SQLITE ? sqliteDb : null, anthropicBody.model);
    if (rankedProviders.length === 0 && lmrhHint.hard.size > 0)
      return res.status(503).json({ error: 'no_provider_satisfies_constraints', failed: [...lmrhHint.hard] });
    if (rankedProviders.length > 0) {
      routingProviders = rankedProviders;
      logger.info(`LMRH/oai routing: ${lmrhHint.task || 'chat'} → ${rankedProviders[0].name}` +
        (unmetAffinities.length ? ` (unmet: ${unmetAffinities.join(',')})` : ''));
    }
  }

  if (isStreaming) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('X-Accel-Buffering', 'no');
    res.flushHeaders();
  }

  for (const provider of routingProviders) {
    try {
      if (provider._lmrhModel) anthropicBody.model = provider._lmrhModel;
      initStats(provider.id);
      config.stats[provider.id].requests++;

      // Apply provider emulation (PBTC, PBRC, vision-strip) on the translated Anthropic body
      const { requestBody: emuBody, usePbtc: _ePbtc, usePbrc: _ePbrc } = applyProviderEmulation(provider, anthropicBody);

      if (isStreaming) {
        // Emulated streaming (PBTC/PBRC): collect non-streaming, post-process, emit OpenAI SSE
        if (_ePbtc || _ePbrc) {
          const _emuRaw = await dispatchProviderCall(provider, { ...emuBody, stream: false }, req.clientKey, lmrhHint, null);
          let _emuResult = _emuRaw;
          if (_ePbtc) _emuResult = pbtcPostprocess(_emuResult, req.body.tools);
          if (_ePbrc) _emuResult = pbrcPostprocess(_emuResult);
          // Re-emit as OpenAI SSE by converting thinking blocks to reasoning_content deltas
          const chatId = `chatcmpl-${Date.now()}`;
          const emitOaiChunk = (delta, finishReason = null) => {
            res.write(`data: ${JSON.stringify({ id: chatId, object: 'chat.completion.chunk',
              created: Math.floor(Date.now() / 1000), model: emuBody.model || provider.model || 'unknown',
              choices: [{ index: 0, delta, finish_reason: finishReason, logprobs: null }] })}\n\n`);
          };
          emitOaiChunk({ role: 'assistant', content: '' });
          for (const block of (_emuResult.content || [])) {
            if (block.type === 'thinking') emitOaiChunk({ reasoning_content: block.thinking || '' });
            else if (block.type === 'text') emitOaiChunk({ content: block.text || '' });
            else if (block.type === 'tool_use') {
              emitOaiChunk({ tool_calls: [{ index: 0, id: block.id, type: 'function', function: { name: block.name, arguments: JSON.stringify(block.input || {}) } }] });
            }
          }
          const finishReason = _emuResult.stop_reason === 'tool_use' ? 'tool_calls' : 'stop';
          emitOaiChunk({}, finishReason);
          res.write('data: [DONE]\n\n');
        } else {
          await streamAsOpenAI(provider, emuBody, res);
        }
        config.stats[provider.id].successes++;
        config.stats[provider.id].totalLatency += Date.now() - startTime;
        providerMonitor.recordSuccess(provider);
        if (req.clientKey) {
          req.clientKey.requests = (req.clientKey.requests || 0) + 1;
          req.clientKey.lastUsed = new Date().toISOString();
          saveApiKeyRecord(req.clientKey);
        }
        res.end();
        return;
      }

      // Non-streaming: dispatch, apply emulation post-processing, convert to OpenAI format
      let anthropicResp = await dispatchProviderCall(provider, emuBody, req.clientKey, lmrhHint, null);
      if (_ePbtc) anthropicResp = pbtcPostprocess(anthropicResp, req.body.tools);
      if (_ePbrc) anthropicResp = pbrcPostprocess(anthropicResp);
      const oaiResp = convertAnthropicResponseToOpenAI(anthropicResp, anthropicBody.model);
      config.stats[provider.id].successes++;
      config.stats[provider.id].totalLatency += Date.now() - startTime;
      config.stats[provider.id].totalInputTokens  += anthropicResp.usage?.input_tokens  || 0;
      config.stats[provider.id].totalOutputTokens += anthropicResp.usage?.output_tokens || 0;
      if (USE_SQLITE && sqliteDb) saveStatsRecord(provider.id);
      providerMonitor.recordSuccess(provider);
      if (req.clientKey) {
        req.clientKey.requests = (req.clientKey.requests || 0) + 1;
        req.clientKey.lastUsed = new Date().toISOString();
        saveApiKeyRecord(req.clientKey);
      }
      return res.json(oaiResp);

    } catch (err) {
      logger.warn(`Provider ${provider.name} failed for /v1/chat/completions: ${err.message}`);
      providerMonitor.recordFailure(provider, err);
      config.stats[provider.id] = config.stats[provider.id] || {};
      config.stats[provider.id].failures = (config.stats[provider.id].failures || 0) + 1;
      if (isStreaming && res.headersSent) { res.end(); return; }
      // Try next provider
    }
  }

  if (isStreaming && res.headersSent) { res.end(); return; }
  return res.status(503).json({ error: 'All providers failed' });
});

// ── OpenAI-format image generation endpoint (/v1/images/generations) ───────────
// Routes image generation requests to a provider that supports it (DALL-E 3 or equivalent).
// Returns OpenAI /v1/images/generations format.
app.post('/v1/images/generations', validateApiKey, async (req, res) => {
  const { prompt, n = 1, size = '1024x1024', quality = 'standard', model: reqModel } = req.body;

  if (!prompt) return res.status(400).json({ error: 'prompt is required' });

  // Find providers that support image generation — prefer OpenAI (DALL-E 3) first
  const imgProviders = config.providers
    .filter(p => p.enabled && p.apiKey && !providerMonitor.isInHoldDown(p))
    .filter(p => p.type === 'openai' || p.supportsImageGeneration)
    .sort((a, b) => {
      // Prioritize providers explicitly flagged or OpenAI type
      const aScore = (p => p.type === 'openai' ? 10 : 0)(a);
      const bScore = (p => p.type === 'openai' ? 10 : 0)(b);
      return (bScore - aScore) || (a.priority - b.priority);
    });

  if (imgProviders.length === 0)
    return res.status(503).json({ error: 'No image generation provider available. Configure an OpenAI provider with a DALL-E capable API key.' });

  for (const provider of imgProviders) {
    try {
      const baseUrl = provider.baseUrl || 'https://api.openai.com';
      const imgModel = reqModel || 'dall-e-3';
      const response = await axios.post(
        `${baseUrl}/v1/images/generations`,
        { prompt, n, size, quality, model: imgModel },
        {
          headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${provider.apiKey}` },
          timeout: 120000
        }
      );
      initStats(provider.id);
      config.stats[provider.id].requests = (config.stats[provider.id].requests || 0) + 1;
      config.stats[provider.id].successes = (config.stats[provider.id].successes || 0) + 1;
      if (USE_SQLITE && sqliteDb) saveStatsRecord(provider.id);
      providerMonitor.recordSuccess(provider);
      if (req.clientKey) {
        req.clientKey.requests = (req.clientKey.requests || 0) + 1;
        req.clientKey.lastUsed = new Date().toISOString();
        saveApiKeyRecord(req.clientKey);
      }
      logger.info(`Image generation via ${provider.name}: "${prompt.substring(0, 60)}..."`);
      return res.json(response.data);
    } catch (err) {
      logger.warn(`Image generation failed on ${provider.name}: ${err.message}`);
      providerMonitor.recordFailure(provider, err);
    }
  }

  return res.status(503).json({ error: 'All image generation providers failed' });
});

// Main proxy endpoint
app.post('/v1/messages', validateApiKey, async (req, res) => {
  const startTime = Date.now();
  const isStreaming = req.body.stream === true;

  logger.info('Received request', {
    requestId: req.requestId,
    model: req.body.model,
    messageCount: req.body.messages?.length,
    streaming: isStreaming
  });

  // Build available provider list: enabled, have API key, not in hold-down
  const sortedProviders = config.providers
    .filter(p => p.enabled && p.apiKey)
    .sort((a, b) => a.priority - b.priority);

  const heldDownFiltered = sortedProviders.filter(p => {
    if (providerMonitor.isInHoldDown(p)) {
      logger.warn(`Provider ${p.name} skipped — in hold-down`);
      return false;
    }
    return true;
  });

  // 2: Capability-aware routing — prefer providers that support the request's features
  const availableProviders = capabilityFilter(heldDownFiltered, req.body);
  if (availableProviders.length < heldDownFiltered.length) {
    const filtered = heldDownFiltered.filter(p => !availableProviders.includes(p)).map(p => p.name);
    logger.info(`Capability router excluded providers: ${filtered.join(', ')}`);
  }

  if (availableProviders.length === 0) {
    logger.error('No available providers (all disabled, missing API key, or in hold-down)');
    return res.status(503).json({ error: 'No providers available' });
  }

  // ── LMRH: parse LLM-Hint and re-rank providers if hint is present ─────────
  // The header is proxy-internal — backends never see it (they build their own headers).
  const lmrhHint = parseLmrhHint(req.headers['llm-hint'] || req.headers['LLM-Hint']);
  let routingProviders = availableProviders;
  let lmrhUnmet = [];
  let lmrhTopCaps = null;

  if (lmrhHint) {
    const { rankedProviders, unmetAffinities, topCaps } =
      rankProvidersWithHint(availableProviders, lmrhHint, USE_SQLITE ? sqliteDb : null, req.body.model);

    if (rankedProviders.length === 0 && lmrhHint.hard.size > 0) {
      logger.warn('LMRH: no provider satisfies hard constraints', { hint: lmrhHint.raw });
      return res.status(503).json({
        error: 'no_provider_satisfies_constraints',
        failed: [...lmrhHint.hard],
        hint: lmrhHint.raw
      });
    }

    if (rankedProviders.length > 0) {
      routingProviders = rankedProviders;
      lmrhUnmet = unmetAffinities;
      lmrhTopCaps = topCaps;
      logger.info(`LMRH routing: task=${lmrhHint.task} → ${rankedProviders[0].name}` +
        (rankedProviders[0]._lmrhModel ? `/${rankedProviders[0]._lmrhModel}` : '') +
        (unmetAffinities.length ? ` (unmet: ${unmetAffinities.join(',')})` : ''));
    } else {
      // Soft-only hints with no match — keep original order, log it
      logger.info(`LMRH hint present but no scored match, using default routing: ${lmrhHint.raw}`);
    }
  }

  // ── Layer 3: Conductor path — parallel racing for non-streaming requests ──
  if (CONDUCTOR_MODE && !isStreaming && routingProviders.length >= 2) {
    const maxLatencyMs = 1800; // use global default for conductor
    logChatRequest('Conductor', 1, req.body.model, req.body.messages, req);
    try {
      const { result, provider } = await raceProvidersParallel(routingProviders, req.body, maxLatencyMs);
      const latency = Date.now() - startTime;

      // Update stats for winning provider
      initStats(provider.id);
      config.stats[provider.id].requests++;
      config.stats[provider.id].successes++;
      config.stats[provider.id].totalLatency += latency;
      config.stats[provider.id].lastUsed = new Date().toISOString();
      config.stats[provider.id].lastSuccess = { timestamp: new Date().toISOString() };
      providerMonitor.recordSuccess(provider);

      const model = result.model || req.body.model || provider.model || 'unknown';
      const usage = result.usage || {};
      const cost = pricingManager.calculateCost(model, usage.input_tokens || 0, usage.output_tokens || 0);
      config.stats[provider.id].totalCost += cost;
      config.stats[provider.id].totalInputTokens  += (usage.input_tokens  || 0);
      config.stats[provider.id].totalOutputTokens += (usage.output_tokens || 0);
      if (USE_SQLITE && sqliteDb) saveStatsRecord(provider.id);
      recordAnalyticsTick(provider.id, { success: true, cost, inputTokens: usage.input_tokens || 0, outputTokens: usage.output_tokens || 0, latencyMs: latency });

      if (req.clientKey) {
        req.clientKey.requests = (req.clientKey.requests || 0) + 1;
        req.clientKey.lastUsed = new Date().toISOString();
        saveApiKeyRecord(req.clientKey);
      }

      logger.info(`Conductor success via ${provider.name}`, { latency: `${latency}ms`, requestId: req.requestId });
      logChatResponse(provider.name, model, result, latency, cost);
      return res.json(result);
    } catch (err) {
      logger.error(`Conductor race failed: ${err.message} — falling through to sequential`, { requestId: req.requestId });
      // Fall through to sequential 3-pass routing below
    }
  }

  let headersSent = false;
  let lastError = null;

  // 3-pass retry: each pass tries every available (non-held) provider in priority order
  passes: for (let pass = 1; pass <= 3; pass++) {
    if (pass > 1) logger.info(`Provider routing pass ${pass}/3`);

    for (const provider of routingProviders) {
      // Skip provider if requested model is not in its allow-list
      if (!isModelAllowedForProvider(provider, req.body.model)) {
        logger.info(`Skipping ${provider.name} — model ${req.body.model} not in enabledModels list`);
        continue;
      }

      initStats(provider.id);
      const providerLog = getProviderLogger(provider.name);

      // Per-provider latency limit (0 = no limit, default 1800ms)
      const maxLatencyMs = provider.maxLatencyMs != null
        ? parseInt(provider.maxLatencyMs)
        : 1800;

      try {
        let result;
        logger.info(`Trying provider: ${provider.name} (pass ${pass}, streaming: ${isStreaming})`, { requestId: req.requestId });
        providerLog.info('Request attempt', {
          pass,
          model: req.body.model,
          messageCount: req.body.messages?.length,
          streaming: isStreaming,
          max_tokens: req.body.max_tokens,
          temperature: req.body.temperature
        });
        // 4a: Context window auto-truncation — trim oldest messages if request is too long
        const providerCaps = PROVIDER_CAPS[provider.type];
        const contextWindow = providerCaps?.contextWindow || 8192;
        const _truncated = { ...req.body, messages: truncateMessagesToFit(req.body.messages, Math.floor(contextWindow * 0.85)) };

        // 4a-emulation: Apply PBTC (tool emulation), PBRC (reasoning emulation),
        // vision stripping, and any other cross-provider feature bridges.
        const { requestBody, usePbtc: _usePbtc, usePbrc: _usePbrc } = applyProviderEmulation(provider, _truncated);

        logChatRequest(provider.name, pass, requestBody.model || provider.model, requestBody.messages, req);

        config.stats[provider.id].requests++;

        // Emulated streaming: PBTC and/or PBRC require a non-streaming call so we can
        // post-process the full response before re-emitting as synthetic SSE.
        if ((_usePbtc || _usePbrc) && isStreaming) {
          const _emuRaw = await dispatchProviderCall(provider, { ...requestBody, stream: false }, req.clientKey, lmrhHint, null);
          let _emuResult = _emuRaw;
          if (_usePbtc) _emuResult = pbtcPostprocess(_emuResult, req.body.tools);
          if (_usePbrc) _emuResult = pbrcPostprocess(_emuResult);
          const streamRes = headersSent ? res : makeBufferedRes(res);
          if (!headersSent) {
            streamRes.setHeader('Content-Type', 'text/event-stream');
            streamRes.setHeader('Cache-Control', 'no-cache');
            streamRes.setHeader('Connection', 'keep-alive');
          }
          // Emit via PBRC stream (handles both thinking + text blocks) — PBTC-only uses pbtcEmitStream
          if (_usePbrc) {
            await pbrcEmitStream(streamRes, _emuResult, req.body.model || provider.model);
          } else {
            await pbtcEmitStream(streamRes, _emuResult, req.body.model || provider.model);
          }
          headersSent = true;
          config.stats[provider.id].successes++;
          config.stats[provider.id].totalLatency += (Date.now() - startTime);
          providerMonitor.recordSuccess(provider);
          logger.info(`Emulated stream emitted via ${provider.name} (pbtc=${_usePbtc} pbrc=${_usePbrc})`);
          res.end();
          return;
        }

        if (isStreaming) {
          // LMRH Phase 4: look up model caps for CoT auto-engage via task=reasoning
          let _lmrhModelCaps = null;
          if (lmrhHint?.task === 'reasoning' && USE_SQLITE && sqliteDb) {
            const _mid = provider._lmrhModel || requestBody.model || provider.model;
            try { _lmrhModelCaps = sqliteDb.getModelCapabilities(provider.id, _mid); } catch (_) {}
          }
          const augMode = getAugmentationMode(provider, req.clientKey, lmrhHint, _lmrhModelCaps);

          // 1d: Use a buffered response wrapper so SSE headers are held until
          // the first chunk arrives — allowing latency-guard failover to still work
          // even if the provider accepts the connection but never sends data.
          const streamRes = headersSent ? res : makeBufferedRes(res);
          if (!headersSent) {
            streamRes.setHeader('Content-Type', 'text/event-stream');
            streamRes.setHeader('Cache-Control', 'no-cache');
            streamRes.setHeader('Connection', 'keep-alive');
            if (lmrhHint) {
              const cap = buildLmrhCapabilityHeader(provider, provider._lmrhModel, lmrhTopCaps, lmrhUnmet,
                augMode === 'cot-pipeline' && lmrhHint.task === 'reasoning');
              if (cap) streamRes.setHeader('LLM-Capability', cap);
            }
            logger.info(`Streaming headers buffered for provider: ${provider.name}`);
          }
          const streamCall = (() => {
            if (augMode === 'cot-pipeline')
              return streamCotPipeline(provider, requestBody, streamRes, req);
            if (augMode === 'native-gemini-thinking') {
              const budget = (provider.model || '').toLowerCase().includes('pro') ? 8000 : 5000;
              return streamGemini(provider, { ...requestBody, _geminiThinkingBudget: budget }, streamRes);
            }
            if (augMode === 'native-o-series')
              return streamOpenAI(provider, { ...requestBody, _reasoningEffort: 'high' }, streamRes);
            if (provider.type === 'anthropic')         return streamAnthropic(provider, requestBody, streamRes);
            if (provider.type === 'google')            return streamGemini(provider, requestBody, streamRes);
            if (provider.type === 'openai')            return streamOpenAI(provider, requestBody, streamRes);
            if (provider.type === 'grok')              return streamGrok(provider, requestBody, streamRes);
            if (provider.type === 'ollama')            return streamOllama(provider, requestBody, streamRes);
            if (provider.type === 'openai-compatible') return streamOpenAICompatible(provider, requestBody, streamRes);
            throw new Error(`Unsupported provider type: ${provider.type}`);
          })();

          if (maxLatencyMs > 0) {
            await Promise.race([
              streamCall,
              new Promise((_, reject) =>
                setTimeout(() => reject(new Error(`Provider latency exceeded ${maxLatencyMs}ms`)), maxLatencyMs)
              )
            ]);
          } else {
            await streamCall;
          }

          // Mark headers as sent only once first chunk has actually flushed
          if (!headersSent && streamRes.headersFlushed) {
            headersSent = true;
            logger.info(`Streaming headers flushed for provider: ${provider.name}`);
          }

        } else {
          // LMRH Phase 4: look up model caps for CoT auto-engage via task=reasoning
          let _lmrhModelCaps = null;
          if (lmrhHint?.task === 'reasoning' && USE_SQLITE && sqliteDb) {
            const _mid = provider._lmrhModel || requestBody.model || provider.model;
            try { _lmrhModelCaps = sqliteDb.getModelCapabilities(provider.id, _mid); } catch (_) {}
          }
          const nonStreamCall = dispatchProviderCall(provider, requestBody, req.clientKey, lmrhHint, _lmrhModelCaps);

          if (maxLatencyMs > 0) {
            result = await Promise.race([
              nonStreamCall,
              new Promise((_, reject) =>
                setTimeout(() => reject(new Error(`Provider latency exceeded ${maxLatencyMs}ms`)), maxLatencyMs)
              )
            ]);
          } else {
            result = await nonStreamCall;
          }

          // 1e: XML Sentinel — scan response text for bad-model output patterns
          // When PBTC/PBRC is active, post-process first; only sentinel-fail if still bad.
          const responseText = result?.content?.map(b => b.text || '').join('') || '';
          if (_usePbtc) {
            result = pbtcPostprocess(result, req.body.tools);
          }
          if (_usePbrc) {
            result = pbrcPostprocess(result);
          }
          if (!_usePbtc && !_usePbrc && isBadModelOutput(responseText)) {
            logger.warn(`XML sentinel triggered for ${provider.name} — bad model output detected, failing over`);
            logChatFailover(provider.name, 'Bad model output (XML sentinel)', pass);
            config.stats[provider.id].failures++;
            config.stats[provider.id].lastError = { message: 'Bad model output (XML sentinel)', timestamp: new Date().toISOString() };
            saveStatsRecord(provider.id);
            lastError = new Error('Bad model output detected');
            logger.info(`Failing over to next provider (pass ${pass})`);
            continue;
          }

          if (lmrhHint) {
            const _cotEngaged = lmrhHint.task === 'reasoning' && _lmrhModelCaps?.native_reasoning === false;
            const cap = buildLmrhCapabilityHeader(provider, provider._lmrhModel, lmrhTopCaps, lmrhUnmet, _cotEngaged);
            if (cap) res.setHeader('LLM-Capability', cap);
          }
          res.json(result);
        }

        // ── Success ───────────────────────────────────────────────────────
        const latency = Date.now() - startTime;
        config.stats[provider.id].successes++;
        config.stats[provider.id].totalLatency += latency;
        config.stats[provider.id].lastUsed = new Date().toISOString();
        config.stats[provider.id].lastSuccess = { timestamp: new Date().toISOString() };

        providerMonitor.recordSuccess(provider);
        logger.info(`Success with ${provider.name}`, { latency: `${latency}ms`, pass });

        addActivityLog('success', `Request completed via ${provider.name}`, {
          model: req.body.model || provider.model,
          latency: `${latency}ms`,
          pass,
          keyName: req.clientKey?.name || 'unknown',
          streaming: isStreaming,
        });

        if (req.clientKey) {
          req.clientKey.requests = (req.clientKey.requests || 0) + 1;
          req.clientKey.lastUsed = new Date().toISOString();
          saveApiKeyRecord(req.clientKey);
        }

        if (isStreaming) {
          recordAnalyticsTick(provider.id, { success: true, cost: 0, inputTokens: 0, outputTokens: 0, latencyMs: latency });
          saveStatsRecord(provider.id);
          // For piped providers (anthropic, grok, ollama, compatible) text isn't captured —
          // streamGemini/streamOpenAI log themselves; log a note for the rest
          const selfLogging = provider.type === 'google' || provider.type === 'openai';
          if (!selfLogging) {
            const latencyMs = Date.now() - startTime;
            logChatStreamResponse(provider.name, req.body.model || provider.model || '', null, latencyMs, 0);
          }
          res.end();
          return;
        }

        const model = result.model || req.body.model || provider.model || 'claude-sonnet-4-5-20250929';
        const usage = result.usage || {};
        const cost = pricingManager.calculateCost(model, usage.input_tokens || 0, usage.output_tokens || 0);

        logger.info(`Cost tracking: model=${model}, input=${usage.input_tokens}, output=${usage.output_tokens}, cost=$${cost.toFixed(6)}`);

        config.stats[provider.id].totalCost += cost;
        config.stats[provider.id].totalInputTokens  += (usage.input_tokens  || 0);
        config.stats[provider.id].totalOutputTokens += (usage.output_tokens || 0);
        recordAnalyticsTick(provider.id, { success: true, cost, inputTokens: usage.input_tokens || 0, outputTokens: usage.output_tokens || 0, latencyMs: latency });

        if (USE_SQLITE && sqliteDb) {
          saveStatsRecord(provider.id);
        } else if (config.stats[provider.id].requests % 10 === 0) {
          saveConfig();
        }

        providerLog.info('Request success', { latency: `${latency}ms`, model, usage, cost });
        logChatResponse(provider.name, model, result, latency, cost);
        return;

      } catch (error) {
        const latency = Date.now() - startTime;

        // "Cannot set headers after they are sent" means streaming already succeeded partially
        // or headers were sent and then something internal failed — don't count as provider failure
        const isHeadersAlreadySent = error.message && error.message.includes('Cannot set headers after they are sent');
        if (isHeadersAlreadySent) {
          logger.warn(`Ignoring post-stream headers error for ${provider.name} — response already delivered`);
          return;
        }

        config.stats[provider.id].failures++;
        config.stats[provider.id].lastError = { message: error.message, timestamp: new Date().toISOString() };
        saveStatsRecord(provider.id);
        recordAnalyticsTick(provider.id, { success: false, cost: 0, inputTokens: 0, outputTokens: 0, latencyMs: latency });
        lastError = error;

        // 4b-4d: Structured error classification — controls hold-down and retry behaviour
        const errCategory = classifyProviderError(error);
        const noHoldDown = ['auth_error', 'not_found', 'client_error', 'context_exceeded'].includes(errCategory);

        if (errCategory === 'context_exceeded') {
          logger.warn(`Context length exceeded for ${provider.name} — skipping hold-down, trying next provider`);
        } else if (errCategory === 'auth_error') {
          logger.error(`Auth error for ${provider.name} (${error.response?.status}) — check API key; skipping hold-down`);
        } else if (errCategory === 'not_found') {
          logger.error(`Not found (404) for ${provider.name} — check model/endpoint; skipping hold-down`);
        } else if (noHoldDown) {
          logger.warn(`Client-side error for ${provider.name} (${error.response?.status}) — not counting toward hold-down`);
        }

        if (!noHoldDown) {
          providerMonitor.recordFailure(provider, latency, error);
        }

        logger.error(`Failed with ${provider.name} (pass ${pass}) [${errCategory}]:`, {
          error: error.message,
          status: error.response?.status,
          latency: `${latency}ms`
        });

        providerLog.error('Request failed', {
          error: error.message,
          category: errCategory,
          status: error.response?.status,
          statusText: error.response?.statusText,
          data: error.response?.data,
          latency: `${latency}ms`,
          stack: error.stack
        });
        logChatFailover(provider.name, `[${errCategory}] ${error.message}${error.response?.status ? ` (HTTP ${error.response.status})` : ''}`, pass);

        // If streaming headers already sent we can't try another provider
        if (isStreaming && headersSent) {
          logger.warn(`Streaming already started for ${provider.name} — cannot failover mid-stream`);
          try {
            res.write(`event: error\n`);
            res.write(`data: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: error.message } })}\n\n`);
            res.end();
          } catch (_) {}
          return;
        }

        logger.info(`Failing over to next provider (pass ${pass})`);
      }
    }
    // End of pass — all available providers failed this pass
  }

  // All 3 passes exhausted
  logger.error('All providers failed across 3 passes');
  addActivityLog('error', 'All providers failed — request returned 503', {
    model: req.body?.model,
    keyName: req.clientKey?.name || 'unknown',
  });
  if (isStreaming && headersSent) {
    res.write(`event: error\n`);
    res.write(`data: ${JSON.stringify({ type: 'error', error: { type: 'api_error', message: 'All providers failed' } })}\n\n`);
    res.end();
  } else if (!res.headersSent) {
    res.status(503).json({
      type: 'error',
      error: {
        type: 'overloaded_error',
        message: 'All providers failed or are temporarily unavailable'
      }
    });
  }
});

// Provider capabilities endpoint
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

// Hold-down status endpoint
app.get('/api/holddown-status', requireAuth, (req, res) => {
  const statuses = {};
  for (const provider of config.providers) {
    const state = providerMonitor.getState(provider.id);
    statuses[provider.id] = {
      providerName: provider.name,
      inHoldDown: providerMonitor.isInHoldDown(provider),
      consecutiveFailures: state.consecutiveFailures,
      holdDownUntil: state.holdDownUntil ? new Date(state.holdDownUntil).toISOString() : null,
      retestScheduled: state.retestTimer != null
    };
  }
  res.json(statuses);
});

// Health check
app.get('/health', (req, res) => {
  const enabledCount = config.providers.filter(p => p.enabled).length;
  res.json({
    status: 'ok',
    version: require('../package.json').version,
    providers: {
      total: config.providers.length,
      enabled: enabledCount
    },
    uptime: process.uptime()
  });
});

// SMTP Settings API
app.get('/api/smtp/settings', requireAuth, (req, res) => {
  const smtpSettings = config.smtp || {};
  // Mask password for security
  const safeSettings = {
    ...smtpSettings,
    pass: smtpSettings.pass ? '••••••••' : ''
  };
  res.json(safeSettings);
});

app.post('/api/smtp/settings', requireAuth, (req, res) => {
  try {
    const {enabled, host, port, secure, user, pass, from, to, subjectPrefix, minSeverity, throttle} = req.body;

    // Initialize smtp settings if not exists
    if (!config.smtp) {
      config.smtp = {};
    }

    // Update settings
    config.smtp.enabled = enabled;
    config.smtp.host = host;
    config.smtp.port = port;
    config.smtp.secure = secure;
    config.smtp.user = user;
    // Only update password if provided (not masked)
    if (pass && pass !== '••••••••') {
      config.smtp.pass = pass;
    }
    config.smtp.from = from;
    config.smtp.to = to;
    config.smtp.subjectPrefix = subjectPrefix || '[LLM Proxy Alert]';
    config.smtp.minSeverity = minSeverity || 'WARNING';
    config.smtp.throttle = throttle || 15;
    if (req.body.sessionTimeoutMinutes != null) {
      config.smtp.sessionTimeoutMinutes = parseInt(req.body.sessionTimeoutMinutes) || 480;
    }

    // Save to config
    saveSmtpRecord();

    // Reinitialize notification manager with new settings
    process.env.SMTP_ENABLED = enabled ? 'true' : 'false';
    process.env.SMTP_HOST = host;
    process.env.SMTP_PORT = port.toString();
    process.env.SMTP_SECURE = secure ? 'true' : 'false';
    process.env.SMTP_USER = user;
    if (pass && pass !== '••••••••') {
      process.env.SMTP_PASS = pass;
    }
    process.env.SMTP_FROM = from;
    process.env.SMTP_TO = to;
    process.env.SMTP_SUBJECT_PREFIX = subjectPrefix;
    process.env.SMTP_MIN_SEVERITY = minSeverity;
    process.env.ALERT_THROTTLE_MINUTES = throttle.toString();

    notificationManager.enabled = enabled;
    notificationManager.smtpConfig = {
      host: host,
      port: port,
      secure: secure,
      auth: {
        user: user,
        pass: config.smtp.pass
      }
    };
    notificationManager.emailConfig = {
      from: from,
      to: to,
      alertSubjectPrefix: subjectPrefix
    };
    notificationManager.minSeverity = notificationManager.getSeverityLevel(minSeverity);
    notificationManager.throttleWindow = throttle * 60 * 1000;

    if (enabled) {
      notificationManager.initialize();
    }

    logger.info('SMTP settings updated', {enabled, host, to});

    res.json({success: true});
  } catch (error) {
    logger.error('Error updating SMTP settings:', error);
    res.status(500).json({error: error.message});
  }
});

app.post('/api/smtp/test', requireAuth, async (req, res) => {
  try {
    await notificationManager.sendTestEmail();
    res.json({success: true, message: 'Test email sent successfully'});
  } catch (error) {
    logger.error('Error sending test email:', error);
    res.status(500).json({error: error.message});
  }
});

// Get config — API keys always masked
app.get('/api/config', (req, res) => {
  const safeConfig = {
    ...config,
    providers: config.providers.map(p => ({
      ...p,
      apiKey: p.apiKey ? `${p.apiKey.slice(0, 10)}...${p.apiKey.slice(-4)}` : 'NOT SET'
    }))
  };
  res.json(safeConfig);
});

// Reveal real API key for a single provider — requires login
app.get('/api/provider-apikey/:id', requireAuth, (req, res) => {
  const provider = config.providers.find(p => p.id === req.params.id);
  if (!provider) return res.status(404).json({ error: 'Provider not found' });
  res.json({ apiKey: provider.apiKey || '' });
});

// Update config
app.post('/api/config', (req, res) => {
  try {
    if (req.body.providers) {
      // Track deleted provider IDs as tombstones so cluster sync doesn't restore them
      const incomingIds = new Set(req.body.providers.map(p => p.id));
      const deletedIds = config.providers.map(p => p.id).filter(id => !incomingIds.has(id));
      if (deletedIds.length > 0) {
        if (!config.deletedProviderIds) config.deletedProviderIds = [];
        for (const id of deletedIds) {
          if (!config.deletedProviderIds.includes(id)) {
            config.deletedProviderIds.push(id);
            logger.info('Provider deleted (tombstone recorded)', { id });
          }
        }
      }

      // Replace the entire providers array to support add/edit/delete
      config.providers = req.body.providers.map((p, idx) => {
        // Check if API key is masked (from the GET endpoint)
        const isMasked = p.apiKey && p.apiKey.includes('...');

        // If key is masked and we have an existing provider with same ID, keep the original key
        let apiKey = p.apiKey;
        if (isMasked) {
          const existingProvider = config.providers.find(existing => existing.id === p.id);
          if (existingProvider) {
            apiKey = existingProvider.apiKey;
            logger.info('Preserving existing API key for provider', { id: p.id, name: p.name });
          }
        }

        return {
          id: p.id,
          name: p.name,
          type: p.type,
          apiKey: apiKey,
          enabled: p.enabled !== undefined ? p.enabled : true,
          priority: p.priority || 999,
          // Optional fields for different provider types
          projectId: p.projectId,
          location: p.location,
          baseUrl: p.baseUrl,
          model: p.model,
          holdDownSeconds:       p.holdDownSeconds       != null ? parseInt(p.holdDownSeconds)  : 180,
          maxLatencyMs:          p.maxLatencyMs          != null ? parseInt(p.maxLatencyMs)     : 1800,
          failureThreshold:      p.failureThreshold      != null ? parseInt(p.failureThreshold) : 2,
          excludeFromToolRequests: p.excludeFromToolRequests === true
        };
      });
    }
    // Save providers (full replace via saveConfig which calls saveAll in SQLite mode)
    saveConfig();
    logger.info('Configuration updated', { providerCount: config.providers.length });

    // Log activity (addActivityLog handles its own DB write in SQLite mode)
    addActivityLog('info', 'Configuration saved', {
      providerCount: config.providers.length
    });

    res.json({ success: true });
  } catch (error) {
    logger.error('Error updating config:', error);
    res.status(500).json({ error: error.message });
  }
});

// Get stats
app.get('/api/stats', (req, res) => {
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
});

// Reset stats
app.post('/api/stats/reset', (req, res) => {
  config.stats = {};
  if (USE_SQLITE && sqliteDb) {
    sqliteDb.clearStats();
  } else {
    saveConfig();
  }
  res.json({ success: true });
});

// Analytics — time-series data for dashboard
app.get('/api/analytics', requireAuth, (req, res) => {
  const window = req.query.window || '24h'; // 1h, 24h, 7d, all
  const windowHours = window === '1h' ? 1 : window === '24h' ? 24 : window === '7d' ? 168 : ANALYTICS_BUCKETS;

  // Collect cutoff bucket label
  const cutoffMs = Date.now() - windowHours * 3600 * 1000;
  const cutoffHour = (() => {
    const d = new Date(cutoffMs);
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth()+1).padStart(2,'0')}-${String(d.getUTCDate()).padStart(2,'0')}T${String(d.getUTCHours()).padStart(2,'0')}`;
  })();

  const byProvider = {};
  for (const [providerId, buckets] of Object.entries(analyticsSeries)) {
    const provider = config.providers.find(p => p.id === providerId);
    const filtered = buckets.filter(b => b.hour >= cutoffHour);
    if (filtered.length === 0) continue;
    byProvider[providerId] = {
      name: provider?.name || providerId,
      buckets: filtered,
      totals: filtered.reduce((acc, b) => ({
        requests: acc.requests + b.requests,
        successes: acc.successes + b.successes,
        failures: acc.failures + b.failures,
        cost: acc.cost + b.cost,
        inputTokens: acc.inputTokens + b.inputTokens,
        outputTokens: acc.outputTokens + b.outputTokens,
        totalLatency: acc.totalLatency + b.totalLatency,
      }), { requests: 0, successes: 0, failures: 0, cost: 0, inputTokens: 0, outputTokens: 0, totalLatency: 0 })
    };
    const t = byProvider[providerId].totals;
    t.avgLatency = t.requests > 0 ? Math.round(t.totalLatency / t.requests) : 0;
    t.successRate = t.requests > 0 ? Math.round((t.successes / t.requests) * 100) : 0;
  }

  // For window=all, use all-time config.stats totals for all providers (authoritative)
  if (window === 'all') {
    for (const [providerId, stats] of Object.entries(config.stats)) {
      const provider = config.providers.find(p => p.id === providerId);
      byProvider[providerId] = {
        name: provider?.name || providerId,
        buckets: analyticsSeries[providerId] || [],
        totals: {
          requests: stats.requests || 0,
          successes: stats.successes || 0,
          failures: stats.failures || 0,
          cost: stats.totalCost || 0,
          inputTokens: stats.totalInputTokens || 0,
          outputTokens: stats.totalOutputTokens || 0,
          totalLatency: stats.totalLatency || 0,
          avgLatency: stats.requests > 0 ? Math.round((stats.totalLatency || 0) / stats.requests) : 0,
          successRate: stats.requests > 0 ? Math.round((stats.successes / stats.requests) * 100) : 0,
        }
      };
    }
  }

  const overall = Object.values(byProvider).reduce((acc, p) => ({
    requests: acc.requests + p.totals.requests,
    cost: acc.cost + p.totals.cost,
    inputTokens: acc.inputTokens + p.totals.inputTokens,
    outputTokens: acc.outputTokens + p.totals.outputTokens,
    failures: acc.failures + p.totals.failures,
  }), { requests: 0, cost: 0, inputTokens: 0, outputTokens: 0, failures: 0 });

  res.json({ window, byProvider, overall });
});

// Chat log SSE live-tail stream
app.get('/api/chat-log-stream', requireAuth, (req, res) => {
  const { name } = req.query;
  if (!name) return res.status(400).json({ error: 'name required' });
  const safeName = name.replace(/[^a-zA-Z0-9_-]/g, '_');

  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no'); // disable nginx buffering
  res.flushHeaders();

  // Send a heartbeat comment every 15s to keep the connection alive
  const heartbeat = setInterval(() => { try { res.write(': heartbeat\n\n'); } catch (_) {} }, 15000);

  if (!chatLogSubscribers[safeName]) chatLogSubscribers[safeName] = new Set();
  chatLogSubscribers[safeName].add(res);

  req.on('close', () => {
    clearInterval(heartbeat);
    if (chatLogSubscribers[safeName]) chatLogSubscribers[safeName].delete(res);
  });
});

// Activity Log
app.get('/api/activity-log', (req, res) => {
  if (!config.activityLog) {
    config.activityLog = [];
  }
  res.json(config.activityLog);
});

// Client API Key Management
app.get('/api/client-keys', (req, res) => {
  if (!config.clientApiKeys) {
    config.clientApiKeys = [];
  }
  res.json(config.clientApiKeys);
});

app.post('/api/client-keys', (req, res) => {
  const { name, quotaEnabled, quotaRpm, quotaRpd, keyType } = req.body;

  if (!name || name.trim() === '') {
    return res.status(400).json({ error: 'Name is required' });
  }

  const newKey = {
    id: `key-${Date.now()}-${crypto.randomBytes(4).toString('hex')}`,
    key: `llm-proxy-${crypto.randomBytes(32).toString('hex')}`,
    name: name.trim(),
    created: new Date().toISOString(),
    lastUsed: null,
    requests: 0,
    enabled: true,
    keyType: ['claude-code', 'standard', 'openai', 'anthropic', 'microsoft', 'google', 'grok'].includes(keyType) ? keyType : 'claude-code',
    quotaEnabled: Boolean(quotaEnabled),
    quotaRpm: parseInt(quotaRpm) || 0,
    quotaRpd: parseInt(quotaRpd) || 0,
  };

  if (!config.clientApiKeys) {
    config.clientApiKeys = [];
  }

  config.clientApiKeys.push(newKey);
  saveApiKeyRecord(newKey);

  logger.info('Client API key created', { name: newKey.name, id: newKey.id });
  res.json(newKey);
});

app.delete('/api/client-keys/:id', (req, res) => {
  const { id } = req.params;

  if (!config.clientApiKeys) {
    return res.status(404).json({ error: 'Key not found' });
  }

  const index = config.clientApiKeys.findIndex(k => k.id === id);

  if (index === -1) {
    return res.status(404).json({ error: 'Key not found' });
  }

  const deleted = config.clientApiKeys.splice(index, 1)[0];
  deleteApiKeyRecord(deleted.id);

  logger.info('Client API key deleted', { name: deleted.name, id: deleted.id });
  res.json({ success: true, deleted });
});

app.patch('/api/client-keys/:id', (req, res) => {
  const { id } = req.params;
  const { enabled, name, quotaEnabled, quotaRpm, quotaRpd, keyType } = req.body;

  if (!config.clientApiKeys) {
    return res.status(404).json({ error: 'Key not found' });
  }

  const key = config.clientApiKeys.find(k => k.id === id);

  if (!key) {
    return res.status(404).json({ error: 'Key not found' });
  }

  if (enabled !== undefined) key.enabled = Boolean(enabled);
  if (name && name.trim() !== '') key.name = name.trim();
  if (keyType && ['claude-code', 'standard', 'openai', 'anthropic', 'microsoft', 'google', 'grok'].includes(keyType)) key.keyType = keyType;
  if (quotaEnabled !== undefined) key.quotaEnabled = Boolean(quotaEnabled);
  if (quotaRpm !== undefined) key.quotaRpm = parseInt(quotaRpm) || 0;
  if (quotaRpd !== undefined) key.quotaRpd = parseInt(quotaRpd) || 0;

  saveApiKeyRecord(key);

  logger.info('Client API key updated', { name: key.name, id: key.id, enabled: key.enabled });
  res.json(key);
});

// Test provider endpoint
// GET /api/provider-chat-log?name=<providerName>&lines=<n>
// Quota status for a key (live in-memory counters)
app.get('/api/client-keys/:id/quota-status', requireAuth, (req, res) => {
  const key = config.clientApiKeys?.find(k => k.id === req.params.id);
  if (!key) return res.status(404).json({ error: 'Key not found' });
  const minKey = Math.floor(Date.now() / 60000);
  const dayKey = new Date().toISOString().slice(0, 10);
  res.json({
    quotaEnabled: key.quotaEnabled || false,
    quotaRpm: key.quotaRpm || 0,
    quotaRpd: key.quotaRpd || 0,
    rpmUsed: (key._rpm?.bucket === minKey) ? key._rpm.count : 0,
    rpdUsed: (key._rpd?.bucket === dayKey) ? key._rpd.count : 0,
  });
});

// Returns the last N lines of the chat log for a named provider
app.get('/api/provider-chat-log', requireAuth, (req, res) => {
  const { name, lines } = req.query;
  if (!name) return res.status(400).json({ error: 'name required' });

  const safeName = name.replace(/[^a-zA-Z0-9_-]/g, '_');
  const logPath = `/app/logs/chat-${safeName}.log`;

  if (!fs.existsSync(logPath)) {
    return res.json({ log: '', exists: false });
  }

  try {
    const n = Math.min(parseInt(lines) || 200, 100000);
    const content = fs.readFileSync(logPath, 'utf8');
    const allLines = content.split('\n');
    const tail = allLines.slice(-n).join('\n');
    res.json({ log: tail, exists: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Scan live models from a provider
app.post('/api/scan-provider-models', async (req, res) => {
  let { providerId, type, apiKey, projectId, location, baseUrl } = req.body;

  if (providerId) {
    const p = config.providers.find(p => p.id === providerId);
    if (!p) return res.status(404).json({ error: 'Provider not found' });
    type = p.type; apiKey = p.apiKey; projectId = p.projectId;
    location = p.location; baseUrl = p.baseUrl;
  }

  if (!type) return res.status(400).json({ error: 'type required' });

  try {
    let models = [];

    if (type === 'anthropic') {
      const resp = await axios.get('https://api.anthropic.com/v1/models', {
        headers: { 'x-api-key': apiKey, 'anthropic-version': '2023-06-01' },
        timeout: 10000
      });
      models = (resp.data.data || []).map(m => ({ id: m.id, name: m.display_name || m.id }));

    } else if (type === 'openai') {
      const resp = await axios.get('https://api.openai.com/v1/models', {
        headers: { 'Authorization': `Bearer ${apiKey}` },
        timeout: 10000
      });
      models = (resp.data.data || [])
        .filter(m => m.id.startsWith('gpt') || m.id.startsWith('o1') || m.id.startsWith('o3') || m.id.startsWith('o4'))
        .sort((a, b) => a.id.localeCompare(b.id))
        .map(m => ({ id: m.id, name: m.id }));

    } else if (type === 'grok') {
      const resp = await axios.get('https://api.x.ai/v1/models', {
        headers: { 'Authorization': `Bearer ${apiKey}` },
        timeout: 10000
      });
      models = (resp.data.data || []).map(m => ({ id: m.id, name: m.id }));

    } else if (type === 'google') {
      const resp = await axios.get(
        `https://generativelanguage.googleapis.com/v1beta/models?key=${apiKey}&pageSize=50`,
        { timeout: 10000 }
      );
      models = (resp.data.models || [])
        .filter(m => m.supportedGenerationMethods?.includes('generateContent'))
        .map(m => ({ id: m.name.replace('models/', ''), name: m.displayName || m.name.replace('models/', '') }));

    } else if (type === 'ollama') {
      const base = (baseUrl || 'http://localhost:11434').replace(/\/$/, '');
      const resp = await axios.get(`${base}/api/tags`, { timeout: 10000 });
      models = (resp.data.models || []).map(m => ({ id: m.name, name: m.name }));

    } else if (type === 'openai-compatible') {
      const base = (baseUrl || '').replace(/\/$/, '');
      if (!base) return res.status(400).json({ error: 'baseUrl required for openai-compatible' });
      const headers = apiKey ? { 'Authorization': `Bearer ${apiKey}` } : {};
      const resp = await axios.get(`${base}/v1/models`, { headers, timeout: 10000 });
      models = (resp.data.data || resp.data.models || []).map(m => ({ id: m.id || m.name, name: m.id || m.name }));

    } else if (type === 'vertex') {
      // Vertex doesn't have a simple REST models list without gcloud auth — return known models
      models = [
        { id: 'gemini-2.5-pro-preview-05-06', name: 'Gemini 2.5 Pro Preview' },
        { id: 'gemini-2.5-flash-preview-04-17', name: 'Gemini 2.5 Flash Preview' },
        { id: 'gemini-2.0-flash-001', name: 'Gemini 2.0 Flash' },
        { id: 'gemini-1.5-pro-002', name: 'Gemini 1.5 Pro' },
        { id: 'gemini-1.5-flash-002', name: 'Gemini 1.5 Flash' },
      ];
    } else {
      return res.status(400).json({ error: `Model scanning not supported for type: ${type}` });
    }

    res.json({ models });
  } catch (err) {
    const status = err.response?.status;
    const msg = err.response?.data?.error?.message || err.response?.data?.message || err.message;
    res.status(500).json({ error: `Scan failed (${status || 'network'}): ${msg}` });
  }
});

// ── LMRH Capability Profile API ───────────────────────────────────────────────

// GET /api/providers/:id/model-capabilities — list all capability profiles for a provider
app.get('/api/providers/:id/model-capabilities', (req, res) => {
  if (!USE_SQLITE || !sqliteDb) return res.json([]);
  const provider = config.providers.find(p => p.id === req.params.id);
  if (!provider) return res.status(404).json({ error: 'Provider not found' });
  res.json(sqliteDb.listModelCapabilities(provider.id));
});

// POST /api/providers/:id/model-capabilities/infer — infer+save caps for a list of model IDs
// Body: { modelIds: ['claude-sonnet-4-6', ...] }
// Returns: { profiles: { modelId: caps, ... } }
app.post('/api/providers/:id/model-capabilities/infer', (req, res) => {
  if (!USE_SQLITE || !sqliteDb) return res.json({ profiles: {} });
  const provider = config.providers.find(p => p.id === req.params.id);
  if (!provider) return res.status(404).json({ error: 'Provider not found' });
  const { modelIds } = req.body;
  if (!Array.isArray(modelIds)) return res.status(400).json({ error: 'modelIds array required' });
  sqliteDb.inferAndSaveCapabilities(provider.id, modelIds);
  const profiles = {};
  for (const modelId of modelIds) {
    profiles[modelId] = sqliteDb.getModelCapabilities(provider.id, modelId);
  }
  res.json({ profiles });
});

// PUT /api/providers/:id/model-capabilities/:modelId — manually save one model's capability profile
// Body: { task:[], latency, cost, safety, context_length, region:[], modality:[], native_reasoning }
app.put('/api/providers/:id/model-capabilities/:modelId', (req, res) => {
  if (!USE_SQLITE || !sqliteDb) return res.status(503).json({ error: 'SQLite not enabled' });
  const provider = config.providers.find(p => p.id === req.params.id);
  if (!provider) return res.status(404).json({ error: 'Provider not found' });
  const { task, latency, cost, safety, context_length, region, modality, native_reasoning } = req.body;
  const caps = {
    task:             Array.isArray(task) ? task : (task ? [task] : ['chat']),
    latency:          latency   || 'medium',
    cost:             cost      || 'standard',
    safety:           safety    != null ? parseInt(safety) : 3,
    context_length:   context_length ? parseInt(context_length) : 32000,
    region:           Array.isArray(region) ? region : (region ? [region] : ['us']),
    modality:         Array.isArray(modality) ? modality : ['text', 'tool'],
    native_reasoning: !!native_reasoning,
  };
  sqliteDb.setModelCapabilities(provider.id, req.params.modelId, caps, 'manual');
  res.json({ ok: true, modelId: req.params.modelId, caps });
});

app.post('/api/test-provider', async (req, res) => {
  let { providerId, type, apiKey, projectId, location, baseUrl, model } = req.body;

  // If providerId is provided, look up the provider from server config (has unmasked keys)
  if (providerId) {
    const existingProvider = config.providers.find(p => p.id === providerId);
    if (!existingProvider) {
      return res.status(404).json({ error: 'Provider not found' });
    }

    // Use the provider from config which has the real API key
    type = existingProvider.type;
    apiKey = existingProvider.apiKey;
    projectId = existingProvider.projectId;
    location = existingProvider.location;
    baseUrl = existingProvider.baseUrl;
    model = existingProvider.model;

    logger.info('Testing existing provider by ID', { providerId, type });
  } else {
    // Testing new provider with provided credentials
    logger.info('Testing new provider with provided credentials', { type });
  }

  if (!type) {
    return res.status(400).json({ error: 'Type is required' });
  }

  // API key is optional for Ollama
  if (!apiKey && type !== 'ollama') {
    return res.status(400).json({ error: 'API key is required for this provider type' });
  }

  const startTime = Date.now();
  const testProvider = { type, apiKey, projectId, location, baseUrl, model };

  // Use appropriate default model for each provider type
  let defaultModel;
  switch(type) {
    case 'anthropic':
      defaultModel = 'claude-sonnet-4-5-20250929';
      break;
    case 'google':
    case 'vertex':
      defaultModel = 'gemini-2.5-flash';
      break;
    case 'grok':
      defaultModel = 'grok-beta';
      break;
    case 'openai':
      defaultModel = 'gpt-4o-mini';
      break;
    case 'openai-compatible':
      defaultModel = 'gpt-3.5-turbo';
      break;
    case 'ollama':
      defaultModel = 'llama2';
      break;
    default:
      defaultModel = 'gpt-3.5-turbo';
  }

  const testRequest = {
    model: model || defaultModel,
    max_tokens: 10,
    messages: [{ role: 'user', content: 'Hi' }]
  };

  try {
    let result;

    switch(type) {
      case 'anthropic':
        result = await callAnthropic(testProvider, testRequest);
        break;
      case 'google':
        result = await callGemini(testProvider, testRequest);
        break;
      case 'vertex':
        result = await callVertex(testProvider, testRequest);
        break;
      case 'grok':
        result = await callGrok(testProvider, testRequest);
        break;
      case 'ollama':
        result = await callOllama(testProvider, testRequest);
        break;
      case 'openai':
        result = await callOpenAI(testProvider, testRequest);
        break;
      case 'openai-compatible':
        result = await callOpenAICompatible(testProvider, testRequest);
        break;
      default:
        return res.status(400).json({ error: 'Invalid provider type' });
    }

    const latency = Date.now() - startTime;

    logger.info('Provider test successful', { type, latency });

    // Update stats if this is an existing provider
    if (providerId) {
      initStats(providerId);
      config.stats[providerId].lastSuccess = {
        timestamp: new Date().toISOString()
      };
      saveStatsRecord(providerId);
    }

    // Log activity
    addActivityLog('success', `Provider test successful: ${type}`, {
      provider: type,
      latency: `${latency}ms`,
      model: model || defaultModel
    });
    if (!USE_SQLITE) saveConfig();

    res.json({
      success: true,
      latency,
      response: result.content?.[0]?.text || 'Provider responded successfully',
      usage: result.usage
    });
  } catch (error) {
    const latency = Date.now() - startTime;

    logger.error('Provider test failed', { type, error: error.message, latency });

    // Update stats if this is an existing provider
    if (providerId) {
      initStats(providerId);
      config.stats[providerId].lastError = {
        message: error.response?.data?.error?.message || error.message,
        timestamp: new Date().toISOString()
      };
      saveStatsRecord(providerId);
    }

    // Log activity
    addActivityLog('error', `Provider test failed: ${type}`, {
      provider: type,
      latency: `${latency}ms`,
      error: error.response?.data?.error?.message || error.message
    });
    if (!USE_SQLITE) saveConfig();

    res.json({
      success: false,
      latency,
      error: error.response?.data?.error?.message || error.message
    });
  }
});

// Authentication endpoints
app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;

  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }

  const user = config.users.find(u => u.username === username);

  if (!user) {
    logger.warn('Login attempt with invalid username', { username });
    addActivityLog('warning', `Failed login attempt for user: ${username}`, { reason: 'Invalid username' });
    if (!USE_SQLITE) saveConfig();
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  const passwordMatch = await bcrypt.compare(password, user.password);

  if (!passwordMatch) {
    logger.warn('Login attempt with invalid password', { username });
    addActivityLog('warning', `Failed login attempt for user: ${username}`, { reason: 'Invalid password' });
    if (!USE_SQLITE) saveConfig();
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  // Apply configurable session timeout (default 480 minutes = 8 hours)
  const sessionTimeoutMinutes = parseInt(config.smtp?.sessionTimeoutMinutes) || 480;
  req.session.cookie.maxAge = sessionTimeoutMinutes * 60 * 1000;

  req.session.user = {
    id: user.id,
    username: user.username,
    role: user.role
  };

  // Layer 5: register session in active session registry
  req.session.save(() => {
    registerSession(req.session.id, user, req);
  });

  logger.info('User logged in', { username: user.username, sessionTimeoutMinutes, requestId: req.requestId });
  addActivityLog('success', `User logged in: ${user.username}`, { role: user.role });
  if (!USE_SQLITE) saveConfig();

  res.json({ success: true, user: { username: user.username, role: user.role } });
});

app.post('/api/auth/logout', (req, res) => {
  const username = req.session?.user?.username;
  const sessionId = req.session?.id;
  revokeSession(sessionId);
  req.session.destroy();
  logger.info('User logged out', { username });
  res.json({ success: true });
});

app.get('/api/auth/check', (req, res) => {
  if (req.session && req.session.user) {
    res.json({ authenticated: true, user: req.session.user });
  } else {
    res.json({ authenticated: false });
  }
});

// User management endpoints (admin only)
app.get('/api/users', requireAuth, (req, res) => {
  if (req.session.user.role !== 'admin') {
    return res.status(403).json({ error: 'Admin access required' });
  }

  const safeUsers = config.users.map(u => ({
    id: u.id,
    username: u.username,
    role: u.role,
    created: u.created
  }));

  res.json(safeUsers);
});

app.post('/api/users', requireAuth, async (req, res) => {
  if (req.session.user.role !== 'admin') {
    return res.status(403).json({ error: 'Admin access required' });
  }

  const { username, password, role, email } = req.body;

  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }

  if (config.users.find(u => u.username === username)) {
    return res.status(400).json({ error: 'Username already exists' });
  }

  const hashedPassword = await bcrypt.hash(password, 10);
  const newUser = {
    id: `user-${Date.now()}`,
    username,
    password: hashedPassword,
    role: role || 'user',
    email: email || '',
    theme: 'dark', // Default to dark mode
    created: new Date().toISOString()
  };

  config.users.push(newUser);
  saveUserRecord(newUser);

  logger.info('User created', { username, role: newUser.role });
  res.json({ id: newUser.id, username: newUser.username, role: newUser.role, email: newUser.email });
});

app.delete('/api/users/:id', requireAuth, (req, res) => {
  if (req.session.user.role !== 'admin') {
    return res.status(403).json({ error: 'Admin access required' });
  }

  const { id } = req.params;
  const index = config.users.findIndex(u => u.id === id);

  if (index === -1) {
    return res.status(404).json({ error: 'User not found' });
  }

  if (config.users[index].username === req.session.user.username) {
    return res.status(400).json({ error: 'Cannot delete your own account' });
  }

  const deleted = config.users.splice(index, 1)[0];
  deleteUserRecord(deleted.id);

  logger.info('User deleted', { username: deleted.username });
  res.json({ success: true });
});

// Email configuration for password resets
const emailTransporter = nodemailer.createTransport({
  host: process.env.SMTP_HOST || 'localhost',
  port: process.env.SMTP_PORT || 587,
  secure: process.env.SMTP_SECURE === 'true',
  auth: process.env.SMTP_USER ? {
    user: process.env.SMTP_USER,
    pass: process.env.SMTP_PASSWORD
  } : undefined
});

// Password reset tokens storage (in-memory for now, could be moved to config)
const passwordResetTokens = {};

// Profile management endpoints
app.get('/api/profile', requireAuth, (req, res) => {
  const user = config.users.find(u => u.id === req.session.user.id);

  if (!user) {
    return res.status(404).json({ error: 'User not found' });
  }

  res.json({
    id: user.id,
    username: user.username,
    email: user.email || '',
    theme: user.theme || 'dark',
    role: user.role
  });
});

app.post('/api/profile', requireAuth, async (req, res) => {
  const { email, theme } = req.body;

  const userIndex = config.users.findIndex(u => u.id === req.session.user.id);

  if (userIndex === -1) {
    return res.status(404).json({ error: 'User not found' });
  }

  if (email !== undefined) {
    config.users[userIndex].email = email;
  }

  if (theme !== undefined && (theme === 'dark' || theme === 'light')) {
    config.users[userIndex].theme = theme;
  }

  saveUserRecord(config.users[userIndex]);

  logger.info('Profile updated', { username: config.users[userIndex].username });
  res.json({
    id: config.users[userIndex].id,
    username: config.users[userIndex].username,
    email: config.users[userIndex].email,
    theme: config.users[userIndex].theme,
    role: config.users[userIndex].role
  });
});

app.post('/api/profile/password', requireAuth, async (req, res) => {
  const { newPassword } = req.body;

  if (!newPassword || newPassword.length < 6) {
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  }

  const userIndex = config.users.findIndex(u => u.id === req.session.user.id);

  if (userIndex === -1) {
    return res.status(404).json({ error: 'User not found' });
  }

  const hashedPassword = await bcrypt.hash(newPassword, 10);
  config.users[userIndex].password = hashedPassword;

  saveUserRecord(config.users[userIndex]);

  logger.info('Password changed', { username: config.users[userIndex].username });
  addActivityLog('success', `Password changed for user: ${config.users[userIndex].username}`);

  res.json({ success: true, message: 'Password updated successfully' });
});

// Forgot password - request reset (uses username instead of email)
// ── Layer 5: Session Management Endpoints ────────────────────────────────────

// GET /api/sessions — list active sessions for the current user
app.get('/api/sessions', requireAuth, (req, res) => {
  const userId = req.session.user.id;
  const currentSessionId = req.session.id;
  const sessions = [];
  for (const [id, s] of activeSessions) {
    if (s.userId === userId) {
      sessions.push({ ...s, current: id === currentSessionId });
    }
  }
  sessions.sort((a, b) => new Date(b.loginTime) - new Date(a.loginTime));
  res.json({ sessions });
});

// DELETE /api/sessions/:sessionId — revoke a specific session (must belong to current user)
app.delete('/api/sessions/:sessionId', requireAuth, (req, res) => {
  const { sessionId } = req.params;
  const s = activeSessions.get(sessionId);
  if (!s || s.userId !== req.session.user.id) {
    return res.status(404).json({ error: 'Session not found' });
  }
  revokeSession(sessionId);
  logger.info('Session revoked', { by: req.session.user.username, sessionId, requestId: req.requestId });
  res.json({ success: true });
});

// DELETE /api/sessions — revoke all sessions for current user except the current one
app.delete('/api/sessions', requireAuth, (req, res) => {
  const userId = req.session.user.id;
  const currentSessionId = req.session.id;
  let count = 0;
  for (const [id] of [...activeSessions]) {
    const s = activeSessions.get(id);
    if (s?.userId === userId && id !== currentSessionId) {
      revokeSession(id);
      count++;
    }
  }
  logger.info('All other sessions revoked', { by: req.session.user.username, count, requestId: req.requestId });
  res.json({ success: true, revoked: count });
});

app.post('/api/auth/forgot-password', async (req, res) => {
  const { username } = req.body;

  if (!username) {
    return res.status(400).json({ error: 'Username required' });
  }

  const user = config.users.find(u => u.username === username);

  // Always return success to prevent username enumeration
  if (!user) {
    logger.warn('Password reset requested for non-existent username', { username });
    return res.json({ success: true, message: 'If an account exists with that username and has an email on file, a reset link has been sent' });
  }

  // Check if user has email
  if (!user.email || user.email.trim() === '') {
    logger.warn('Password reset requested for user without email', { username });
    // Still return success to prevent information leakage
    return res.json({ success: true, message: 'If an account exists with that username and has an email on file, a reset link has been sent' });
  }

  // Check if SMTP is configured
  if (!config.smtp || !config.smtp.enabled || !config.smtp.host) {
    logger.error('Password reset attempted but SMTP not configured');
    // Return generic success to prevent leaking SMTP configuration status
    return res.json({ success: true, message: 'If an account exists with that username and has an email on file, a reset link has been sent' });
  }

  // Generate reset token
  const token = crypto.randomBytes(32).toString('hex');
  const expires = Date.now() + 3600000; // 1 hour

  passwordResetTokens[token] = {
    username: user.username,
    expires: expires,
    used: false
  };

  // Clean up expired tokens
  for (const key in passwordResetTokens) {
    if (passwordResetTokens[key].expires < Date.now()) {
      delete passwordResetTokens[key];
    }
  }

  // Send email using NotificationManager
  const resetUrl = `${process.env.APP_URL || 'http://localhost:3000'}/reset-password.html?token=${token}`;

  try {
    if (notificationManager.transporter) {
      await notificationManager.transporter.sendMail({
        from: config.smtp.from || 'noreply@llmproxy.local',
        to: user.email,
        subject: 'Password Reset Request - LLM Proxy Manager',
        html: `
          <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: #667eea;">Password Reset Request</h2>
            <p>Hello <strong>${user.username}</strong>,</p>
            <p>You requested a password reset for your LLM Proxy Manager account.</p>
            <p>Click the button below to reset your password:</p>
            <div style="text-align: center; margin: 30px 0;">
              <a href="${resetUrl}" style="background: #667eea; color: white; padding: 12px 30px; text-decoration: none; border-radius: 6px; display: inline-block;">Reset Password</a>
            </div>
            <p>Or copy and paste this link into your browser:</p>
            <p style="background: #f5f5f5; padding: 10px; border-radius: 4px; word-break: break-all;">${resetUrl}</p>
            <p><strong>This link will expire in 1 hour.</strong></p>
            <p>If you did not request this reset, please ignore this email. Your password will remain unchanged.</p>
            <hr style="margin: 30px 0; border: none; border-top: 1px solid #ddd;">
            <p style="color: #666; font-size: 12px;">This is an automated message from LLM Proxy Manager. Please do not reply to this email.</p>
          </div>
        `
      });

      logger.info('Password reset email sent', { username: user.username, email: user.email });
      addActivityLog('info', `Password reset email sent to user: ${user.username}`);
      if (!USE_SQLITE) saveConfig();
    } else {
      throw new Error('Email transporter not initialized');
    }
  } catch (error) {
    logger.error('Failed to send password reset email', { error: error.message, username: user.username });
    addActivityLog('error', `Failed to send password reset email for user: ${user.username}`, { error: error.message });
    if (!USE_SQLITE) saveConfig();
    // Still return success to prevent leaking information
  }

  res.json({ success: true, message: 'If an account exists with that username and has an email on file, a reset link has been sent' });
});

// Verify reset token
app.get('/api/auth/verify-reset-token/:token', (req, res) => {
  const { token } = req.params;

  const resetData = passwordResetTokens[token];

  if (!resetData) {
    return res.json({ valid: false, error: 'Invalid or expired reset token' });
  }

  if (resetData.expires < Date.now()) {
    delete passwordResetTokens[token];
    return res.json({ valid: false, error: 'Reset token has expired' });
  }

  if (resetData.used) {
    return res.json({ valid: false, error: 'Reset token has already been used' });
  }

  res.json({ valid: true, username: resetData.username });
});

// Reset password
app.post('/api/auth/reset-password', async (req, res) => {
  const { token, newPassword } = req.body;

  if (!token || !newPassword) {
    return res.status(400).json({ error: 'Token and new password required' });
  }

  if (newPassword.length < 6) {
    return res.status(400).json({ error: 'Password must be at least 6 characters' });
  }

  const resetData = passwordResetTokens[token];

  if (!resetData) {
    return res.status(400).json({ error: 'Invalid or expired reset token' });
  }

  if (resetData.expires < Date.now()) {
    delete passwordResetTokens[token];
    return res.status(400).json({ error: 'Reset token has expired' });
  }

  if (resetData.used) {
    return res.status(400).json({ error: 'Reset token has already been used' });
  }

  const userIndex = config.users.findIndex(u => u.username === resetData.username);

  if (userIndex === -1) {
    delete passwordResetTokens[token];
    return res.status(404).json({ error: 'User not found' });
  }

  const hashedPassword = await bcrypt.hash(newPassword, 10);
  config.users[userIndex].password = hashedPassword;

  // Mark token as used
  passwordResetTokens[token].used = true;

  saveUserRecord(config.users[userIndex]);

  logger.info('Password reset completed', { username: config.users[userIndex].username });
  addActivityLog('success', `Password reset completed for user: ${config.users[userIndex].username}`);

  res.json({ success: true, message: 'Password reset successfully' });
});

// Initialize
loadConfig();
initializeUsers();

// Initialize Provider Hold-Down
const providerMonitor = new ProviderHoldDown(logger, (providerId) => {
  return config.providers.find(p => p.id === providerId) || null;
});

// Initialize Cluster Manager
const clusterManager = new ClusterManager(logger, config);

// Initialize Notification Manager
const notificationManager = new NotificationManager(logger, config);

// Hold-down event handlers
providerMonitor.on('holddown.entered', ({ provider, consecutiveFailures }) => {
  const state = providerMonitor.getState(provider.id);
  addActivityLog('warning', `Provider ${provider.name} entered hold-down after ${consecutiveFailures} consecutive failures`, {
    providerId: provider.id,
    holdDownUntil: state.holdDownUntil ? new Date(state.holdDownUntil).toISOString() : null
  });
});

providerMonitor.on('holddown.cleared', ({ provider }) => {
  addActivityLog('success', `Provider ${provider.name} restored — hold-down retest passed`, {
    providerId: provider.id
  });
});

providerMonitor.on('holddown.restarted', ({ provider }) => {
  addActivityLog('warning', `Provider ${provider.name} hold-down restarted — retest failed`, {
    providerId: provider.id
  });
});

// Cluster event handlers
clusterManager.on('peer.unhealthy', (peer) => {
  addActivityLog('warning', `Cluster peer unhealthy: ${peer.name}`, {
    peerId: peer.id
  });
  notificationManager.alertClusterNodeDown(peer);
  if (!USE_SQLITE) saveConfig();
});

clusterManager.on('peer.healthy', (peer) => {
  addActivityLog('info', `Cluster peer healthy: ${peer.name}`, {
    peerId: peer.id,
    latency: peer.latency
  });
  if (!USE_SQLITE) saveConfig();
});

clusterManager.on('config.merged', ({ peer, changes }) => {
  addActivityLog('info', `Configuration synchronized from ${peer}`, {
    changes: changes
  });
  if (!USE_SQLITE) saveConfig();
});

// Add startup log entry
addActivityLog('info', 'LLM Proxy server started', {
  enabledProviders: config.providers.filter(p => p.enabled).length,
  totalProviders: config.providers.length,
  clusterEnabled: clusterManager.enabled
});
if (!USE_SQLITE) saveConfig();

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
  if (!USE_SQLITE) saveConfig();

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
      providers: config.providers,
      deletedProviderIds: config.deletedProviderIds || [],
      activityLog: process.env.CLUSTER_SYNC_ACTIVITY_LOG === 'true'
        ? config.activityLog
        : []
    }
  });
});

// Cluster status endpoint (for client applications)
app.get('/cluster/status', requireAuth, (req, res) => {
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

// Update cluster node name
app.put('/cluster/node', requireAuth, (req, res) => {
  const { nodeType, nodeId, name } = req.body;
  if (!name) return res.status(400).json({ error: 'name required' });

  const clusterConfig = config.cluster || {};

  if (nodeType === 'local') {
    clusterConfig.localName = name;
    clusterManager.nodeName = name;
  } else {
    const nodes = clusterConfig.nodes || [];
    const node = nodes.find(n => n.name === nodeId || n.host === nodeId);
    if (node) {
      // Update peer in-memory name too
      const peer = clusterManager.peers.find(p => p.id === nodeId);
      if (peer) peer.name = name;
      node.name = name;
    }
  }

  config.cluster = clusterConfig;
  saveClusterRecord();
  res.json({ success: true });
});

// Hold-down monitoring status
app.get('/monitoring/status', requireAuth, (req, res) => {
  res.json(providerMonitor.getMonitoringStatus());
});

// Manual hold-down release (admin UI)
app.post('/monitoring/holddown/release', requireAuth, (req, res) => {
  const { providerId } = req.body;
  if (!providerId) {
    return res.status(400).json({ error: 'providerId required' });
  }
  providerMonitor.manualRelease(providerId);
  addActivityLog('info', `Hold-down manually released for provider`, {
    providerId,
    username: req.session.username
  });
  res.json({ success: true, message: 'Hold-down released' });
});

// Manual hold-down apply (admin UI)
app.post('/monitoring/holddown/apply', requireAuth, (req, res) => {
  const { providerId, durationSeconds } = req.body;
  if (!providerId) {
    return res.status(400).json({ error: 'providerId required' });
  }
  providerMonitor.manualHold(providerId, durationSeconds);
  addActivityLog('warning', `Hold-down manually applied to provider`, {
    providerId,
    durationSeconds: durationSeconds || 180,
    username: req.session.username
  });
  res.json({ success: true, message: 'Hold-down applied' });
});

// ==================== END CLUSTER ENDPOINTS ====================
app.listen(PORT, '0.0.0.0', () => {
  logger.info(`LLM Proxy server running on port ${PORT}`);
  logger.info(`SSE streaming support: ENABLED`);
  logger.info(`Enabled providers: ${config.providers.filter(p => p.enabled).length}`);
  logger.info(`Cluster mode: ${clusterManager.enabled ? 'ENABLED' : 'DISABLED'}`);

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

  process.on('SIGINT', () => {
    logger.info('SIGINT received, shutting down gracefully...');
    providerMonitor.stop();
    clusterManager.stop();
    process.exit(0);
  });
});
