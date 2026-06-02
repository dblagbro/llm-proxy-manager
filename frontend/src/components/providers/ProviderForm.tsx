import { useState } from 'react'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { providersApi } from '@/api'
import { useToast } from '@/components/ui/Toast'
import type { ProviderType, Provider } from '@/types'
import { GrokWebProviderFields } from './GrokWebProviderFields'
import { AnthropicBillingPanel } from './AnthropicBillingPanel'
import { CodexBillingPanel } from './CodexBillingPanel'
import { NodeBridgeStatusPanel } from './NodeBridgeStatusPanel'

const PROVIDER_TYPES: ProviderType[] = [
  'anthropic', 'openai', 'google', 'vertex', 'grok', 'ollama', 'compatible',
  'claude-oauth',  // v2.7.0 — Claude Pro Max subscription via pasted credentials
  'ChatGPT-oauth-plan',   // v3.0.15 — OpenAI Codex CLI / ChatGPT subscription
  'cohere',        // v3.0.23 — Cohere embeddings (and rerank/chat)
  'azure',         // v3.0.66 — Microsoft Azure OpenAI Service
  'openrouter',    // v3.1.3 — OpenRouter multi-vendor marketplace
  'grok-web',      // v3.2.0 — grok.com web subscription via pasted cookies
  'cursor-oauth',  // v4.4.31 — Cursor Pro/Business subscription via cursor-bridge sidecar
]

// v3.8.0 — display labels for the provider_type dropdown. The internal
// string value was renamed from "codex-oauth" to "ChatGPT-oauth-plan"
// (#251). The label keeps the old name in parens as a transitional
// hint so operators familiar with the old name can still find it.
// Drop the "(codex-oauth)" suffix in a future version once operators
// have absorbed the change.
function providerTypeLabel(t: ProviderType): string {
  if (t === 'ChatGPT-oauth-plan') return 'ChatGPT-oauth-plan (was codex-oauth)'
  return t
}

// v3.0.15: per-OAuth-flavor copy + API method bindings, keyed by ProviderType.
// Lets the OAuth panel render correctly for either claude-oauth or codex-oauth
// without duplicating the markup.
type OAuthFlavor = {
  label: string
  callbackHostHint: string
  defaultModel: string
  pasteFallbackInstructions: { cmd: string; catFile: string; tokenShape: string }
  pasteFallbackPlaceholder: string
  authorize: () => Promise<{ state: string; authorize_url: string }>
  exchange: (data: Parameters<typeof providersApi.oauthExchange>[0]) => Promise<Provider>
  rotate: (id: string, data: { state: string; callback: string }) => Promise<Provider>
}

