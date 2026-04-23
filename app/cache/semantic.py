"""Semantic cache backed by Redis (RediSearch / HNSW) via RedisVL.

Embeddings are produced with `litellm.aembedding()` so any provider
configured for embeddings (OpenAI, Voyage, self-hosted) can be swapped in
via the `semantic_cache_embedding_model` runtime setting.

Single shared index keyed by (namespace + embedding). Namespace isolates
tenants, model version, tool set, system prompt hash — see `keys.py`.
All cache ops are graceful: if Redis-Stack isn't available or embedding
call fails, we log once and return miss/no-op (never break the request).
"""
import logging
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


_INDEX_SCHEMA = {
    "index": {"name": "llmproxy_semcache", "prefix": "smc_entry", "storage_type": "hash"},
    "fields": [
        {"name": "namespace", "type": "tag"},
        {"name": "prompt", "type": "text"},
        {"name": "response", "type": "text"},
        {"name": "ttl", "type": "numeric"},
        {
            "name": "prompt_vector",
            "type": "vector",
            "attrs": {
                "dims": 0,  # populated at init time
                "algorithm": "hnsw",
                "distance_metric": "cosine",
            },
        },
    ],
}


class SemanticCache:
    """Thread-safe lazy-init cache. A single instance is shared across requests."""

    def __init__(self) -> None:
        self._index = None
        self._init_attempted = False
        self._init_ok = False

    async def _ensure_init(self) -> bool:
        if self._init_attempted:
            return self._init_ok
        self._init_attempted = True
        if not settings.redis_url:
            logger.info("semantic_cache.disabled — REDIS_URL unset")
            return False
        try:
            from redisvl.index import AsyncSearchIndex
            from redisvl.schema import IndexSchema

            schema_dict = {**_INDEX_SCHEMA}
            schema_dict["fields"] = [dict(f) for f in _INDEX_SCHEMA["fields"]]
            for f in schema_dict["fields"]:
                if f["name"] == "prompt_vector":
                    f["attrs"] = dict(f["attrs"])
                    f["attrs"]["dims"] = settings.semantic_cache_embedding_dims
            schema = IndexSchema.from_dict(schema_dict)
            self._index = AsyncSearchIndex(schema, redis_url=settings.redis_url)
            await self._index.create(overwrite=False)
            self._init_ok = True
            logger.info(
                "semantic_cache.ready dims=%d model=%s",
                settings.semantic_cache_embedding_dims,
                settings.semantic_cache_embedding_model,
            )
        except Exception as exc:
            logger.warning("semantic_cache.init_failed %s — cache disabled", exc)
            self._init_ok = False
        return self._init_ok

    async def _embed(self, text: str) -> Optional[list[float]]:
        try:
            import litellm
            resp = await litellm.aembedding(
                model=settings.semantic_cache_embedding_model,
                input=[text],
                dimensions=settings.semantic_cache_embedding_dims,
            )
            data = resp.data[0] if isinstance(resp.data, list) else resp["data"][0]
            emb = getattr(data, "embedding", None) or data["embedding"]
            return list(emb)
        except Exception as exc:
            logger.warning("semantic_cache.embed_failed %s", exc)
            return None

    async def check(
        self, namespace: str, query: str, threshold: float
    ) -> Optional[tuple[str, float]]:
        """Return (cached_response, similarity) on hit, else None."""
        if not query or not await self._ensure_init():
            return None
        vec = await self._embed(query)
        if vec is None:
            return None
        try:
            from redisvl.query import VectorQuery
            from redisvl.query.filter import Tag
            vq = VectorQuery(
                vector=vec,
                vector_field_name="prompt_vector",
                return_fields=["response", "prompt"],
                num_results=1,
                filter_expression=Tag("namespace") == namespace,
            )
            results = await self._index.query(vq)
            if not results:
                return None
            top = results[0]
            # RedisVL returns vector_distance (0 = identical, 2 = opposite);
            # similarity = 1 - distance / 2 for cosine.
            distance = float(top.get("vector_distance", 1.0))
            similarity = 1.0 - (distance / 2.0)
            if similarity < threshold:
                return None
            return top.get("response", ""), similarity
        except Exception as exc:
            logger.warning("semantic_cache.check_failed %s", exc)
            return None

    async def store(
        self, namespace: str, query: str, response: str, ttl_sec: int
    ) -> None:
        if not query or not response or not await self._ensure_init():
            return
        vec = await self._embed(query)
        if vec is None:
            return
        try:
            import struct
            packed = struct.pack(f"{len(vec)}f", *vec)
            await self._index.load(
                [{
                    "namespace": namespace,
                    "prompt": query[:4000],
                    "response": response,
                    "ttl": ttl_sec,
                    "prompt_vector": packed,
                }],
                ttl=ttl_sec,
            )
        except Exception as exc:
            logger.warning("semantic_cache.store_failed %s", exc)


_instance: Optional[SemanticCache] = None


def get_cache() -> SemanticCache:
    global _instance
    if _instance is None:
        _instance = SemanticCache()
    return _instance
