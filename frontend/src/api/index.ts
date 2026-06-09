import { api } from './client'
import { getBasePath } from '@/lib/basePath'
import type {
  AuthUser, Provider, ProviderFormData, ModelCapability, TestResult, ScannedModel,
  ApiKey, User, ActivityEvent, MetricsSummary, MetricBucket,
  MetricsByNodeResponse,
  ClusterStatus, HealthStatus, ExternalStatus, CacheStats,
  NodeAuthState,
  MyComplianceResponse, ComplianceEvent, CompliancePolicyChange,
  ClusterComplianceReadiness,
} from '@/types'

// ── Auth ──────────────────────────────────────────────────────────────────────
export const authApi = {
  login:  (username: string, password: string) =>
    api.post<AuthUser>('/api/auth/login', { username, password }),
  logout: () => api.post<void>('/api/auth/logout'),
  me:     () => api.get<AuthUser>('/api/auth/me'),
  // Unauthenticated-safe boot probe — always 200, so a logged-out page load
  // does not log a 401 console error (BUG-020). Used by the auth bootstrap.
  session: () => api.get<Partial<AuthUser> & { authenticated: boolean }>('/api/auth/session'),
  setPreferences: (prefs: { timezone?: string | null; time_format?: '12h' | '24h' | '' | null }) =>
    api.patch<AuthUser>('/api/auth/preferences', prefs),
}