const OAUTH_FLAVORS: Record<string, OAuthFlavor> = {
  'claude-oauth': {
    label: 'Claude Pro Max — sign in with your subscription',
    callbackHostHint: 'platform.claude.com/oauth/code/callback',
    defaultModel: 'claude-sonnet-4-6',
    pasteFallbackInstructions: {
      cmd: 'claude login',
      catFile: '~/.claude/credentials.json',
      tokenShape: 'sk-ant-oat…',
    },
    pasteFallbackPlaceholder: '{\n  "access_token": "sk-ant-oat01-…",\n  "refresh_token": "…",\n  "expires_at": "2026-05-24T00:00:00Z"\n}\n\n— or just —\n\nsk-ant-oat01-…',
    authorize: () => providersApi.oauthAuthorize(),
    exchange: (data) => providersApi.oauthExchange(data),
    rotate: (id, data) => providersApi.oauthRotate(id, data),
  },
  'ChatGPT-oauth-plan': {
    label: 'ChatGPT subscription (Plus/Team/Enterprise) — sign in via Codex',
    callbackHostHint: 'localhost:1455/auth/callback (browser will dead-end here — copy the URL anyway)',
    defaultModel: 'gpt-5.5',
    pasteFallbackInstructions: {
      cmd: 'codex auth',
      catFile: '~/.codex/auth.json',
      tokenShape: 'JWT (three dot-separated base64 segments)',
    },
    pasteFallbackPlaceholder: '{\n  "tokens": {\n    "id_token": "eyJhbGciOi…",\n    "access_token": "eyJhbGciOi…",\n    "refresh_token": "…"\n  }\n}\n\n— or just —\n\neyJhbGciOi…',
    authorize: () => providersApi.codexOauthAuthorize(),
    exchange: (data) => providersApi.codexOauthExchange(data),
    rotate: (id, data) => providersApi.codexOauthRotate(id, data),
  },
  // v4.4.31 — Cursor Pro/Business subscription. Cursor's deep-link
  // login doesn't redirect with ``?code=…`` like Anthropic/OpenAI;
  // it lands a ``WorkosCursorSessionToken`` cookie on cursor.com.
  // The operator copies that cookie value from DevTools → Application
  // → Cookies and pastes it back; the backend POSTs it to the
  // cursor-bridge sidecar's ``/cursor/loginDeepControl`` to mint the
  // long-lived ``user_<id>::<JWT>`` access cookie we store as
  // Provider.api_key.
  'cursor-oauth': {
    label: 'Cursor Pro / Business — sign in with your Cursor account',
    callbackHostHint: 'cursor.com (DevTools → Application → Cookies → WorkosCursorSessionToken)',
    defaultModel: 'claude-4-sonnet',
    pasteFallbackInstructions: {
      cmd: 'npm run login  (inside the cursor-bridge sidecar)',
      catFile: '<the printed user_<id>::<JWT> string>',
      tokenShape: 'user_<id>::<JWT>',
    },
    pasteFallbackPlaceholder: 'user_01ABCDEFGH…::eyJhbGciOiJIUzI1NiI…\n\n— or as JSON —\n\n{\n  "access_token": "user_01ABCDEFGH…::eyJhbGciOi…"\n}',
    authorize: () => providersApi.cursorOauthAuthorize(),
    exchange: (data) => providersApi.cursorOauthExchange(data),
    rotate: (id, data) => providersApi.cursorOauthRotate(id, data),
  },
}

export type ProviderFormState = {
  name: string
  provider_type: ProviderType
  api_key: string
  base_url: string
  default_model: string
  priority: number
  enabled: boolean
  timeout_sec: number
  exclude_from_tool_requests: boolean
  hold_down_sec: number | null
  failure_threshold: number | null
  extra_config: Record<string, unknown>
  // v2.7.0: the credentials-paste fallback (bare token or JSON blob)
  oauth_credentials_blob: string
  // v2.7.1: the browser-initiated OAuth flow carries state + callback
  // across the authorize → user-opens-URL → paste-back cycle.
  oauth_state: string
  oauth_authorize_url: string
  oauth_callback: string
  // v3.0.64: per-provider usage tracking + rotation config (Phase 2)
  usage_tracking_enabled: boolean
  usage_session_window_sec: number | null
  usage_weekly_reset_dow: number | null   // 0=Mon … 6=Sun
  usage_weekly_reset_hour: number | null  // 0..23 local hour
  usage_session_limit_tokens: number | null
  usage_weekly_limit_tokens: number | null
  usage_rotation_threshold_pct: number | null
}

export function emptyProviderForm(): ProviderFormState {
  return {
    name: '',
    provider_type: 'openai',
    api_key: '',
    base_url: '',
    default_model: '',
    priority: 10,
    enabled: true,
    timeout_sec: 60,
    exclude_from_tool_requests: false,
    hold_down_sec: null,
    failure_threshold: null,
    extra_config: {},
    oauth_credentials_blob: '',
    oauth_state: '',
    oauth_authorize_url: '',
    oauth_callback: '',
    usage_tracking_enabled: false,
    usage_session_window_sec: null,
    usage_weekly_reset_dow: null,
    usage_weekly_reset_hour: null,
    usage_session_limit_tokens: null,
    usage_weekly_limit_tokens: null,
    usage_rotation_threshold_pct: null,
  }
}

