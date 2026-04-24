import { api } from './client'
import type {
  AuthUser, Provider, ProviderFormData, ModelCapability, TestResult, ScannedModel,
  ApiKey, User, ActivityEvent, MetricsSummary, MetricBucket,
  ClusterStatus, HealthStatus, ExternalStatus,
} from '@/types'

// ── Auth ──────────────────────────────────────────────────────────────────────
export const authApi = {
  login:  (username: string, password: string) =>
    api.post<AuthUser>('/api/auth/login', { username, password }),
  logout: () => api.post<void>('/api/auth/logout'),
  me:     () => api.get<AuthUser>('/api/auth/me'),
}

// ── Providers ─────────────────────────────────────────────────────────────────
export const providersApi = {
  list:       ()                         => api.get<Provider[]>('/api/providers'),
  get:        (id: string)               => api.get<Provider>(`/api/providers/${id}`),
  create:     (data: ProviderFormData)   => api.post<Provider>('/api/providers', data),
  update:     (id: string, data: ProviderFormData) =>
    api.put<Provider>(`/api/providers/${id}`, data),
  delete:     (id: string)               => api.delete<void>(`/api/providers/${id}`),
  toggle:     (id: string)               => api.patch<{ enabled: boolean }>(`/api/providers/${id}/toggle`),
  test:       (id: string)               => api.post<TestResult>(`/api/providers/${id}/test`),
  scanModels: (id: string)               => api.post<{ scanned: number; models: ScannedModel[]; warning?: string }>(`/api/providers/${id}/scan-models`),
  capabilities: (id: string)             => api.get<ModelCapability[]>(`/api/providers/${id}/model-capabilities`),
  updateCapability: (id: string, modelId: string, data: Partial<ModelCapability>) =>
    api.put<ModelCapability>(`/api/providers/${id}/model-capabilities/${encodeURIComponent(modelId)}`, data),
  inferCapabilities: (id: string)        => api.post<{ updated: number }>(`/api/providers/${id}/model-capabilities/infer`),
  // v2.7.1: browser-initiated Claude Pro Max OAuth flow
  oauthAuthorize: () =>
    api.post<{ state: string; authorize_url: string }>('/api/providers/claude-oauth/authorize', {}),
  oauthExchange: (data: {
    state: string
    callback: string
    name: string
    default_model?: string
    base_url?: string
    priority: number
    enabled: boolean
    timeout_sec: number
    exclude_from_tool_requests: boolean
    hold_down_sec: number | null
    failure_threshold: number | null
    extra_config: Record<string, unknown>
  }) => api.post<Provider>('/api/providers/claude-oauth/exchange', data),
}

// ── API Keys ──────────────────────────────────────────────────────────────────
export const keysApi = {
  list:   ()                           => api.get<ApiKey[]>('/api/keys'),
  create: (data: { name?: string; key_type: string; rate_limit_rpm?: number }) =>
    api.post<ApiKey & { raw_key: string }>('/api/keys', data),
  update: (id: string, data: Partial<ApiKey>) => api.patch<ApiKey>(`/api/keys/${id}`, data),
  delete: (id: string)                 => api.delete<void>(`/api/keys/${id}`),
  bulkDelete: (ids: string[])          => api.post<{ deleted: number; requested: number }>('/api/keys/bulk-delete', { ids }),
  reveal: (id: string)                 => api.get<{ id: string; raw_key: string }>(`/api/keys/${id}/reveal`),
}

// ── Users ─────────────────────────────────────────────────────────────────────
export const usersApi = {
  list:   ()                             => api.get<User[]>('/api/users'),
  create: (data: { username: string; password: string; role: string }) =>
    api.post<User>('/api/users', data),
  update: (id: string, data: { password?: string; role?: string }) =>
    api.patch<User>(`/api/users/${id}`, data),
  delete: (id: string)                   => api.delete<void>(`/api/users/${id}`),
}

// ── Monitoring ────────────────────────────────────────────────────────────────
export const monitoringApi = {
  activity:   (limit = 100) => api.get<ActivityEvent[]>(`/api/monitoring/activity?limit=${limit}`),
  metrics:    (hours = 24)  => api.get<MetricsSummary>(`/api/monitoring/metrics?hours=${hours}`),
  providerMetrics: (id: string, hours = 24) =>
    api.get<{ provider_id: string; hours: number; buckets: MetricBucket[] }>(
      `/api/monitoring/metrics/${id}?hours=${hours}`
    ),
  statusPages: ()           => api.get<ExternalStatus>('/api/monitoring/status-pages'),
}

