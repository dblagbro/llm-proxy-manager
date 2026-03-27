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
const ClusterManager = require('./cluster');
const ProviderMonitor = require('./monitor');
const NotificationManager = require('./notifications');

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
    new winston.transports.File({ filename: '/app/logs/error.log', level: 'error' }),
    new winston.transports.File({ filename: '/app/logs/combined.log' }),
    new winston.transports.Console({ format: winston.format.simple() })
  ]
});

// Middleware
app.use(cors());
app.use(bodyParser.json({ limit: '10mb' }));
app.use(express.static('public'));

// Session configuration
app.use(cookieParser());
app.use(session({
  secret: process.env.SESSION_SECRET || 'llm-proxy-secret-change-in-production',
  resave: false,
  saveUninitialized: false,
  cookie: {
    secure: false, // Set to true if using HTTPS
    httpOnly: true,
    maxAge: 24 * 60 * 60 * 1000 // 24 hours
  }
}));

// Config file path
const CONFIG_PATH = '/app/config/providers.json';

// Default configuration - minimal example providers
// Providers can be added/configured through the Web UI or environment variables
let config = {
  providers: [],
  stats: {},
  clientApiKeys: [],
  users: [],
  activityLog: []
};

// Load/Save config functions
function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = fs.readFileSync(CONFIG_PATH, 'utf8');
      config = JSON.parse(data);

      // Initialize activityLog if it doesn't exist
      if (!config.activityLog) {
        config.activityLog = [];
      }

      logger.info('Configuration loaded from file');
    } else {
      saveConfig();
    }
  } catch (error) {
    logger.error('Error loading config:', error);
  }
}

function saveConfig() {
  try {
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
    logger.info('Configuration saved');
  } catch (error) {
    logger.error('Error saving config:', error);
  }
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

  config.activityLog.unshift(entry);  // Add to beginning

  // Keep only last 100 entries
  if (config.activityLog.length > 100) {
    config.activityLog = config.activityLog.slice(0, 100);
  }

  // Save config periodically (every 10 entries)
  if (config.activityLog.length % 10 === 0) {
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
      lastUsed: null,
      lastError: null
    };
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
    const hashedPassword = await bcrypt.hash('admin', 10);
    config.users.push({
      id: 'user-admin',
      username: 'admin',
      password: hashedPassword,
      role: 'admin',
      created: new Date().toISOString()
    });
    saveConfig();
    logger.info('Default admin user created: admin (change password immediately!)');
  }
}

// API Key validation middleware
function validateApiKey(req, res, next) {
  // Exempt certain paths from API key validation
  const exemptPaths = ['/health', '/api/config', '/api/stats', '/api/client-keys', '/api/test-provider', '/'];
  const isExempt = exemptPaths.some(path => req.path === path || req.path.startsWith(path));

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

  // Attach client key info to request
  req.clientKey = clientKey;
  clientKey.lastUsed = new Date().toISOString();

  logger.info('API key validated', { keyName: clientKey.name, keyId: clientKey.id });
  next();
}

// Translate Anthropic request to Google Gemini format
function translateToGemini(anthropicRequest) {
  const messages = anthropicRequest.messages || [];
  let parts = [];

  for (const msg of messages) {
    const role = msg.role === 'assistant' ? 'model' : 'user';
    const text = typeof msg.content === 'string' ? msg.content :
                 Array.isArray(msg.content) ? msg.content.map(c => c.text || '').join('') : '';
    parts.push({ role, parts: [{ text }] });
  }

  return {
    contents: parts,
    generationConfig: {
      temperature: anthropicRequest.temperature || 1.0,
      maxOutputTokens: anthropicRequest.max_tokens || 4096,
      topP: anthropicRequest.top_p || 1.0,
    }
  };
}

// Translate Google Gemini response to Anthropic format (non-streaming)
function translateFromGemini(geminiResponse, model) {
  const candidate = geminiResponse.candidates?.[0];
  const content = candidate?.content?.parts?.[0]?.text || '';

  return {
    id: `msg_${Date.now()}`,
    type: 'message',
    role: 'assistant',
    content: [{ type: 'text', text: content }],
    model: model || 'gemini-1.5-pro',
    stop_reason: candidate?.finishReason === 'STOP' ? 'end_turn' : 'max_tokens',
    usage: {
      input_tokens: geminiResponse.usageMetadata?.promptTokenCount || 0,
      output_tokens: geminiResponse.usageMetadata?.candidatesTokenCount || 0
    }
  };
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  // Pipe the SSE stream directly to client
  response.data.pipe(res);

  return new Promise((resolve, reject) => {
    response.data.on('end', () => resolve());
    response.data.on('error', (err) => reject(err));
  });
}