// ── Providers ─────────────────────────────────────────────────────────────────
export const providersApi = {
  list:       ()                         => api.get<Provider[]>('/api/providers'),
  rollingStats: ()                       => api.get<Array<{
    provider_id: string
    provider_name: string
    windows: {
      '1h':  { requests: number; successes: number; success_pct: number | null }
      '24h': { requests: number; successes: number; success_pct: number | null }
      '7d':  { requests: number; successes: number; success_pct: number | null }
      '30d': { requests: number; successes: number; success_pct: number | null }
    }
  }>>('/api/providers/rolling-stats'),
  get:        (id: string)               => api.get<Provider>(`/api/providers/${id}`),
  create:     (data: ProviderFormData)   => api.post<Provider>('/api/providers', data),
  update:     (id: string, data: ProviderFormData) =>
    api.put<Provider>(`/api/providers/${id}`, data),
  delete:     (id: string)               => api.delete<void>(`/api/providers/${id}`),
  toggle:     (id: string)               => api.patch<{ enabled: boolean }>(`/api/providers/${id}/toggle`),
  test:       (id: string)               => api.post<TestResult>(`/api/providers/${id}/test`),
  scanModels: (id: string)               => api.post<{ scanned: number; models: ScannedModel[]; warning?: string }>(`/api/providers/${id}/scan-models`),
  capabilities: (id: string)             => api.get<ModelCapability[]>(`/api/providers/${id}/model-capabilities`),
  // v4.4 M-5 (Path A): per-node bridge auth state for this provider.
  // Empty array when no rows exist (typical for non-grok-web providers
  // and for grok-web providers before the first probe has run).
  nodeAuthStates: (id: string)           => api.get<NodeAuthState[]>(`/api/providers/${id}/node-auth-states`),
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
  // v3.0.15: browser-initiated Codex CLI / ChatGPT-subscription OAuth flow
  codexOauthAuthorize: () =>
    api.post<{ state: string; authorize_url: string }>('/api/providers/codex-oauth/authorize', {}),
  codexOauthExchange: (data: {
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
  }) => api.post<Provider>('/api/providers/codex-oauth/exchange', data),
  codexOauthRotate: (id: string, data: { state: string; callback: string }) =>
    api.post<Provider>(`/api/providers/${id}/codex-oauth-rotate`, data),
  // v4.4.31: Cursor Pro/Business subscription onboarding via the
  // cursor-bridge sidecar. Same shape as the codex/claude flows so the
  // form's OAUTH_FLAVORS lookup can share the rendering path.
  cursorOauthAuthorize: () =>
    api.post<{ state: string; authorize_url: string }>('/api/providers/cursor-oauth/authorize', {}),
  cursorOauthExchange: (data: {
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
  }) => api.post<Provider>('/api/providers/cursor-oauth/exchange', data),
  cursorOauthRotate: (id: string, data: { state: string; callback: string }) =>
    api.post<Provider>(`/api/providers/${id}/cursor-oauth-rotate`, data),
  // v4.4.33: polished poll-based onboarding. The backend long-polls Cursor's
  // IDE auth endpoint for up to ~5 min after the operator's browser login.
  // No DevTools cookie copy. ``state`` ties to the PKCE pair generated by
  // ``cursorOauthAuthorize``.
  cursorOauthPoll: (data: {
    state: string
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
  }) => api.post<Provider>('/api/providers/cursor-oauth/poll', data),
  cursorOauthPollRotate: (id: string, data: { state: string }) =>
    api.post<Provider>(`/api/providers/${id}/cursor-oauth-poll-rotate`, data),
  // v2.8.0: clear the BUG-002 "needs re-auth" flag (admin asserts they fixed it)
  clearAuthFailure: (id: string) =>
    api.post<{ ok: boolean }>(`/api/providers/${id}/clear-auth-failure`, {}),
  // v3.7.5 — Anthropic Console billing scrape admin endpoints
  setBillingCredentials: (id: string, data: { org_uuid: string; cookies: string }) =>
    api.post<{ ok: boolean; provider_id: string; org_uuid: string; cookie_count: number; captured_at: number }>(
      `/api/providers/${id}/anthropic-billing-credentials`, data,
    ),
  refreshBillingNow: (id: string) =>
    api.post<{
      ok: boolean
      auth_state: string
      http_status: number | null
      snapshot_id: number | null
      seven_day_utilization: number | null
      five_hour_utilization: number | null
      rotation_decision: Record<string, unknown>
    }>(`/api/providers/${id}/anthropic-billing-refresh`, {}),
  listSnapshots: (id: string, limit: number = 20) =>
    api.get<Array<{
      id: number
      captured_at: string | null
      source: string | null
      http_status: number | null
      auth_state: string | null
      error: string | null
      five_hour_utilization: number | null
      five_hour_resets_at: string | null
      seven_day_utilization: number | null
      seven_day_resets_at: string | null
      seven_day_sonnet_utilization: number | null
      seven_day_opus_utilization: number | null
      extra_usage_is_enabled: boolean | null
      extra_usage_monthly_limit: number | null
      extra_usage_used_credits: number | null
      extra_usage_utilization: number | null
      extra_usage_currency: string | null
    }>>(`/api/providers/${id}/external-usage?limit=${limit}`),
  evaluateRotationRulesNow: () =>
    api.post<{ evaluated: number; decisions: Array<Record<string, unknown>> }>(
      '/api/providers/_evaluate-rotation-rules', {},
    ),
  // v3.9.19 — bulk-refresh usage stats for every credentialed claude-oauth
  // provider; re-evaluates rotation rules so accounts return to service
  // when usage drops (e.g. after Anthropic resets counters early).
  refreshAllBilling: () =>
    api.post<{
      providers: number
      scraped_ok: number
      returned_to_service: number
      results: Array<{
        provider_id: string
        provider_name: string
        ok: boolean
        error?: string
        auth_state?: string | null
        seven_day_utilization?: number | null
        five_hour_utilization?: number | null
        rotation_decision?: string | null
        returned_to_service?: boolean
      }>
    }>('/api/providers/_refresh-all-anthropic-billing', {}),
  // v3.7.27 (#245) — ChatGPT Plus / Codex Cloud billing scrape admin endpoints
  setCodexBillingCredentials: (id: string, data: { endpoint_url: string; cookies: string }) =>
    api.post<{ ok: boolean; provider_id: string; endpoint_url: string; cookie_count: number; captured_at: number }>(
      `/api/providers/${id}/codex-billing-credentials`, data,
    ),
  refreshCodexBillingNow: (id: string) =>
    api.post<{
      ok: boolean
      auth_state: string
      http_status: number | null
      snapshot_id: number | null
    }>(`/api/providers/${id}/codex-billing-refresh`, {}),
  // v5.3.5 — Cursor billing parity (mirrors codex/anthropic above)
  refreshCursorBillingNow: (id: string) =>
    api.post<{
      ok: boolean
      auth_state: string
      http_status: number | null
      snapshot_id: number | null
      seven_day_utilization?: number | null
      five_hour_utilization?: number | null
    }>(`/api/providers/${id}/cursor-billing-refresh`, {}),
  refreshAllCursorBilling: () =>
    api.post<{
      providers: number
      scraped_ok: number
      returned_to_service: number
      results: Array<{
        provider_id: string
        provider_name: string
        ok: boolean
        error?: string
        auth_state?: string | null
        seven_day_utilization?: number | null
        rotation_decision?: string | null
      }>
    }>('/api/providers/_refresh-all-cursor-billing', {}),
  // v5.3.5 — Codex bulk equivalent of refreshAllBilling (Anthropic)
  refreshAllCodexBilling: () =>
    api.post<{
      providers: number
      scraped_ok: number
      returned_to_service: number
      results: Array<{
        provider_id: string
        provider_name: string
        ok: boolean
        error?: string
        auth_state?: string | null
        seven_day_utilization?: number | null
        rotation_decision?: string | null
      }>
    }>('/api/providers/_refresh-all-codex-billing', {}),
  // v3.7.28 (#252 phase 1) — bulk-release all manual override locks
  // (the "Release all to AI control" banner button)
  releaseManualOverrides: () =>
    api.post<{ released: number }>('/api/providers/_release-manual-overrides', {}),
}

