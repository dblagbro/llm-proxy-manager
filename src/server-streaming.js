const express = require('express');
const bodyParser = require('body-parser');
const cors = require('cors');
const axios = require('axios');
const fs = require('fs');
const winston = require('winston');

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

// Config file path
const CONFIG_PATH = '/app/config/providers.json';

// Default configuration
let config = {
  providers: [
    {
      id: 'anthropic-claude-3',
      name: 'Anthropic Claude Code #3',
      type: 'anthropic',
      apiKey: process.env.ANTHROPIC_KEY_3 || '',
      enabled: true,
      priority: 1
    },
    {
      id: 'anthropic-c1',
      name: 'C1 Anthropic Claude',
      type: 'anthropic',
      apiKey: process.env.ANTHROPIC_KEY_C1 || '',
      enabled: true,
      priority: 2
    },
    {
      id: 'google-gemini-1',
      name: 'Google Gemini API',
      type: 'google',
      apiKey: process.env.GOOGLE_API_KEY_1 || '',
      enabled: true,
      priority: 3
    },
    {
      id: 'google-vertex',
      name: 'C1 Vertex AI / Google AI',
      type: 'google',
      apiKey: process.env.GOOGLE_API_KEY_VERTEX || '',
      projectId: process.env.GOOGLE_PROJECT_ID || 'c1-ai-center-of-excellence',
      enabled: true,
      priority: 4
    }
  ],
  stats: {}
};

// Load/Save config functions
function loadConfig() {
  try {
    if (fs.existsSync(CONFIG_PATH)) {
      const data = fs.readFileSync(CONFIG_PATH, 'utf8');
      config = JSON.parse(data);
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
    model: model || 'gemini-pro',
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
      timeout: 120000
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
  const model = request.model?.includes('gemini') ? request.model : 'gemini-pro';

  const response = await axios.post(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:streamGenerateContent?key=${provider.apiKey}&alt=sse`,
    geminiRequest,
    {
      headers: { 'Content-Type': 'application/json' },
      responseType: 'stream',
      timeout: 120000
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
      timeout: 120000
    }
  );

  return response.data;
}

async function callGemini(provider, request) {
  const geminiRequest = translateToGemini(request);
  const model = request.model?.includes('gemini') ? request.model : 'gemini-pro';

  const response = await axios.post(
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${provider.apiKey}`,
    geminiRequest,
    {
      headers: { 'Content-Type': 'application/json' },
      timeout: 120000
    }
  );

  return translateFromGemini(response.data, model);
}

// Main proxy endpoint
app.post('/v1/messages', async (req, res) => {
  const startTime = Date.now();
  const isStreaming = req.body.stream === true;

  logger.info('Received request', {
    model: req.body.model,
    messageCount: req.body.messages?.length,
    streaming: isStreaming
  });

  // Get enabled providers sorted by priority
  const enabledProviders = config.providers
    .filter(p => p.enabled && p.apiKey)
    .sort((a, b) => a.priority - b.priority);

  if (enabledProviders.length === 0) {
    logger.error('No enabled providers available');
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
    config.stats[provider.id].requests++;

    try {
      logger.info(`Trying provider: ${provider.name} (streaming: ${isStreaming})`);

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
        if (provider.type === 'anthropic') {
          result = await callAnthropic(provider, req.body);
        } else if (provider.type === 'google') {
          result = await callGemini(provider, req.body);
        }
        res.json(result);
      }

      const latency = Date.now() - startTime;
      config.stats[provider.id].successes++;
      config.stats[provider.id].totalLatency += latency;
      config.stats[provider.id].lastUsed = new Date().toISOString();

      logger.info(`Success with ${provider.name}`, { latency: `${latency}ms` });

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
      req.body.providers.forEach((updatedProvider, idx) => {
        if (config.providers[idx]) {
          config.providers[idx].enabled = updatedProvider.enabled;
          config.providers[idx].priority = updatedProvider.priority;
          config.providers[idx].name = updatedProvider.name;
        }
      });
    }
    saveConfig();
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

// Initialize
loadConfig();

app.listen(PORT, '0.0.0.0', () => {
  logger.info(`LLM Proxy server running on port ${PORT}`);
  logger.info(`SSE streaming support: ENABLED`);
  logger.info(`Enabled providers: ${config.providers.filter(p => p.enabled).length}`);
});
