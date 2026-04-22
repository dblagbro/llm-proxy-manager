import { Input } from '@/components/ui/Input'
import type { ProviderType, Provider } from '@/types'

const PROVIDER_TYPES: ProviderType[] = ['anthropic', 'openai', 'google', 'vertex', 'grok', 'ollama', 'compatible']

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
  }
}

interface Props {
  form: ProviderFormState
  onChange: (f: ProviderFormState) => void
  editing: boolean
}

export function ProviderForm({ form, onChange, editing }: Props) {
  const set = (patch: Partial<ProviderFormState>) => onChange({ ...form, ...patch })

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
          className="px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
        >
          {PROVIDER_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
        </select>
      </div>
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
      <Input
        label="Default Model"
        value={form.default_model}
        onChange={e => set({ default_model: e.target.value })}
        placeholder="e.g. gpt-4o"
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
