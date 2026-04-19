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
  scanModels: (id: string)               => api.post<{ scanned: number; models: ScannedModel[] }>(`/api/providers/${id}/scan-models`),
  capabilities: (id: string)             => api.get<ModelCapability[]>(`/api/providers/${id}/model-capabilities`),
  updateCapability: (id: string, modelId: string, data: Partial<ModelCapability>) =>
    api.put<ModelCapability>(`/api/providers/${id}/model-capabilities/${encodeURIComponent(modelId)}`, data),
  inferCapabilities: (id: string)        => api.post<{ updated: number }>(`/api/providers/${id}/model-capabilities/infer`),
}

// ── API Keys ──────────────────────────────────────────────────────────────────
export const keysApi = {
  list:   ()                           => api.get<ApiKey[]>('/api/keys'),
  create: (data: { name?: string; key_type: string; rate_limit_rpm?: number }) =>
    api.post<ApiKey & { raw_key: string }>('/api/keys', data),
  update: (id: string, data: Partial<ApiKey>) => api.patch<ApiKey>(`/api/keys/${id}`, data),
  delete: (id: string)                 => api.delete<void>(`/api/keys/${id}`),
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
