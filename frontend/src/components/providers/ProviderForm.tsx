import { Input } from '@/components/ui/Input'
import type { ProviderType, Provider } from '@/types'

const PROVIDER_TYPES: ProviderType[] = [
  'anthropic', 'openai', 'google', 'vertex', 'grok', 'ollama', 'compatible',
  'claude-oauth',  // v2.7.0 — Claude Pro Max subscription via pasted credentials
]

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
  // v2.7.0: only used when provider_type === 'claude-oauth'
  oauth_credentials_blob: string
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
  }
}

interface Props {
  form: ProviderFormState
  onChange: (f: ProviderFormState) => void
  editing: boolean
}

export function ProviderForm({ form, onChange, editing }: Props) {
  const set = (patch: Partial<ProviderFormState>) => onChange({ ...form, ...patch })
  const isOAuth = form.provider_type === 'claude-oauth'

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

      {/* v2.7.0: claude-oauth gets a credential-paste section instead of API Key + Base URL. */}
      {isOAuth ? (
        <div className="md:col-span-2 space-y-3 rounded-lg border border-indigo-200 dark:border-indigo-800 bg-indigo-50/40 dark:bg-indigo-950/30 p-4">
          <div className="text-sm font-medium text-gray-800 dark:text-gray-100">
            Claude Pro Max credentials
          </div>
          <ol className="list-decimal list-inside text-xs text-gray-600 dark:text-gray-300 space-y-1">
            <li>On any machine with Claude Code installed, run <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">claude login</code></li>
            <li>Run <code className="px-1 font-mono bg-gray-100 dark:bg-gray-800 rounded">cat ~/.claude/credentials.json</code> (or paste your <code className="font-mono">sk-ant-oat…</code> token directly)</li>
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
            placeholder={`{\n  "access_token": "sk-ant-oat01-…",\n  "refresh_token": "…",\n  "expires_at": "2026-05-24T00:00:00Z"\n}\n\n— or just —\n\nsk-ant-oat01-…`}
            className="w-full px-3 py-2 text-xs font-mono bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-300 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
            required={!editing}
          />
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
