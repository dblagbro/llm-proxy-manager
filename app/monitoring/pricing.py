"""
Cost estimation per request.

Lookup order (v5.20.6):
  1. DB ``model_pricing_catalog`` (fresh, ingested daily from
     LiteLLM upstream by ``model_cost_map_worker``).
  2. ``litellm.cost_per_token`` (whatever ships with the installed
     ``litellm`` package).
  3. Zero (unknown/free/local model).

v5.20.6 removed the stale ``_OVERRIDES`` dict — the catalog covers
every model that was in it, with fresher prices from LiteLLM's ``main``
branch. Same-transaction DB check on 2026-07-08 confirmed all 8
override entries are in the catalog; two even differed from the
hardcodes (opus 3x, haiku 25%) — the catalog is more accurate.

If the catalog is empty AND litellm doesn't know the model, cost
returns zero — that's fine for the "free/local" case (e.g. an on-prem
llama.cpp sidecar). Not a regression versus the pre-v5.20.6 shape,
which would have also returned zero for the same input.
"""
import logging
from typing import Optional

import litellm

logger = logging.getLogger(__name__)


# v5.20.4 — In-process cache of the DB catalog. Populated lazily on
# first lookup + refreshed by the sync worker (which calls
# ``invalidate_catalog_cache`` on successful upsert). Keyed on
# model name; value is (input_per_token, output_per_token).
_CATALOG_CACHE: dict[str, tuple[float, float]] = {}
# v5.22.13 — model_key -> max_output_tokens, same lifecycle as _CATALOG_CACHE.
_MAX_OUTPUT_CACHE: dict[str, int] = {}
_CATALOG_LOADED = False


def invalidate_catalog_cache() -> None:
    """Called by the sync worker after upsert so the next lookup
    reloads from DB."""
    global _CATALOG_LOADED
    _CATALOG_CACHE.clear()
    _MAX_OUTPUT_CACHE.clear()
    _CATALOG_LOADED = False


def _load_catalog_cache_sync() -> None:
    """Best-effort synchronous load — pricing lookups are on the
    request hot path, so we can't await here. We use a synchronous
    engine to hit the same SQLite DB. Failures leave the cache empty
    (which just falls through to the existing litellm/override lookup)."""
    global _CATALOG_LOADED
    if _CATALOG_LOADED:
        return
    _CATALOG_LOADED = True  # set FIRST so a load failure doesn't loop
    try:
        from sqlalchemy import create_engine, text
        # v5.22.13 BUGFIX: this used to be
        #     from app.models.database import DATABASE_URL
        # but app.models.database does NOT export that name. The ImportError
        # was swallowed by the broad ``except Exception: pass`` below, so
        # _CATALOG_CACHE stayed permanently EMPTY and every pricing lookup
        # silently fell through to the litellm built-in. The daily
        # model_cost_map_worker ingest (2,568 rows) was never read by anything.
        # Read the configured URL from settings, which is the real source.
        from app.config import settings
        # AsyncSessionLocal uses aiosqlite; swap to sync driver for
        # this cheap read.
        sync_url = settings.database_url.replace("sqlite+aiosqlite", "sqlite")
        engine = create_engine(sync_url)
        with engine.connect() as conn:
            rows = conn.execute(text(
                "SELECT model_key, input_cost_per_token, output_cost_per_token, "
                "max_output_tokens FROM model_pricing_catalog"
            )).fetchall()
        for row in rows:
            _CATALOG_CACHE[row[0]] = (float(row[1]), float(row[2]))
            # v5.22.13 — the catalog also carries each model's OUTPUT cap
            # (90% coverage as of 2026-08-18). Cache it alongside pricing so
            # the router can refuse to send a request to a provider that
            # physically cannot emit the requested max_tokens. NULL stays
            # absent from the map, and absent means "unknown, do not filter".
            if row[3] is not None:
                try:
                    _MAX_OUTPUT_CACHE[row[0]] = int(row[3])
                except (TypeError, ValueError):
                    pass
    except Exception:
        # Table may not exist yet on a fresh boot before init_db() —
        # that's fine, the cache stays empty and lookups fall through
        # to the litellm built-in.
        pass


