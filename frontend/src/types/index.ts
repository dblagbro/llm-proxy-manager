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
  | 'codex-oauth'    // v3.0.15: OpenAI Codex CLI / ChatGPT subscription via OAuth
  | 'cohere'         // v3.0.23: primarily embeddings (also rerank/chat)
  | 'azure'          // v3.0.66: Microsoft Azure OpenAI Service
  | 'openrouter'     // v3.1.3: OpenRouter multi-vendor marketplace
  | 'grok-web'       // v3.2.0: grok.com web subscription via cookie replay

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
  // v3.0.64+: per-provider subscription-quota tracking. Surfaced when
  // ``usage_tracking_enabled=true`` on the provider AND a session/weekly
  // window has been computed by the usage tracker. Both pcts are 0-N
  // (can exceed 100 if the operator's configured limit is lower than
  // the actual upstream allowance — e.g. Anthropic Pro Max). Tokens
  // are absolute counts when the limit hasn't been set (pct == null).
  usage_tracking_enabled?: boolean
  usage_session_pct?: number | null
  usage_weekly_pct?: number | null
  usage_session_tokens?: number | null
  usage_weekly_tokens?: number | null
  usage_session_limit_tokens?: number | null
  usage_weekly_limit_tokens?: number | null
  // v3.7.0/v3.7.1/v3.7.2/v3.7.3 — Anthropic Console billing scrape +
  // auto-rotation surface. Operator pastes a captured browser cookie
  // blob; the scraper runs every 4h and writes ExternalUsageSnapshot
  // rows. When seven_day_utilization >= 95% the auto-rotation rule
  // sets auto_skip_until = snapshot.seven_day_resets_at; router
  // skips this provider until then. Cookies themselves are never
  // returned by the API — only the boolean has_anthropic_session_cookies.
  anthropic_org_uuid?: string | null
  has_anthropic_session_cookies?: boolean
  anthropic_session_captured_at?: number | null
  auto_skip_until?: string | null
  auto_skip_reason?: string | null
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
  // v3.5.0+ — model-identity fields. Optional on the wire; older
  // proxies omit them. See docs/rfc/2026-05-model-identity.md
  // for the canonical naming convention + family/variant rationale.
  aliases?: string[]
  model_family?: string | null
  model_variant?: string | null
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
// v3.7.17 — 'admin-readonly-catalog' is the narrow-scope key the
// coordinator-hub team provisions in their "Proxy Catalog Admin Key"
// setting. Backed by app/auth/catalog_scope.py (v3.7.2): can edit
// /api/llm/models/{id} per-model aliases/family/variant, but
// verify_api_key rejects it on /v1/messages + /v1/chat/completions,
// so it cannot make inference calls.
export type KeyType = 'standard' | 'claude-code' | 'admin-readonly-catalog'

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

// v3.0.73 — /api/monitoring/cache-stats response shape (rolling window).
export interface CacheStatsBucket {
  events: number
  events_with_cache_read: number
  events_with_cache_creation: number
  cache_read_tokens: number
  cache_creation_tokens: number
  new_input_tokens: number
  cache_hit_rate_pct: number
  cache_share_of_input_pct: number
  estimated_savings_usd: number
}

export interface CacheStatsGroup extends CacheStatsBucket {
  id: string
  name: string
}

export interface CacheStatsTimeBucket extends CacheStatsBucket {
  bucket_start: string  // ISO 8601 UTC
  bucket_end: string
}

export interface CacheStats {
  window_minutes: number
  rate_per_million_usd: number
  cache_discount_pct: number
  overall: CacheStatsBucket
  by_group: CacheStatsGroup[]
  group_by: 'provider' | 'api_key' | 'none'
  // v3.0.80+: present when caller passes bucket_minutes>0
  bucket_minutes?: number
  time_series?: CacheStatsTimeBucket[]
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
