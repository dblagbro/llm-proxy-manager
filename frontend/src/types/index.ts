// ── Auth ─────────────────────────────────────────────────────────────────────
export type TimeFormatPref = '12h' | '24h' | null
export interface AuthUser {
  username: string
  role: 'admin' | 'user'
  timezone?: string | null     // IANA name, null = browser default
  time_format?: TimeFormatPref // 12h | 24h | null = locale default
}

// ── Providers ─────────────────────────────────────────────────────────────────
export type ProviderType =
  | 'anthropic' | 'openai' | 'google' | 'vertex' | 'grok' | 'ollama' | 'compatible'
  | 'claude-oauth'   // v2.7.0: Claude Pro Max subscription via pasted OAuth credentials

export interface Provider {
  id: string
  name: string
  provider_type: ProviderType
  api_key: string | null        // masked: "sk-ant-ap..."
  base_url: string | null
  default_model: string | null
  priority: number
  enabled: boolean
  timeout_sec: number
  exclude_from_tool_requests: boolean
  hold_down_sec: number | null
  failure_threshold: number | null
  extra_config: Record<string, unknown>
  created_at: string
  // v2.7.0: surfaced only for claude-oauth providers. null otherwise.
  oauth_expires_at?: number | null
  has_oauth_refresh_token?: boolean
  // v2.7.8: when set, the provider's auth failed and admin must re-key
  // (or re-OAuth). UI renders a red "Needs re-auth" badge.
  auth_failed?: { since: number; last_error: string } | null
}

export interface ProviderFormData {
  name: string
  provider_type: ProviderType
  api_key?: string
  base_url?: string
  default_model?: string
  priority: number
  enabled: boolean
  timeout_sec: number
  exclude_from_tool_requests: boolean
  hold_down_sec: number | null
  failure_threshold: number | null
  extra_config: Record<string, unknown>
  // v2.7.0: the JSON blob (or bare token) the admin pastes for claude-oauth
  oauth_credentials_blob?: string
}

export interface ModelCapability {
  id: number
  provider_id: string
  model_id: string
  tasks: string[]
  latency: 'low' | 'medium' | 'high'
  cost_tier: 'economy' | 'standard' | 'premium'
  safety: number
  context_length: number
  regions: string[]
  modalities: string[]
  native_reasoning: boolean
  native_tools: boolean
  native_vision: boolean
  source: 'inferred' | 'manual'
}

export interface TestResult {
  success: boolean
  response?: string
  error?: string
  model: string
}

export interface ScannedModel {
  model_id: string
  tasks: string[]
  cost_tier: string
  native_reasoning: boolean
}

// ── Circuit Breaker ───────────────────────────────────────────────────────────
export type CBState = 'closed' | 'open' | 'half-open'

export interface CircuitBreakerInfo {
  state: CBState
  failures: number
  hold_down_remaining: number
}

// ── API Keys ──────────────────────────────────────────────────────────────────
export type KeyType = 'standard' | 'claude-code'

export interface ApiKey {
  id: string
  name: string
  key_prefix: string
  key_type: KeyType
  enabled: boolean
  total_requests: number
  total_tokens: number
  total_cost_usd: number
  spending_cap_usd: number | null
  rate_limit_rpm: number | null
  last_used_at: string | null
  created_at: string
  raw_key?: string  // only on create response
}

// ── Users ─────────────────────────────────────────────────────────────────────
export interface User {
  id: string
  username: string
  role: 'admin' | 'user'
  created_at: string
}

// ── Activity Log ──────────────────────────────────────────────────────────────
export type Severity = 'info' | 'warning' | 'error' | 'critical'

export interface ActivityEvent {
  id: number
  event_type: string
  severity: Severity
  message: string
  provider_id: string | null
  timestamp: string
  metadata: Record<string, unknown>
}

// ── Metrics ───────────────────────────────────────────────────────────────────
export interface MetricBucket {
  ts: string
  requests: number
  successes: number
  failures: number
  total_tokens: number
  total_cost_usd: number
  avg_latency_ms: number
  circuit_state: CBState
}

export interface ProviderSummary {
  provider_id: string
  provider_name?: string
  requests: number
  successes: number
  failures: number
  success_rate: number
  total_tokens: number
  total_cost_usd: number
  avg_latency_ms: number
  avg_ttft_ms?: number
  circuit_state: CBState
}

export interface MetricsSummary {
  hours: number
  providers: ProviderSummary[]
  circuit_breakers: Record<string, CircuitBreakerInfo>
}

// ── Cluster ───────────────────────────────────────────────────────────────────
export interface ClusterNode {
  id: string
  name: string
  url: string
  status: 'healthy' | 'degraded' | 'unreachable' | 'unknown'
  latency_ms?: number
  last_heartbeat?: number
  healthy_providers?: number
  total_providers?: number
}

export interface ClusterStatus {
  cluster_enabled: boolean
  local_node: ClusterNode
  peers: ClusterNode[]
  total_nodes: number
  healthy_nodes: number
}

// ── Health ────────────────────────────────────────────────────────────────────
export interface HealthStatus {
  status: 'healthy' | 'degraded'
  version: string
  nodeId: string | null
  totalProviders: number
  healthyProviders: number
  circuitBreakers: Record<string, CircuitBreakerInfo>
}

export interface ExternalStatus {
  anthropic: { degraded: boolean; description: string }
  openai: { degraded: boolean; description: string }
  google: { degraded: boolean; description: string }
}