export function providerToForm(p: Provider): ProviderFormState {
  return {
    name: p.name,
    provider_type: p.provider_type,
    api_key: '',
    base_url: p.base_url ?? '',
    default_model: p.default_model ?? '',
    priority: p.priority,
    enabled: p.enabled,
    timeout_sec: p.timeout_sec,
    exclude_from_tool_requests: p.exclude_from_tool_requests,
    hold_down_sec: p.hold_down_sec ?? null,
    failure_threshold: p.failure_threshold ?? null,
    extra_config: p.extra_config ?? {},
    oauth_credentials_blob: '',
    oauth_state: '',
    oauth_authorize_url: '',
    oauth_callback: '',
    usage_tracking_enabled: (p as unknown as { usage_tracking_enabled?: boolean }).usage_tracking_enabled ?? false,
    usage_session_window_sec: (p as unknown as { usage_session_window_sec?: number | null }).usage_session_window_sec ?? null,
    usage_weekly_reset_dow: (p as unknown as { usage_weekly_reset_dow?: number | null }).usage_weekly_reset_dow ?? null,
    usage_weekly_reset_hour: (p as unknown as { usage_weekly_reset_hour?: number | null }).usage_weekly_reset_hour ?? null,
    usage_session_limit_tokens: (p as unknown as { usage_session_limit_tokens?: number | null }).usage_session_limit_tokens ?? null,
    usage_weekly_limit_tokens: (p as unknown as { usage_weekly_limit_tokens?: number | null }).usage_weekly_limit_tokens ?? null,
    usage_rotation_threshold_pct: (p as unknown as { usage_rotation_threshold_pct?: number | null }).usage_rotation_threshold_pct ?? null,
  }
}

interface Props {
  form: ProviderFormState
  onChange: (f: ProviderFormState) => void
  editing: boolean
  // v3.7.5 — when editing an existing claude-oauth provider, pass the
  // full Provider so the AnthropicBillingPanel can render its scrape
  // state (org_uuid / cookies-present / auto_skip_until / snapshots).
  // Optional so create-flow callers don't have to construct a stub.
  provider?: Provider
  onProviderUpdated?: () => void
}

