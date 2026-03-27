# Authentication Implementation Plan

## Current Status
- ❌ No authentication on Web UI
- ❌ No provider add/edit forms
- ✅ API key system for client apps works
- ✅ Basic Web UI exists

## What We Need

### 1. Backend Authentication
```javascript
// Add to server.js:
- bcrypt for password hashing
- express-session for session management
- Login/logout endpoints
- User management endpoints
- requireAuth middleware for Web UI routes
- Default admin user: dblagbro / Super*120120
```

### 2. Frontend Login Page
```html
- login.html with username/password form
- Session check on page load
- Redirect to login if not authenticated
- Logout button
```

### 3. Complete Provider Form
```html
- Modal with all fields:
  - Name (text input)
  - Type (dropdown: anthropic/google)
  - API Key (password input with show/hide)
  - Project ID (for Google, conditional)
  - Priority (number)
  - Enabled (checkbox)
- Test button
- Save button
```

### 4. User Management UI
```html
- Tab/section for user management
- List users
- Add user button
- Delete user button
- Change password
```

### 5. Playwright Tests
```javascript
test('login flow', async ({ page }) => {
  await page.goto('http://localhost:3100');
  await page.fill('[name=username]', 'dblagbro');
  await page.fill('[name=password]', 'Super*120120');
  await page.click('button[type=submit]');
  await expect(page).toHaveURL(/dashboard/);
});

test('add provider', async ({ page }) => {
  // login first
  // click "Add Provider"
  // fill form
  // submit
  // verify provider appears
});
```

## Implementation Approach

Due to token limits and complexity, I recommend:

### Quick Option (10 min):
1. Create login.html
2. Add session middleware + auth endpoints to server.js
3. Add requireAuth to Web UI route
4. Manual test

### Complete Option (30 min):
1. Full rewrite of server.js with auth
2. Complete Web UI with all forms
3. Playwright test suite
4. Automated deployment + testing

## Files to Create/Modify

1. `src/server.js` - Add authentication
2. `public/login.html` - New login page
3. `public/index.html` - Add auth check, complete forms
4. `tests/auth.spec.js` - Playwright tests
5. `package.json` - Add dependencies

## Testing Checklist

- [ ] Can login with dblagbro / Super*120120
- [ ] Cannot access Web UI without login
- [ ] Can add a new provider with full form
- [ ] Can edit existing provider
- [ ] Can test provider
- [ ] Can generate API key
- [ ] Can add new user
- [ ] Can logout

## Current Blocker

The session is timing out or we're at token limits. Let me create a simple, working implementation that we can deploy and test immediately.
