from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Optional


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Server
    port: int = Field(3000, alias="PORT")
    log_level: str = Field("info", alias="LOG_LEVEL")
    secret_key: str = Field("change-me-in-production", alias="SECRET_KEY")

    # Database
    database_url: str = Field(
        "sqlite+aiosqlite:////app/data/llmproxy.db", alias="DATABASE_URL"
    )

    # Redis (optional — in-memory fallback when not set)
    redis_url: Optional[str] = Field(None, alias="REDIS_URL")

    # Circuit breaker defaults
    circuit_breaker_threshold: int = Field(3, alias="CIRCUIT_BREAKER_THRESHOLD")
    circuit_breaker_timeout_sec: int = Field(60, alias="CIRCUIT_BREAKER_TIMEOUT_SEC")
    circuit_breaker_halfopen_sec: int = Field(30, alias="CIRCUIT_BREAKER_HALFOPEN_SEC")
    circuit_breaker_success_needed: int = Field(2, alias="CIRCUIT_BREAKER_SUCCESS_NEEDED")

    # Hold-down timer (seconds to suppress a provider after failure)
    hold_down_sec: int = Field(120, alias="HOLD_DOWN_SEC")

    # CoT-E pipeline
    cot_enabled: bool = Field(True, alias="COT_ENABLED")
    cot_max_iterations: int = Field(1, alias="COT_MAX_ITERATIONS")
    cot_quality_threshold: int = Field(6, alias="COT_QUALITY_THRESHOLD")
    cot_critique_max_tokens: int = Field(200, alias="COT_CRITIQUE_MAX_TOKENS")
    cot_plan_max_tokens: int = Field(400, alias="COT_PLAN_MAX_TOKENS")
    cot_session_ttl_sec: int = Field(1800, alias="COT_SESSION_TTL_SEC")
    cot_session_max_analyses: int = Field(3, alias="COT_SESSION_MAX_ANALYSES")
    # Skip critique/refinement when the initial draft exceeds this token count;
    # 0 = always refine. Avoids wasted calls on already-thorough long answers.
    cot_min_tokens_skip: int = Field(800, alias="COT_MIN_TOKENS_SKIP")
    # Native reasoning — injected into requests routed to thinking-capable providers.
    # budget_tokens applies to Gemini 2.5 and is passed through for Anthropic thinking requests.
    # reasoning_effort applies to OpenAI o-series (low / medium / high).
    native_thinking_budget_tokens: int = Field(8192, alias="NATIVE_THINKING_BUDGET_TOKENS")
    native_reasoning_effort: str = Field("medium", alias="NATIVE_REASONING_EFFORT")

    # Verification pass — generates "what commands verify this answer?" after refinement.
    # Disabled by default (adds one extra LLM call). Enable globally or per-request
    # via X-Cot-Verify: true.
    cot_verify_enabled: bool = Field(False, alias="COT_VERIFY_ENABLED")
    cot_verify_max_tokens: int = Field(400, alias="COT_VERIFY_MAX_TOKENS")
    # When True, only verify answers that contain shell code blocks or infra CLI tools.
    # When False, verify every CoT response (use with care — adds latency to all requests).
    cot_verify_auto_detect: bool = Field(True, alias="COT_VERIFY_AUTO_DETECT")
    # Cross-provider critique (Wave 2 #8): route the critique pass to a DIFFERENT
    # provider than the one producing the draft. Eliminates ~5-15% self-preference
    # bias documented in 2024-25 LLM-as-Judge surveys.
    cot_cross_provider_critique: bool = Field(True, alias="COT_CROSS_PROVIDER_CRITIQUE")
    # Wave 2 #9: actually execute verify steps (HTTP/DNS/TCP only, 5s each).
    # Off by default; flip on once operators are comfortable that only the
    # network-safe subset ever executes in-process. Unsafe commands are
    # always emitted as structured SSE verify_step events for client-side exec.
    cot_verify_execute: bool = Field(False, alias="COT_VERIFY_EXECUTE")
    cot_verify_step_timeout_sec: float = Field(5.0, alias="COT_VERIFY_STEP_TIMEOUT_SEC")

    # Semantic cache (Wave 1 #3). Requires Redis-Stack / RediSearch.
    semantic_cache_enabled: bool = Field(True, alias="SEMANTIC_CACHE_ENABLED")
    semantic_cache_threshold: float = Field(0.88, alias="SEMANTIC_CACHE_THRESHOLD")
    semantic_cache_ttl_sec: int = Field(86400, alias="SEMANTIC_CACHE_TTL_SEC")
    semantic_cache_embedding_model: str = Field(
        "text-embedding-3-small", alias="SEMANTIC_CACHE_EMBEDDING_MODEL"
    )
    # Matryoshka-truncated dimensions — 512 keeps ~98% quality at 33% size
    semantic_cache_embedding_dims: int = Field(512, alias="SEMANTIC_CACHE_EMBEDDING_DIMS")
    # Minimum response length (chars) to be worth caching — filters refusals, errors
    semantic_cache_min_response_chars: int = Field(200, alias="SEMANTIC_CACHE_MIN_RESPONSE_CHARS")

    # Hedged requests (Wave 1 #4)
    hedge_enabled: bool = Field(True, alias="HEDGE_ENABLED")
    hedge_max_per_sec: float = Field(5.0, alias="HEDGE_MAX_PER_SEC")

    # Cluster
    cluster_enabled: bool = Field(False, alias="CLUSTER_ENABLED")
    cluster_node_id: Optional[str] = Field(None, alias="CLUSTER_NODE_ID")
    cluster_node_name: Optional[str] = Field(None, alias="CLUSTER_NODE_NAME")
    cluster_node_url: Optional[str] = Field(None, alias="CLUSTER_NODE_URL")
    cluster_peers: Optional[str] = Field(None, alias="CLUSTER_PEERS")  # "id:url,id:url"
    cluster_sync_secret: Optional[str] = Field(None, alias="CLUSTER_SYNC_SECRET")
    cluster_heartbeat_sec: int = Field(30, alias="CLUSTER_HEARTBEAT_SEC")

    # Notifications
    smtp_enabled: bool = Field(False, alias="SMTP_ENABLED")
    smtp_host: Optional[str] = Field(None, alias="SMTP_HOST")
    smtp_port: int = Field(587, alias="SMTP_PORT")
    smtp_user: Optional[str] = Field(None, alias="SMTP_USER")
    smtp_pass: Optional[str] = Field(None, alias="SMTP_PASS")
    smtp_from: Optional[str] = Field(None, alias="SMTP_FROM")
    smtp_to: Optional[str] = Field(None, alias="SMTP_TO")


settings = Settings()
