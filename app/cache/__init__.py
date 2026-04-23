"""Semantic response cache (Wave 1 #3).

Two layers:
- keys.py: multi-dim namespace construction (tenant × model × version × tool_hash × …)
- semantic.py: Redis-backed vector lookup + store with graceful no-op when
  Redis/RediSearch is unavailable.

Gateway-layer, not litellm-native: we need tenant-aware namespacing and
quality gates that the litellm built-in doesn't expose.
"""
from app.cache.keys import (
    build_namespace,
    extract_query_text,
    is_cacheable_temperature,
    contains_pii,
)
from app.cache.semantic import SemanticCache, get_cache

__all__ = [
    "build_namespace",
    "extract_query_text",
    "is_cacheable_temperature",
    "contains_pii",
    "SemanticCache",
    "get_cache",
]