def _catalog_lookup(model: str) -> Optional[tuple[float, float]]:
    """Return (input_per_token, output_per_token) from the DB catalog
    if present, else None. Tries the model as-is + with/without a
    provider prefix (same variants as the _OVERRIDES lookup)."""
    _load_catalog_cache_sync()
    entry = _CATALOG_CACHE.get(model)
    if entry is not None:
        return entry
    if "/" in model:
        entry = _CATALOG_CACHE.get(model.split("/", 1)[1])
        if entry is not None:
            return entry
    # Try prefix-adding for bare model names
    for k, v in _CATALOG_CACHE.items():
        if k.endswith("/" + model):
            return v
    return None


def max_output_tokens_for(model: str) -> int | None:
    """Return the model's maximum OUTPUT tokens from the DB catalog, or
    ``None`` when unknown.

    v5.22.13. Mirrors ``_catalog_lookup``'s prefix-variant matching so
    ``cohere/command-r``, ``command-r`` and a bare alias all resolve.

    ``None`` means "not in the catalog" and callers MUST treat it as
    "do not filter" — the catalog covers ~90% of models, and silently
    excluding providers for the other 10% would be worse than the
    occasional upstream rejection this exists to prevent.
    """
    _load_catalog_cache_sync()
    v = _MAX_OUTPUT_CACHE.get(model)
    if v is not None:
        return v
    if "/" in model:
        v = _MAX_OUTPUT_CACHE.get(model.split("/", 1)[1])
        if v is not None:
            return v
    for k, val in _MAX_OUTPUT_CACHE.items():
        if k.endswith("/" + model):
            return val
    return None


def estimate_cost_split(
    litellm_model: str, input_tokens: int, output_tokens: int,
) -> tuple[float, float]:
    """v3.4.0: returns ``(input_cost_usd, output_cost_usd)`` tuple.

    Splits the per-direction cost so callers (record_request, snapshot)
    can track / report input and output separately. ``estimate_cost()``
    sums the two for back-compat.

    v5.20.6 lookup order:
      1. DB ``model_pricing_catalog`` (fresh, LiteLLM ``main`` daily)
      2. ``litellm.cost_per_token`` (installed package)
      3. Zero (unknown/free/local)
    """
    # 1) DB catalog
    catalog_hit = _catalog_lookup(litellm_model)
    if catalog_hit is not None:
        in_per, out_per = catalog_hit
        if in_per > 0 or out_per > 0:
            return (input_tokens * in_per, output_tokens * out_per)

    # 2) litellm's built-in cost lookup
    try:
        in_cost, out_cost = litellm.cost_per_token(
            model=litellm_model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
        in_cost_f = float(in_cost)
        out_cost_f = float(out_cost)
        if in_cost_f > 0 or out_cost_f > 0:
            return (in_cost_f, out_cost_f)
    except Exception:
        pass

    # Unknown model: return zeros (free/local)
    return (0.0, 0.0)


def estimate_cost(litellm_model: str, input_tokens: int, output_tokens: int) -> float:
    """Returns total estimated cost in USD (input + output combined).

    v3.0.2: previous version called ``litellm.completion_cost(prompt_tokens=...,
    completion_tokens=...)`` — that kwargs signature was rejected by current
    litellm with TypeError, so every call silently returned 0 and the proxy
    reported $0.00 for every request. Switched to ``litellm.cost_per_token``
    which returns ``(input_cost, output_cost)`` already scaled by the token
    counts (the values are the totals, not per-token rates).

    v3.4.0: thin wrapper around ``estimate_cost_split()``. Existing
    callers keep working unchanged.
    """
    in_cost, out_cost = estimate_cost_split(litellm_model, input_tokens, output_tokens)
    return in_cost + out_cost


def format_cost(usd: float) -> str:
    if usd == 0:
        return "$0.00"
    if usd < 0.000001:
        return f"${usd:.8f}"
    if usd < 0.01:
        return f"${usd:.6f}"
    return f"${usd:.4f}"