// ── API Keys ──────────────────────────────────────────────────────────────────
export const keysApi = {
  list:    ()                          => api.get<ApiKey[]>('/api/keys'),
  // v5.1.0 / Batch B1 — separate method for the admin Trash tab (kept
  // distinct so the default ``list`` keeps a clean useQuery signature).
  listAll: ()                          => api.get<ApiKey[]>('/api/keys?include_deleted=true'),
  create: (data: {
    name?: string
    key_type: string
    rate_limit_rpm?: number
    blocked_companies?: string[] | null
    allowed_paths?: string[] | null
    // v5.2.1 / Batch V2 — fine-grained policy on create.
    allowed_companies?: string[] | null
    blocked_models?: string[] | null
    allowed_models?: string[] | null
    debug_echo_enabled?: boolean
    reason?: string
    // v5.1.0 / Batch B2 — clone caps + compliance from an existing key.
    copy_from_id?: string
  }) =>
    api.post<ApiKey & { raw_key: string }>('/api/keys', data),
  // v5.1.0 / Batch B1 — restore tombstoned key within the
  // api_key_tombstone_retention_days window.
  restore: (id: string) =>
    api.post<{ ok: boolean; id: string; restored_at: number }>(
      `/api/keys/${id}/restore`, {},
    ),
  // v5.0.0 — PATCH accepts the new compliance fields and a reason string
  // (required when blocked_companies or allowed_paths change, per
  // decision 6). Reason is logged into compliance_policy_changes.
  update: (id: string, data: Partial<ApiKey> & { reason?: string }) =>
    api.patch<ApiKey>(`/api/keys/${id}`, data),
  delete: (id: string)                 => api.delete<void>(`/api/keys/${id}`),
  bulkDelete: (ids: string[])          => api.post<{ deleted: number; requested: number }>('/api/keys/bulk-delete', { ids }),
  reveal: (id: string)                 => api.get<{ id: string; raw_key: string }>(`/api/keys/${id}/reveal`),
  // v3.10.8 — the effective model catalog for one key (models on every
  // provider the key can route to). Powers the "Copy models" action.
  models: (id: string) =>
    api.get<{ key_id: string; key_name: string; count: number; models: string[] }>(
      `/api/keys/${id}/models`,
    ),
}

// ── Compliance (v5.0.0) ──────────────────────────────────────────────────────
export interface ComplianceEventsQuery {
  api_key_id?: string | null
  event_type?: string | null
  start?: string | null
  end?: string | null
  blocked_company?: string | null
  limit?: number
}

function _complianceQs(q: ComplianceEventsQuery): string {
  const sp = new URLSearchParams()
  if (q.api_key_id)      sp.set('api_key_id', q.api_key_id)
  if (q.event_type)      sp.set('event_type', q.event_type)
  if (q.start)           sp.set('start', q.start)
  if (q.end)             sp.set('end', q.end)
  if (q.blocked_company) sp.set('blocked_company', q.blocked_company)
  if (q.limit != null)   sp.set('limit', String(q.limit))
  return sp.toString()
}

export const complianceApi = {
  me: () => api.get<MyComplianceResponse>('/api/me/compliance'),
  events: (q: ComplianceEventsQuery = {}) =>
    api.get<{ events: ComplianceEvent[] }>(
      `/api/admin/compliance-events?${_complianceQs({ limit: 200, ...q })}`,
    ),
  // CSV export: returns the URL the browser should hit directly (so
  // download triggers natively rather than going through the SPA fetch
  // wrapper). Caller builds an <a href={url} download> or window.open.
  eventsCsvUrl: (q: ComplianceEventsQuery = {}) => {
    const qs = _complianceQs(q)
    const base = getBasePath()
    return `${base}/api/admin/compliance-events?format=csv${qs ? '&' + qs : ''}`
  },
  policyChanges: (apiKeyId?: string | null, limit = 100) => {
    const sp = new URLSearchParams()
    if (apiKeyId) sp.set('api_key_id', apiKeyId)
    sp.set('limit', String(limit))
    return api.get<{ changes: CompliancePolicyChange[] }>(
      `/api/admin/compliance-policy-changes?${sp.toString()}`,
    )
  },
  clusterReady: () =>
    api.get<ClusterComplianceReadiness>('/api/admin/cluster/compliance-ready'),
  // v5.3.2 — lightweight company list for the policy-editor dropdowns.
  // Returns KNOWN_COMPANIES + any operator-defined custom entries
  // (COMPLIANCE_CUSTOM_COMPANIES env JSON). Sorted by label.
  taxonomy: () =>
    api.get<{ companies: { id: string; label: string; source: 'known' | 'custom' }[] }>(
      '/api/compliance/taxonomy',
    ),
}

