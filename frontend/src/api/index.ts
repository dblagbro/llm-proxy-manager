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
  // v2.7.7: re-auth an existing claude-oauth provider in-place
  oauthRotate: (id: string, data: { state: string; callback: string }) =>
    api.post<Provider>(`/api/providers/${id}/oauth-rotate`, data),
  // v2.8.0: clear the BUG-002 "needs re-auth" flag (admin asserts they fixed it)
  clearAuthFailure: (id: string) =>
    api.post<{ ok: boolean }>(`/api/providers/${id}/clear-auth-failure`, {}),
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
export interface ActivityQuery {
  limit?: number
  before_id?: number | null
  provider_id?: string | null
  severity?: string | null
  search?: string | null
}

function _activityQs(q: ActivityQuery): string {
  const sp = new URLSearchParams()
  if (q.limit != null)       sp.set('limit', String(q.limit))
  if (q.before_id != null)   sp.set('before_id', String(q.before_id))
  if (q.provider_id)         sp.set('provider_id', q.provider_id)
  if (q.severity)            sp.set('severity', q.severity)
  if (q.search)              sp.set('search', q.search)
  return sp.toString()
}

export const monitoringApi = {
  activity:        (q: ActivityQuery = {}) =>
    api.get<ActivityEvent[]>(`/api/monitoring/activity?${_activityQs({ limit: 200, ...q })}`),
  activityCount:   (q: Omit<ActivityQuery, 'limit' | 'before_id'> = {}) =>
    api.get<{ total: number }>(`/api/monitoring/activity/count?${_activityQs(q)}`),
  metrics:         (hours = 24) => api.get<MetricsSummary>(`/api/monitoring/metrics?hours=${hours}`),
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

// ── OAuth capture (v2.5.0) ──────────────────────────────────────────────────
// Removed from the main UI in v2.8.1 — Claude Pro Max OAuth setup now lives
// in the Providers page (claude-oauth provider type). Backend
// /api/oauth-capture/* endpoints remain for ad-hoc reverse-engineering of
// future vendor CLIs via curl; they're admin-only and not user-facing.


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
