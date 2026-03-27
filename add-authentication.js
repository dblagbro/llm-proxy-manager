// Script to add authentication to server.js
const fs = require('fs');
const bcrypt = require('bcrypt');

// Read current server.js
let serverCode = fs.readFileSync('src/server.js', 'utf8');

// Add imports after existing requires
const authImports = `const bcrypt = require('bcrypt');
const session = require('express-session');
const cookieParser = require('cookie-parser');
`;

serverCode = serverCode.replace(
  "const winston = require('winston');",
  `const winston = require('winston');\n${authImports}`
);

// Add session middleware after existing middleware
const sessionMiddleware = `
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
`;

serverCode = serverCode.replace(
  "app.use(express.static('public'));",
  `app.use(express.static('public'));\n${sessionMiddleware}`
);

// Add users array to config
serverCode = serverCode.replace(
  '  clientApiKeys: []',
  `  clientApiKeys: [],
  users: []`
);

// Add authentication middleware function after initStats
const authMiddlewareFn = `
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
    const hashedPassword = await bcrypt.hash('Super*120120', 10);
    config.users.push({
      id: 'user-admin',
      username: 'dblagbro',
      password: hashedPassword,
      role: 'admin',
      created: new Date().toISOString()
    });
    saveConfig();
    logger.info('Default admin user created: dblagbro');
  }
}
`;

serverCode = serverCode.replace(
  '// API Key validation middleware',
  authMiddlewareFn + '\n// API Key validation middleware'
);

// Add login/logout/auth check endpoints before the Initialize section
const authEndpoints = `
// Authentication endpoints
app.post('/api/auth/login', async (req, res) => {
  const { username, password } = req.body;

  if (!username || !password) {
    return res.status(400).json({ error: 'Username and password required' });
  }

  const user = config.users.find(u => u.username === username);

  if (!user) {
    logger.warn('Login attempt with invalid username', { username });
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  const passwordMatch = await bcrypt.compare(password, user.password);

  if (!passwordMatch) {
    logger.warn('Login attempt with invalid password', { username });
    return res.status(401).json({ error: 'Invalid credentials' });
  }

  req.session.user = {
    id: user.id,
    username: user.username,
    role: user.role
  };

  logger.info('User logged in', { username: user.username });
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
    id: \`user-\${Date.now()}\`,
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

`;

serverCode = serverCode.replace(
  '// Initialize\nloadConfig();',
  authEndpoints + '\n// Initialize\nloadConfig();\ninitializeUsers();'
);

// Write updated server
fs.writeFileSync('src/server-with-auth.js', serverCode);
console.log('✓ Created src/server-with-auth.js with authentication');