// v5.1.0 / Batch C1 — activity-log on/off toggle (compliance panic button)
export interface LoggingStatus {
  enabled: boolean
  setting_key: string
  last_flip: {
    changed_at: string | null
    changed_by: string | null
    reason: string | null
    policy_change_id: string | null
  } | null
}

export const loggingApi = {
  status: () => api.get<LoggingStatus>('/api/admin/logging/status'),
  toggle: (enabled: boolean, reason?: string) =>
    api.post<{ ok: boolean; enabled: boolean; prior_state: boolean;
               noop: boolean; audit_id: string }>(
      '/api/admin/logging/toggle', { enabled, reason },
    ),
  // v5.1.1 / Batch C2 — time-range bulk purge of activity_log rows.
  // Fans out to peers via HMAC. Window is capped at 90 days
  // server-side.
  purge: (startTs: number, endTs: number, reason: string) =>
    api.post<{
      ok: boolean
      local: { deleted: number; audit_id: string }
      peers: { peer_id: string; peer_url: string; status: string;
               deleted: number; error?: string }[]
    }>('/api/admin/activity-log/purge', {
      start_ts: startTs, end_ts: endTs, reason,
    }),
  // v5.1.2 / Batch C3 — retention period editable in WebUI.
  retention: () =>
    api.get<RetentionState>('/api/admin/logging/retention'),
  setRetention: (body: {
    info_days?: number | null
    warning_days?: number | null
    error_days?: number | null
    clear_info?: boolean
    clear_warning?: boolean
    clear_error?: boolean
    reason?: string
  }) =>
    api.post<{
      ok: boolean; audit_ids: string[]; current: RetentionState
    }>('/api/admin/logging/retention', body),
}

// v5.2.0 / Batch V1 — LLM-call emergency stop (kill switch).
// Distinct from the v5.1.0 logging toggle: this one halts ROUTING,
// not log writes. UI lives in CompliancePage.tsx next to the
// LoggingControlsPanel.
export interface LLMEmergencyStatus {
  enabled: boolean
  setting_key: string
  last_flip: {
    changed_at: string | null
    changed_by: string | null
    reason: string | null
    policy_change_id: string | null
  } | null
}

export const llmEmergencyApi = {
  status: () => api.get<LLMEmergencyStatus>('/api/admin/llm-emergency-stop/status'),
  toggle: (enabled: boolean, reason?: string) =>
    api.post<{ ok: boolean; enabled: boolean; prior_state: boolean;
               noop: boolean; audit_id: string }>(
      '/api/admin/llm-emergency-stop/toggle', { enabled, reason },
    ),
}

// v5.1.2 / Batch C3
export interface RetentionEntry {
  override: number | null
  env_default: number
  effective_days: number
}
export interface RetentionState {
  info:    RetentionEntry
  warning: RetentionEntry
  error:   RetentionEntry
}

// ── Users ─────────────────────────────────────────────────────────────────────
export interface BulkDeleteResult {
  deleted: string[]
  errors: { id: string; reason: string }[]
}

export const usersApi = {
  list:   ()                             => api.get<User[]>('/api/users'),
  create: (data: { username: string; password: string; role: string }) =>
    api.post<User>('/api/users', data),
  update: (id: string, data: { password?: string; role?: string }) =>
    api.patch<User>(`/api/users/${id}`, data),
  delete: (id: string)                   => api.delete<void>(`/api/users/${id}`),
  // v5.0.22 — bulk-delete endpoint (BUG-070 + bulk-UX feature)
  bulkDelete: (ids: string[])            => api.post<BulkDeleteResult>('/api/users/bulk_delete', { ids }),
}

