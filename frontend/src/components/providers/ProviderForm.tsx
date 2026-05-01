import { useState } from 'react'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { providersApi } from '@/api'
import { useToast } from '@/components/ui/Toast'
import type { ProviderType, Provider } from '@/types'

const PROVIDER_TYPES: ProviderType[] = [
  'anthropic', 'openai', 'google', 'vertex', 'grok', 'ollama', 'compatible',
  'claude-oauth',  // v2.7.0 — Claude Pro Max subscription via pasted credentials
  'codex-oauth',   // v3.0.15 — OpenAI Codex CLI / ChatGPT subscription
  'cohere',        // v3.0.23 — Cohere embeddings (and rerank/chat)
]

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
  'codex-oauth': {
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
  }
}

interface Props {
  form: ProviderFormState
  onChange: (f: ProviderFormState) => void
  editing: boolean
}

export function ProviderForm({ form, onChange, editing }: Props) {
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
          {PROVIDER_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
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
                <li>Open the URL in a tab where you're signed in to your {form.provider_type === 'codex-oauth' ? 'ChatGPT' : 'Claude'} account and approve access.</li>
                <li>You'll be redirected to <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">{flavor.callbackHostHint}</code>.</li>
                <li>Copy that code (or the full URL from your address bar) and paste it below. We'll trade it for a token automatically.</li>
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

              {form.oauth_authorize_url && (
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
        placeholder={isOAuth ? 'claude-sonnet-4-6' : 'e.g. gpt-4o'}
      />
      <Input
        label="Priority (lower = preferred)"
        type="number"
        value={String(form.priority)}
        onChange={e => set({ priority: Number(e.target.value) })}
      />
      <Input
        label="Timeout (seconds)"
        type="number"
        value={String(form.timeout_sec)}
        onChange={e => set({ timeout_sec: Number(e.target.value) })}
      />
      <Input
        label="Hold-down after failure (seconds, blank = global 120s)"
        type="number"
        value={form.hold_down_sec == null ? '' : String(form.hold_down_sec)}
        onChange={e => set({ hold_down_sec: e.target.value === '' ? null : Number(e.target.value) })}
        placeholder="120"
      />
      <Input
        label="Failure threshold before trip (blank = global 3)"
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
    </div>
  )
}