// Stream Google Gemini API responses (translate to Anthropic SSE format)
async function streamGemini(provider, request, res) {
  const geminiRequest = translateToGemini(request);
  const model = request.model?.includes('gemini') ? request.model : 'gemini-2.5-flash';

  const response = await axios.post(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:streamGenerateContent?key=${provider.apiKey}&alt=sse`,
    geminiRequest,
    {
      headers: { 'Content-Type': 'application/json' },
      responseType: 'stream',
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  const messageId = `msg_${Date.now()}`;
  let textBuffer = '';

  // Send Anthropic-format SSE events
  res.write(`event: message_start\n`);
  res.write(`data: ${JSON.stringify({
    type: 'message_start',
    message: {
      id: messageId,
      type: 'message',
      role: 'assistant',
      content: [],
      model: model,
      usage: { input_tokens: 0, output_tokens: 0 }
    }
  })}\n\n`);

  res.write(`event: content_block_start\n`);
  res.write(`data: ${JSON.stringify({
    type: 'content_block_start',
    index: 0,
    content_block: { type: 'text', text: '' }
  })}\n\n`);

  response.data.on('data', (chunk) => {
    const lines = chunk.toString().split('\n').filter(line => line.trim());

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          const text = data.candidates?.[0]?.content?.parts?.[0]?.text || '';

          if (text) {
            textBuffer += text;
            res.write(`event: content_block_delta\n`);
            res.write(`data: ${JSON.stringify({
              type: 'content_block_delta',
              index: 0,
              delta: { type: 'text_delta', text: text }
            })}\n\n`);
          }
        } catch (e) {
          logger.error('Error parsing Gemini chunk:', e);
        }
      }
    }
  });

  return new Promise((resolve, reject) => {
    response.data.on('end', () => {
      res.write(`event: content_block_stop\n`);
      res.write(`data: ${JSON.stringify({ type: 'content_block_stop', index: 0 })}\n\n`);

      res.write(`event: message_delta\n`);
      res.write(`data: ${JSON.stringify({
        type: 'message_delta',
        delta: { stop_reason: 'end_turn', stop_sequence: null },
        usage: { output_tokens: textBuffer.length }
      })}\n\n`);

      res.write(`event: message_stop\n`);
      res.write(`data: ${JSON.stringify({ type: 'message_stop' })}\n\n`);

      resolve();
    });

    response.data.on('error', reject);
  });
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
    }
  );

  return translateFromGemini(response.data, model);
}

// Grok (xAI)
async function callGrok(provider, request) {
  const response = await axios.post(
    'https://api.x.ai/v1/chat/completions',
    {
      model: request.model || 'grok-beta',
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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
  const response = await axios.post(
    'https://api.openai.com/v1/chat/completions',
    {
      model: request.model || provider.model || 'gpt-4o-mini',
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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

// OpenAI-compatible API (for 3rd party services)
async function callOpenAICompatible(provider, request) {
  const baseUrl = provider.baseUrl || 'https://api.openai.com/v1';

  const response = await axios.post(
    `${baseUrl}/chat/completions`,
    {
      model: request.model || provider.model || 'gpt-3.5-turbo',
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
      timeout: providerMonitor.getProviderTimeout(provider.type)
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

// Main proxy endpoint
app.post('/v1/messages', validateApiKey, async (req, res) => {
  const startTime = Date.now();
  const isStreaming = req.body.stream === true;

  logger.info('Received request', {
    model: req.body.model,
    messageCount: req.body.messages?.length,
    streaming: isStreaming
  });

  // Get enabled providers sorted by priority
  let enabledProviders = config.providers
    .filter(p => p.enabled && p.apiKey)
    .sort((a, b) => a.priority - b.priority);

  // Apply circuit breaker filtering
  enabledProviders = enabledProviders.filter(p => {
    const check = providerMonitor.canAttemptProvider(p);
    if (!check.allowed) {
      logger.warn(`Provider ${p.name} blocked by circuit breaker: ${check.reason}`);
    }
    return check.allowed;
  });

  if (enabledProviders.length === 0) {
    logger.error('No available providers (all disabled or circuit breakers open)');
    return res.status(503).json({ error: 'No providers available' });
  }

  // Set SSE headers if streaming
  if (isStreaming) {
    res.setHeader('Content-Type', 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('Connection', 'keep-alive');
  }

  // Try providers in order
  for (const provider of enabledProviders) {
    initStats(provider.id);

    try {
      logger.info(`Trying provider: ${provider.name} (streaming: ${isStreaming})`);

      // Increment requests counter only when actually attempting this provider
      config.stats[provider.id].requests++;

      if (isStreaming) {
        // Streaming mode
        if (provider.type === 'anthropic') {
          await streamAnthropic(provider, req.body, res);
        } else if (provider.type === 'google') {
          await streamGemini(provider, req.body, res);
        }
      } else {
        // Non-streaming mode
        let result;
        switch(provider.type) {
          case 'anthropic':
            result = await callAnthropic(provider, req.body);
            break;
          case 'google':
            result = await callGemini(provider, req.body);
            break;
          case 'vertex':
            result = await callVertex(provider, req.body);
            break;
          case 'grok':
            result = await callGrok(provider, req.body);
            break;
          case 'ollama':
            result = await callOllama(provider, req.body);
            break;
          case 'openai':
            result = await callOpenAI(provider, req.body);
            break;
          case 'openai-compatible':
            result = await callOpenAICompatible(provider, req.body);
            break;
          default:
            throw new Error(`Unsupported provider type: ${provider.type}`);
        }
        res.json(result);
      }

      const latency = Date.now() - startTime;
      config.stats[provider.id].successes++;
      config.stats[provider.id].totalLatency += latency;
      config.stats[provider.id].lastUsed = new Date().toISOString();

      // Record success with circuit breaker
      providerMonitor.recordSuccess(provider);

      logger.info(`Success with ${provider.name}`, { latency: `${latency}ms` });

      // Track client key usage
      if (req.clientKey) {
        req.clientKey.requests = (req.clientKey.requests || 0) + 1;
        req.clientKey.lastUsed = new Date().toISOString();
      }

      // Save stats periodically
      if (config.stats[provider.id].requests % 10 === 0) {
        saveConfig();
      }

      if (isStreaming) {
        res.end();
      }
      return;

    } catch (error) {
      const latency = Date.now() - startTime;
      config.stats[provider.id].failures++;
      config.stats[provider.id].lastError = {
        message: error.message,
        timestamp: new Date().toISOString()
      };

      // Record failure with circuit breaker
      providerMonitor.recordFailure(provider, error);

      logger.error(`Failed with ${provider.name}:`, {
        error: error.message,
        status: error.response?.status,
        latency: `${latency}ms`
      });

      // If this was the last provider, return error
      if (provider === enabledProviders[enabledProviders.length - 1]) {
        if (isStreaming) {
          res.write(`event: error\n`);
          res.write(`data: ${JSON.stringify({
            type: 'error',
            error: {
              type: 'api_error',
              message: 'All providers failed'
            }
          })}\n\n`);
          res.end();
        } else {
          return res.status(error.response?.status || 500).json({
            error: 'All providers failed',
            lastError: error.response?.data || error.message
          });
        }
        return;
      }

      // Otherwise continue to next provider
      logger.info('Failing over to next provider');
    }
  }
});

// Health check
app.get('/health', (req, res) => {
  const enabledCount = config.providers.filter(p => p.enabled).length;
  res.json({
    status: 'ok',
    providers: {
      total: config.providers.length,
      enabled: enabledCount
    },
    uptime: process.uptime()
  });
});

// Get config
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

// Update config
app.post('/api/config', (req, res) => {
  try {
    if (req.body.providers) {
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
          model: p.model
        };
      });
    }
    saveConfig();
    logger.info('Configuration updated', { providerCount: config.providers.length });

    // Log activity
    addActivityLog('info', 'Configuration saved', {
      providerCount: config.providers.length
    });
    saveConfig();  // Save again to persist activity log

    res.json({ success: true });
  } catch (error) {
    logger.error('Error updating config:', error);
    res.status(500).json({ error: error.message });
  }
});

// Get stats
app.get('/api/stats', (req, res) => {
  res.json(config.stats);
});

// Reset stats
app.post('/api/stats/reset', (req, res) => {
  config.stats = {};
  saveConfig();
  res.json({ success: true });
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
  const { name } = req.body;

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
    enabled: true
  };

  if (!config.clientApiKeys) {
    config.clientApiKeys = [];
  }

  config.clientApiKeys.push(newKey);
  saveConfig();

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
  saveConfig();

  logger.info('Client API key deleted', { name: deleted.name, id: deleted.id });
  res.json({ success: true, deleted });
});

app.patch('/api/client-keys/:id', (req, res) => {
  const { id } = req.params;
  const { enabled, name } = req.body;

  if (!config.clientApiKeys) {
    return res.status(404).json({ error: 'Key not found' });
  }

  const key = config.clientApiKeys.find(k => k.id === id);

  if (!key) {
    return res.status(404).json({ error: 'Key not found' });
  }

  if (enabled !== undefined) {
    key.enabled = Boolean(enabled);
  }

  if (name && name.trim() !== '') {
    key.name = name.trim();
  }

  saveConfig();

  logger.info('Client API key updated', { name: key.name, id: key.id, enabled: key.enabled });
  res.json(key);
});

// Test provider endpoint
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

    // Log activity
    addActivityLog('success', `Provider test successful: ${type}`, {
      provider: type,
      latency: `${latency}ms`,
      model: model || defaultModel
    });
    saveConfig();  // Save immediately for activity log

    res.json({
      success: true,
      latency,
      response: result.content?.[0]?.text || 'Provider responded successfully',
      usage: result.usage
    });
  } catch (error) {
    const latency = Date.now() - startTime;

    logger.error('Provider test failed', { type, error: error.message, latency });

    // Log activity
    addActivityLog('error', `Provider test failed: ${type}`, {
      provider: type,
      latency: `${latency}ms`,
      error: error.response?.data?.error?.message || error.message
    });
    saveConfig();  // Save immediately for activity log

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
    saveConfig();
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  const passwordMatch = await bcrypt.compare(password, user.password);

  if (!passwordMatch) {
    logger.warn('Login attempt with invalid password', { username });
    addActivityLog('warning', `Failed login attempt for user: ${username}`, { reason: 'Invalid password' });
    saveConfig();
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  req.session.user = {
    id: user.id,
    username: user.username,
    role: user.role
  };

  logger.info('User logged in', { username: user.username });
  addActivityLog('success', `User logged in: ${user.username}`, { role: user.role });
  saveConfig();

  res.json({ success: true, user: { username: user.username, role: user.role } });
});

app.post('/api/auth/logout', (req, res) => {
  const username = req.session?.user?.username;
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

  const { username, password, role } = req.body;

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
    created: new Date().toISOString()
  };

  config.users.push(newUser);
  saveConfig();

  logger.info('User created', { username, role: newUser.role });
  res.json({ id: newUser.id, username: newUser.username, role: newUser.role });
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
  saveConfig();

  logger.info('User deleted', { username: deleted.username });
  res.json({ success: true });
});

// Initialize
loadConfig();
initializeUsers();

// Initialize Provider Monitor
const providerMonitor = new ProviderMonitor(logger);

// Initialize Cluster Manager
const clusterManager = new ClusterManager(logger, config);

// Initialize Notification Manager
const notificationManager = new NotificationManager(logger, config);

// Monitor event handlers
providerMonitor.on('circuit.open', ({ provider, reason }) => {
  addActivityLog('warning', `Circuit breaker OPEN for ${provider.name}`, {
    providerId: provider.id,
    reason: reason
  });
  notificationManager.alertCircuitBreakerOpen(provider, reason);
  saveConfig();
});

providerMonitor.on('circuit.closed', ({ provider }) => {
  addActivityLog('success', `Circuit breaker CLOSED for ${provider.name} - recovered`, {
    providerId: provider.id
  });
  saveConfig();
});

providerMonitor.on('billing.error', ({ provider, error }) => {
  addActivityLog('error', `Billing/quota error detected for ${provider.name}`, {
    providerId: provider.id,
    error: error
  });
  notificationManager.alertBillingError(provider, error);
  saveConfig();
});

providerMonitor.on('external.degraded', ({ providerType, status, incidents }) => {
  addActivityLog('warning', `External service ${providerType} reporting ${status}`, {
    incidents: incidents
  });
  notificationManager.alertExternalServiceDown(providerType, status, incidents);
  saveConfig();
});

// Cluster event handlers
clusterManager.on('peer.unhealthy', (peer) => {
  addActivityLog('warning', `Cluster peer unhealthy: ${peer.name}`, {
    peerId: peer.id
  });
  notificationManager.alertClusterNodeDown(peer);
  saveConfig();
});

clusterManager.on('peer.healthy', (peer) => {
  addActivityLog('info', `Cluster peer healthy: ${peer.name}`, {
    peerId: peer.id,
    latency: peer.latency
  });
  saveConfig();
});

clusterManager.on('config.merged', ({ peer, changes }) => {
  addActivityLog('info', `Configuration synchronized from ${peer}`, {
    changes: changes
  });
  saveConfig();
});

// Add startup log entry
addActivityLog('info', 'LLM Proxy server started', {
  enabledProviders: config.providers.filter(p => p.enabled).length,
  totalProviders: config.providers.length,
  clusterEnabled: clusterManager.enabled
});
saveConfig();

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
  saveConfig();

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
  saveConfig();

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