// ── Monitoring ────────────────────────────────────────────────────────────────
export interface ActivityQuery {
  limit?: number
  before_id?: number | null
  provider_id?: string | null
  severity?: string | null
  // v3.0.79: filter by event_meta.error_class taxonomy bucket
  // (auth, billing, rate_limit, timeout, network, upstream_5xx, bad_request,
  // unknown). Comma-separated for OR semantics.
  error_class?: string | null
  search?: string | null
}

function _activityQs(q: ActivityQuery): string {
  const sp = new URLSearchParams()
  if (q.limit != null)       sp.set('limit', String(q.limit))
  if (q.before_id != null)   sp.set('before_id', String(q.before_id))
  if (q.provider_id)         sp.set('provider_id', q.provider_id)
  if (q.severity)            sp.set('severity', q.severity)
  if (q.error_class)         sp.set('error_class', q.error_class)
  if (q.search)              sp.set('search', q.search)
  return sp.toString()
}

export const monitoringApi = {
  activity:        (q: ActivityQuery = {}) =>
    api.get<ActivityEvent[]>(`/api/monitoring/activity?${_activityQs({ limit: 200, ...q })}`),
  activityCount:   (q: Omit<ActivityQuery, 'limit' | 'before_id'> = {}) =>
    api.get<{ total: number }>(`/api/monitoring/activity/count?${_activityQs(q)}`),
  metrics:         (hours = 24) => api.get<MetricsSummary>(`/api/monitoring/metrics?hours=${hours}`),
  metricsByNode:   (hours = 24) => api.get<MetricsByNodeResponse>(`/api/monitoring/metrics-by-node?hours=${hours}`),
  providerMetrics: (id: string, hours = 24) =>
    api.get<{ provider_id: string; hours: number; buckets: MetricBucket[] }>(
      `/api/monitoring/metrics/${id}?hours=${hours}`
    ),
  statusPages: ()           => api.get<ExternalStatus>('/api/monitoring/status-pages'),
  // v3.5.4+ — keep-alive probe back-off state. Empty providers_in_backoff
  // dict at steady-state. When grok-web (or any subscription-tier-probed
  // provider) hits a streak of 429s, surfaces consecutive_rate_limits
  // count + remaining cool-off seconds. Used by the dashboard probe-state
  // widget (v3.5.6+). See docs/refactor-log.md R3 entry for the v3.3.3
  // back-off design rationale.
  probeState: () => api.get<{
    providers_in_backoff: Record<string, { consecutive_rate_limits: number; backoff_remaining_sec: number }>
    as_of: string
  }>('/api/monitoring/probe-state'),
  // v3.0.73 — cache-stats rollup. Reads cache_read/creation tokens from
  // event_meta over a rolling window and surfaces hit rate + estimated $ savings.
  cacheStats: (params: {
    windowMinutes?: number
    groupBy?: 'provider' | 'api_key' | 'none'
    bucketMinutes?: number  // v3.0.80: when set, response includes time_series
    ratePerMillion?: number
    cacheDiscountPct?: number
  } = {}) => {
    const qs = new URLSearchParams()
    if (params.windowMinutes !== undefined) qs.set('window_minutes', String(params.windowMinutes))
    if (params.groupBy !== undefined) qs.set('group_by', params.groupBy)
    if (params.bucketMinutes !== undefined) qs.set('bucket_minutes', String(params.bucketMinutes))
    if (params.ratePerMillion !== undefined) qs.set('rate_per_million', String(params.ratePerMillion))
    if (params.cacheDiscountPct !== undefined) qs.set('cache_discount_pct', String(params.cacheDiscountPct))
    const q = qs.toString()
    return api.get<CacheStats>(`/api/monitoring/cache-stats${q ? '?' + q : ''}`)
  },
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
// v5.0.18 — UI-configurable peer list. listPeers / addPeer / removePeer
// drive the new Settings → Cluster Peers panel.
export interface ClusterPeerRow {
  id: string
  url: string
  name: string | null
  added_at: string | null
  removed_at: string | null
  active: boolean
}

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
  // v5.0.18 — UI-configurable cluster peers
  // v5.0.18-hotfix: paths corrected from /api/cluster/peers (returned
  // 404 — server router mounts at /cluster without /api/ prefix,
  // matching every other cluster method above). UI panel was DOA
  // before this fix. Caught by QA sweep 2026-06-05.
  listPeers:   ()                                => api.get<ClusterPeerRow[]>('/cluster/peers'),
  addPeer:     (body: {id: string; url: string; name?: string}) =>
                                                    api.post<{ok: boolean; id: string; url: string}>('/cluster/peers', body),
  removePeer:  (peerId: string)                  => api.delete<{ok: boolean; id: string; removed_at: string}>(`/cluster/peers/${peerId}`),
}