export function ProviderForm({ form, onChange, editing, provider, onProviderUpdated }: Props) {
  const set = (patch: Partial<ProviderFormState>) => onChange({ ...form, ...patch })
  const flavor = OAUTH_FLAVORS[form.provider_type]
  const isOAuth = !!flavor
  const [generating, setGenerating] = useState(false)
  const [showPasteFallback, setShowPasteFallback] = useState(false)
  const toast = useToast()

  async function handleGenerateAuthUrl() {
    if (!flavor) return
    setGenerating(true)
    try {
      const r = await flavor.authorize()
      set({ oauth_state: r.state, oauth_authorize_url: r.authorize_url, oauth_callback: '' })
    } catch (e: unknown) {
      toast.error((e as Error).message || 'Failed to generate auth URL')
    } finally {
      setGenerating(false)
    }
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      <Input
        label="Name"
        value={form.name}
        onChange={e => set({ name: e.target.value })}
        required
      />
      <div className="flex flex-col gap-1.5">
        <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Provider Type</label>
        <select
          value={form.provider_type}
          onChange={e => set({ provider_type: e.target.value as ProviderType })}
          className="px-3 py-2 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
        >
          {PROVIDER_TYPES.map(t => <option key={t} value={t}>{providerTypeLabel(t)}</option>)}
        </select>
      </div>

      {/* v2.7.1: OAuth new-create gets the browser-initiated flow.
          v3.0.15: same flow now drives both claude-oauth and codex-oauth via the
          OAUTH_FLAVORS lookup. Editing or "I already have a token" falls back to
          paste-credentials. */}
      {isOAuth ? (
        <div className="md:col-span-2 space-y-4 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/40 dark:bg-indigo-950/30 p-4">
          <div className="text-sm font-medium text-gray-800 dark:text-gray-100">
            {flavor.label}
          </div>

          {!showPasteFallback && (
            <div className="space-y-3">
              <ol className="list-decimal list-inside text-xs text-gray-600 dark:text-gray-300 space-y-1">
                {editing && <li className="text-amber-600 dark:text-amber-400 font-medium">Re-authorize this provider — replaces the stored access &amp; refresh tokens.</li>}
                <li>Click <strong>{editing ? 'Generate New Auth URL' : 'Generate Auth URL'}</strong> below.</li>
                {form.provider_type === 'cursor-oauth' ? (
                  <>
                    <li>Open the URL in a new tab and sign in to Cursor (any team).</li>
                    <li>Come back here and click <strong>{editing ? 'Save Provider' : 'Save Provider'}</strong>. The backend polls Cursor's IDE auth endpoint and grabs the access token automatically — no cookie copy.</li>
                  </>
                ) : (
                  <>
                    <li>Open the URL in a tab where you're signed in to your {form.provider_type === 'ChatGPT-oauth-plan' ? 'ChatGPT' : 'Claude'} account and approve access.</li>
                    <li>You'll be redirected to <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">{flavor.callbackHostHint}</code>.</li>
                    <li>Copy that code (or the full URL from your address bar) and paste it below. We'll trade it for a token automatically.</li>
                  </>
                )}
              </ol>

              <div className="flex items-center gap-2">
                <Button
                  type="button"
                  variant={form.oauth_authorize_url ? 'outline' : 'primary'}
                  size="sm"
                  onClick={handleGenerateAuthUrl}
                  loading={generating}
                >
                  {form.oauth_authorize_url ? 'Regenerate Auth URL' : (editing ? 'Generate New Auth URL' : 'Generate Auth URL')}
                </Button>
                {form.oauth_authorize_url && (
                  <a
                    href={form.oauth_authorize_url}
                    target="_blank"
                    rel="noreferrer"
                    className="text-xs text-indigo-600 dark:text-indigo-300 underline break-all"
                  >
                    Open Auth URL ↗
                  </a>
                )}
              </div>

              {form.oauth_authorize_url && form.provider_type === 'cursor-oauth' && (
                <div className="rounded border border-indigo-300 dark:border-indigo-700 bg-indigo-50/60 dark:bg-indigo-950/30 p-3 space-y-1.5">
                  <div className="text-xs font-semibold text-indigo-900 dark:text-indigo-200">
                    Waiting for your Cursor login…
                  </div>
                  <div className="text-[11px] text-gray-700 dark:text-gray-300">
                    Open the URL above, sign in to Cursor, then click <strong>Save Provider</strong> below. We'll poll Cursor's IDE auth endpoint and grab the token (typically &lt; 5 seconds after you finish logging in).
                  </div>
                  <div className="text-[11px] text-gray-500 dark:text-gray-400">
                    If polling times out, the most common cause is your sign-in didn't complete — try again, then click Save Provider once you see the Cursor success page.
                  </div>
                </div>
              )}

              {form.oauth_authorize_url && form.provider_type !== 'cursor-oauth' && (
                <>
                  <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
                    Paste the authorization code (or the full callback URL)
                  </label>
                  <textarea
                    value={form.oauth_callback}
                    onChange={e => set({ oauth_callback: e.target.value })}
                    rows={3}
                    placeholder={`code=…&state=…\n— or —\nhttp(s)://${flavor.callbackHostHint.split(' ')[0]}?code=…&state=…`}
                    className="w-full px-3 py-2 text-xs font-mono bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-300 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    required={!editing}
                  />
                </>
              )}

              <div className="text-[11px] text-gray-500 dark:text-gray-400">
                {editing ? 'Or ' : 'Already have a token from '}
                <code className="font-mono">{flavor.pasteFallbackInstructions.cmd}</code>?{' '}
                <button
                  type="button"
                  className="underline hover:text-gray-700 dark:hover:text-gray-200"
                  onClick={() => setShowPasteFallback(true)}
                >
                  Paste credentials instead
                </button>
              </div>
            </div>
          )}

          {showPasteFallback && (
            <div className="space-y-2">
              <ol className="list-decimal list-inside text-xs text-gray-600 dark:text-gray-300 space-y-1">
                <li>On any machine with the CLI installed, run <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">{flavor.pasteFallbackInstructions.cmd}</code></li>
                <li>Run <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">cat {flavor.pasteFallbackInstructions.catFile}</code> (or paste your bare <code className="font-mono">{flavor.pasteFallbackInstructions.tokenShape}</code> directly)</li>
                <li>Paste the entire output below and save. We parse, encrypt, and store — the blob itself is never persisted.</li>
              </ol>
              <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
                Credentials JSON or bare token
                {editing && <span className="ml-1 text-gray-400">(leave blank to keep current)</span>}
              </label>
              <textarea
                value={form.oauth_credentials_blob}
                onChange={e => set({ oauth_credentials_blob: e.target.value })}
                rows={6}
                placeholder={flavor.pasteFallbackPlaceholder}
                className="w-full px-3 py-2 text-xs font-mono bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-300 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                required={!editing}
              />
              <div className="text-[11px] text-gray-500 dark:text-gray-400">
                <button
                  type="button"
                  className="underline hover:text-gray-700 dark:hover:text-gray-200"
                  onClick={() => setShowPasteFallback(false)}
                >
                  ← Back to browser sign-in
                </button>
              </div>
            </div>
          )}
        </div>
      ) : form.provider_type === 'grok-web' ? (
        // v3.2.0+: grok.com web-subscription provider. Two paths:
        //
        //   Bridge mode (recommended, v3.2.1):
        //     - The llm-proxy2-grok-bridge sidecar holds the live grok.com
        //       session (Playwright + Chromium + persistent cookies)
        //     - Operator clicks "Connect Grok" once → opens noVNC tab →
        //       signs in via Google OAuth → bridge keeps cookies fresh
        //     - Set bridge_url + conversation_id; bridge does the rest
        //
        //   Manual mode (legacy v3.2.0):
        //     - Paste cookie_header + statsig-id + conversation_id from a
        //       captured cURL. Re-paste every few hours when CF cookies
        //       rotate. No bridge container needed.
        <GrokWebProviderFields form={form} set={set} editing={editing} />
      ) : (
        <>
          <Input
            label={editing ? 'API Key (leave blank to keep current)' : 'API Key'}
            type="password"
            value={form.api_key}
            onChange={e => set({ api_key: e.target.value })}
            required={!editing}
          />
          <Input
            label="Base URL (optional)"
            value={form.base_url}
            onChange={e => set({ base_url: e.target.value })}
            placeholder="https://api.example.com"
          />
        </>
      )}

      <Input
        label="Default Model"
        value={form.default_model}
        onChange={e => set({ default_model: e.target.value })}
        placeholder={
          isOAuth ? 'claude-sonnet-4-6' :
          form.provider_type === 'grok-web' ? 'grok-3' :
          form.provider_type === 'openrouter' ? 'openai/gpt-4o' :
          'e.g. gpt-4o'
        }
      />
      <Input
        label="Priority (lower = preferred)"
        tooltip="Routing order. Lower numbers are tried first. Use 1 for your free / subscription provider (Grok-Web, Claude-OAuth, Codex-OAuth) so it absorbs traffic before paid providers. Use 5+ for paid fallbacks (OpenRouter, direct OpenAI). Ties are broken by capability score from LMRH."
        type="number"
        value={String(form.priority)}
        onChange={e => set({ priority: Number(e.target.value) })}
      />
      <Input
        label="Timeout (seconds)"
        tooltip="Hard upper bound on a single request to this provider. Beyond this, the request fails over to the next priority. 60s is a sensible default; raise for slow reasoning models, lower for known-fast providers. Doesn't apply to keep-alive probes (those use a separate 15s budget)."
        type="number"
        value={String(form.timeout_sec)}
        onChange={e => set({ timeout_sec: Number(e.target.value) })}
      />
      <Input
        label="Hold-down after failure (seconds, blank = global 120s)"
        tooltip="When the circuit breaker opens for this provider (too many failures), how long to skip it before re-trying. 120s default = enough for a transient outage to clear; 21600s (6h) for billing failures so we don't keep retrying a depleted account. Leave blank to use the global default."
        type="number"
        value={form.hold_down_sec == null ? '' : String(form.hold_down_sec)}
        onChange={e => set({ hold_down_sec: e.target.value === '' ? null : Number(e.target.value) })}
        placeholder="120"
      />
      <Input
        label="Failure threshold before trip (blank = global 3)"
        tooltip="How many consecutive failures before the circuit breaker opens (provider gets removed from the candidate pool until hold-down expires). 3 default = balances ‘don't over-react to one blip’ vs ‘don't keep hammering a dead endpoint’. Lower for fragile providers, higher for noisy ones."
        type="number"
        value={form.failure_threshold == null ? '' : String(form.failure_threshold)}
        onChange={e => set({ failure_threshold: e.target.value === '' ? null : Number(e.target.value) })}
        placeholder="3"
      />
      <div className="flex items-center gap-3 mt-5">
        <label className="text-sm text-gray-700 dark:text-gray-300">Exclude from tool requests</label>
        <input
          type="checkbox"
          checked={!!form.exclude_from_tool_requests}
          onChange={e => set({ exclude_from_tool_requests: e.target.checked })}
          className="h-4 w-4 accent-indigo-600"
        />
      </div>

      {/* v3.7.5 — Anthropic Console billing scrape + auto-rotation
          surface, only rendered for claude-oauth providers in edit
          mode (need a stored provider with id + cookies state). */}
      {editing && provider && form.provider_type === 'claude-oauth' && (
        <AnthropicBillingPanel
          provider={provider}
          onUpdated={onProviderUpdated}
        />
      )}

      {/* v3.7.27 (#245) — ChatGPT / Codex Cloud usage scrape surface,
          same shape as the Anthropic panel above but operator supplies
          the analytics endpoint URL since it isn't documented. */}
      {editing && provider && form.provider_type === 'ChatGPT-oauth-plan' && (
        <CodexBillingPanel
          provider={provider}
          onUpdated={onProviderUpdated}
        />
      )}

      {/* v4.4 M-5 (Path A) — per-node bridge auth status panel.
          Renders only when the provider's extra_config has
          `node_local_session=true` set; today only configured for
          grok-web when running the v4.4 per-node-bridge topology.
          For all other providers the component returns null and
          adds no UI footprint. */}
      {editing && provider && (
        <NodeBridgeStatusPanel provider={provider} />
      )}

      {/* v3.0.64: Per-provider usage-based rotation (Phase 2 — config UI).
          v3.7.25 (#257): for claude-oauth providers, the External Usage
          panel above replaced this section's function in v3.7.0. The
          legacy disclosure + collapsed-fields UI is removed — the
          section now renders only for non-claude-oauth providers
          (e.g. codex-oauth, where no External Usage scrape exists yet).
          The underlying ``usage_*`` columns remain in the DB for audit
          and may still be set via the API; this just hides the now-
          confusing knobs from the claude-oauth detail form. */}
      {form.provider_type !== 'claude-oauth' && (
      <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
        <div className="flex items-center justify-between mb-3">
          <div>
            <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Usage-based rotation
            </h4>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
              Track rolling token usage so the proxy can rotate priority between same-type
              providers when one gets too far ahead. Suitable for OAuth subscriptions
              with session + weekly quotas.
            </p>
          </div>
          <label className="flex items-center gap-2 shrink-0">
            <input
              type="checkbox"
              checked={!!form.usage_tracking_enabled}
              onChange={e => set({ usage_tracking_enabled: e.target.checked })}
              className="h-4 w-4 accent-indigo-600"
            />
            <span className="text-sm text-gray-700 dark:text-gray-300">Enable tracking</span>
          </label>
        </div>

        {form.usage_tracking_enabled && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <Input
              label="Session window (seconds)"
              tooltip="How long the session bucket runs before resetting. Claude.ai's documented session window is 5 hours = 18000s. Codex CLI runs on a similar 5h cadence. Adjust if your subscription tier uses a different rolling window."
              type="number"
              value={form.usage_session_window_sec == null ? '' : String(form.usage_session_window_sec)}
              onChange={e => set({ usage_session_window_sec: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="18000 (5h, claude.ai default)"
            />
            <Input
              label="Session token limit"
              tooltip="Operator-configured ceiling on tokens per session window — NOT the actual upstream provider's allowance. Set conservatively to get an early warning before you hit the real provider limit. Pre-fix the proxy showed Devin-Anthropic-Max-VG at 256% because this was set to 20M while Anthropic's actual Pro Max session allowance is much higher. The dashboard surfaces 'over limit' when usage exceeds THIS value, not when the upstream rejects. Set blank or 0 to disable session-window enforcement."
              type="number"
              value={form.usage_session_limit_tokens == null ? '' : String(form.usage_session_limit_tokens)}
              onChange={e => set({ usage_session_limit_tokens: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="2000000"
            />
            <div>
              <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Weekly reset day
              </label>
              <select
                value={form.usage_weekly_reset_dow == null ? '' : String(form.usage_weekly_reset_dow)}
                onChange={e => set({ usage_weekly_reset_dow: e.target.value === '' ? null : Number(e.target.value) })}
                className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-1.5 text-sm"
              >
                <option value="">— not set —</option>
                <option value="0">Monday</option>
                <option value="1">Tuesday</option>
                <option value="2">Wednesday</option>
                <option value="3">Thursday</option>
                <option value="4">Friday</option>
                <option value="5">Saturday</option>
                <option value="6">Sunday (claude.ai default)</option>
              </select>
            </div>
            <Input
              label="Weekly reset hour (0-23 local)"
              tooltip="Hour-of-day when the weekly bucket resets (operator's local timezone). Claude.ai's documented reset is 4pm Sunday. This determines when the dashboard's 'over limit' badge clears."
              type="number"
              value={form.usage_weekly_reset_hour == null ? '' : String(form.usage_weekly_reset_hour)}
              onChange={e => set({ usage_weekly_reset_hour: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="16 (4pm, claude.ai default)"
            />
            <Input
              label="Weekly token limit"
              tooltip="Operator-configured ceiling on tokens per weekly window — NOT the actual upstream provider's allowance. Anthropic Pro Max documents a weekly limit but doesn't publish a number; OAuth tier appears to be ~10× the session limit empirically. Set conservatively (e.g. 5x your session limit) to get an early warning before hitting the real upstream limit, OR set high (50M+) to use this as a sanity-check ceiling only. The dashboard 'over limit' banner triggers at >100% of THIS value, not when the upstream rejects. Set blank or 0 to disable weekly-window enforcement."
              type="number"
              value={form.usage_weekly_limit_tokens == null ? '' : String(form.usage_weekly_limit_tokens)}
              onChange={e => set({ usage_weekly_limit_tokens: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="20000000"
            />
            <Input
              label="Rotation threshold (% gap)"
              type="number"
              value={form.usage_rotation_threshold_pct == null ? '' : String(form.usage_rotation_threshold_pct)}
              onChange={e => set({ usage_rotation_threshold_pct: e.target.value === '' ? null : Number(e.target.value) })}
              placeholder="30 (rotate when usage % gap exceeds this)"
            />
          </div>
        )}
      </div>
      )}
    </div>
  )
}
