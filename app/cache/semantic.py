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
        # v5.0.0 compliance — tag every cache row with the source provider's
        # owner_company so check() can drop hits the requesting key has banned.
        # NULL is treated as banned by check() when blocklist non-empty
        # (decision 7: unknown provenance must not be served to a banned key).
        {"name": "source_company", "type": "tag"},
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
            # v5.0.0 — if a prior boot created the index without the
            # source_company tag, drop + recreate. One-time cache loss on
            # upgrade is acceptable (rebuilds within hours of normal traffic);
            # serving pre-tagged hits to compliance-filtered keys is NOT.
            await self._ensure_index_schema()
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

    async def _ensure_index_schema(self) -> None:
        """One-shot guard: if the existing RediSearch index is missing the
        v5.0.0 ``source_company`` field, drop it so ``create(overwrite=False)``
        below rebuilds with the new schema. Silent no-op when the index
        doesn't exist yet (fresh boot) or when FT.INFO isn't reachable —
        the subsequent create() handles those cases.
        """
        try:
            info = await self._index.info()
        except Exception:
            return  # No existing index or transient error — create() handles it.
        try:
            attrs = info.get("attributes") if isinstance(info, dict) else None
            if not attrs:
                return
            field_names = set()
            for a in attrs:
                if isinstance(a, dict):
                    name = a.get("attribute") or a.get("identifier") or a.get("name")
                    if name:
                        field_names.add(str(name))
                elif isinstance(a, (list, tuple)):
                    # RediSearch sometimes returns attribute info as a flat
                    # list of [key, value, key, value, ...] pairs.
                    for i, v in enumerate(a):
                        if v in ("attribute", "identifier") and i + 1 < len(a):
                            field_names.add(str(a[i + 1]))
            if "source_company" not in field_names:
                logger.info(
                    "semantic_cache.schema_migration — dropping legacy index "
                    "(missing source_company); cache will rebuild from misses"
                )
                try:
                    await self._index.delete(drop=True)
                except Exception as e:
                    logger.warning("semantic_cache.schema_migration_drop_failed %s", e)
        except Exception as e:
            logger.warning("semantic_cache.schema_migration_inspect_failed %s", e)

    async def _embed(self, text: str) -> Optional[list[float]]:
        # v3.0.67: when ``semantic_cache_provider_id`` is set, route the
        # embedding call through that specific proxy provider's api_key +
        # base_url + litellm prefix, so the operator's preferred provider
        # (often the priority=1 row, e.g. Google Gemini) actually serves
        # the embedding rather than litellm calling OpenAI direct via the
        # bare model name. When unset, fall back to the legacy "model name
        # selects provider implicitly" behavior for backwards compat.
        try:
            import litellm
            provider_id = (settings.semantic_cache_provider_id or "").strip()
            kwargs: dict = {
                "model": settings.semantic_cache_embedding_model,
                "input": [text],
                "dimensions": settings.semantic_cache_embedding_dims,
            }
            if provider_id:
                from app.models.database import AsyncSessionLocal
                from app.models.db import Provider
                async with AsyncSessionLocal() as db:
                    p = await db.get(Provider, provider_id)
                if p is not None and p.enabled and p.deleted_at is None:
                    from app.routing.router import build_litellm_model, build_litellm_kwargs
                    # Caller-pinned model lives on settings.semantic_cache_embedding_model;
                    # build_litellm_model prefixes it with the provider's family tag.
                    kwargs["model"] = build_litellm_model(p, model_override=settings.semantic_cache_embedding_model)
                    pkw = build_litellm_kwargs(p)
                    # Don't override max_tokens etc. on an embed call — only auth + endpoint.
                    for k in ("api_key", "api_base", "api_version"):
                        if k in pkw:
                            kwargs[k] = pkw[k]
            resp = await litellm.aembedding(**kwargs)
            data = resp.data[0] if isinstance(resp.data, list) else resp["data"][0]
            emb = getattr(data, "embedding", None) or data["embedding"]
            return list(emb)
        except Exception as exc:
            logger.warning("semantic_cache.embed_failed %s", exc)
            return None

    async def check(
        self,
        namespace: str,
        query: str,
        threshold: float,
        *,
        blocked_companies: Optional[set[str]] = None,
    ) -> Optional[tuple[str, float]]:
        """Return (cached_response, similarity) on hit, else None.

        ``blocked_companies`` (v5.0.0 compliance) filters cache rows whose
        ``source_company`` is banned. Decision 7: rows with NULL
        source_company are also dropped when the blocklist is non-empty —
        unknown provenance must not be served to a banned key.
        """
        if not query or not await self._ensure_init():
            return None
        vec = await self._embed(query)
        if vec is None:
            return None
        try:
            from redisvl.query import VectorQuery
            from redisvl.query.filter import Tag
            # Pull more candidates than 1 when filtering so a single banned
            # row at the top doesn't starve a legitimate runner-up. The
            # similarity gate still applies after the company filter.
            num_results = 5 if blocked_companies else 1
            vq = VectorQuery(
                vector=vec,
                vector_field_name="prompt_vector",
                return_fields=["response", "prompt", "source_company"],
                num_results=num_results,
                filter_expression=Tag("namespace") == namespace,
            )
            results = await self._index.query(vq)
            if not results:
                return None
            if blocked_companies:
                # Decision 7 — NULL source_company is treated as banned by
                # any non-empty blocklist. We can't trust pre-v5 cache rows
                # (or any row that lost provenance) to be safe to serve.
                filtered = [
                    r for r in results
                    if r.get("source_company") is not None
                    and r.get("source_company") not in blocked_companies
                ]
                if not filtered:
                    return None
                results = filtered
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
        self,
        namespace: str,
        query: str,
        response: str,
        ttl_sec: int,
        *,
        source_company: Optional[str] = None,
    ) -> None:
        if not query or not response or not await self._ensure_init():
            return
        vec = await self._embed(query)
        if vec is None:
            return
        try:
            import struct
            packed = struct.pack(f"{len(vec)}f", *vec)
            payload = {
                "namespace": namespace,
                "prompt": query[:4000],
                "response": response,
                "ttl": ttl_sec,
                "prompt_vector": packed,
            }
            # Only set source_company when known; leaving it absent on the
            # hash matches the "NULL = unknown = banned" semantics in check().
            if source_company:
                payload["source_company"] = source_company
            await self._index.load([payload], ttl=ttl_sec)
        except Exception as exc:
            logger.warning("semantic_cache.store_failed %s", exc)


_instance: Optional[SemanticCache] = None


def get_cache() -> SemanticCache:
    global _instance
    if _instance is None:
        _instance = SemanticCache()
    return _instance