// ── Settings ──────────────────────────────────────────────────────────────────
export type SettingSchemaItem = {
  key: string
  type: 'bool' | 'int' | 'float' | 'str'
  label: string
  group: string
  help?: string | null
  secret: boolean
  default: unknown
}

export const settingsApi = {
  get:    ()                             => api.get<Record<string, unknown>>('/api/settings'),
  schema: ()                             => api.get<SettingSchemaItem[]>('/api/settings/schema'),
  save:   (data: Record<string, unknown>) => api.put<{ saved: string[] }>('/api/settings', data),
  clusterDiff: ()                        => api.get<{
    cluster_enabled: boolean
    all_synced?: boolean
    local?: { node_id: string; settings: Record<string, unknown> }
    peers?: Array<{
      id: string; name: string; status: string
      settings: Record<string, unknown> | null
      diffs: string[]
      error?: string
    }>
  }>('/api/settings/cluster-diff'),
}

// ── OAuth capture (v2.5.0) ────────────────────────────────────────────────────

export type OAuthCapturePreset = {
  key: string
  label: string
  cli_hint: string
  primary_upstream: string
  extra_upstreams: string[]
  env_var_names: string[]
  setup_hint: string
}

export type OAuthCaptureProfile = {
  name: string
  preset: string | null
  upstream_urls: string[]
  enabled: boolean
  notes: string | null
  created_at: string | null
  has_secret: boolean
  secret?: string  // only returned on create / rotate / reveal
}

export type OAuthCaptureLogEntry = {
  id: number
  profile_name: string | null
  method: string
  path: string
  upstream_url: string
  resp_status: number | null
  latency_ms: number
  error: string | null
  req_body_preview: string
  resp_body_preview: string
  created_at: string | null
}

export const oauthCaptureApi = {
  listPresets:  () => api.get<OAuthCapturePreset[]>('/api/oauth-capture/_presets'),
  listProfiles: () => api.get<OAuthCaptureProfile[]>('/api/oauth-capture/_profiles'),
  createProfile: (body: { name: string; preset?: string; upstream_urls?: string[]; notes?: string; enabled?: boolean }) =>
    api.post<OAuthCaptureProfile>('/api/oauth-capture/_profiles', body),
  updateProfile: (name: string, body: { upstream_urls?: string[]; enabled?: boolean; notes?: string | null; rotate_secret?: boolean }) =>
    api.patch<OAuthCaptureProfile>(`/api/oauth-capture/_profiles/${encodeURIComponent(name)}`, body),
  revealSecret: (name: string) =>
    api.get<{ name: string; secret: string }>(`/api/oauth-capture/_profiles/${encodeURIComponent(name)}/secret`),
  deleteProfile: (name: string) =>
    api.delete<{ ok: boolean }>(`/api/oauth-capture/_profiles/${encodeURIComponent(name)}`),
  listLog: (profile?: string, limit = 100) =>
    api.get<OAuthCaptureLogEntry[]>(
      `/api/oauth-capture/_log?limit=${limit}${profile ? `&profile=${encodeURIComponent(profile)}` : ''}`
    ),
  clearLog: (profile?: string) =>
    api.delete<{ deleted: number; profile: string }>(
      `/api/oauth-capture/_log${profile ? `?profile=${encodeURIComponent(profile)}` : ''}`
    ),
  // SSE stream URL — components open it with native EventSource
  streamUrl: (profile: string) =>
    `/api/oauth-capture/_log/stream/${encodeURIComponent(profile)}`,
  exportUrl: (profile: string) =>
    `/api/oauth-capture/_log/export/${encodeURIComponent(profile)}`,
}


// ── Cluster ───────────────────────────────────────────────────────────────────
export const clusterApi = {
  status:  ()                              => api.get<ClusterStatus>('/cluster/status'),
  health:  ()                              => api.get<HealthStatus>('/health'),
  sync:    ()                              => api.post<void>('/cluster/sync'),
  cbReset: (providerId: string)            => api.post<void>(`/cluster/circuit-breaker/${providerId}/reset`),
  cbOpen:  (providerId: string)            => api.post<void>(`/cluster/circuit-breaker/${providerId}/open`),
  forceCircuitBreaker: (providerId: string, action: 'open' | 'close') =>
    action === 'open'
      ? api.post<void>(`/cluster/circuit-breaker/${providerId}/open`)
      : api.post<void>(`/cluster/circuit-breaker/${providerId}/reset`),
}
