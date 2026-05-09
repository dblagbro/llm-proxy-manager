"""LMRH v2 client — polls /lmrh/providers and synthesizes optimal LLM-Hint.

Designed for callers (coordinator-hub, devinGPT, paperless, etc.) who want
to participate in the bidirectional protocol without writing the polling
+ scoring boilerplate themselves. Single-file, no dependencies beyond
``httpx``.

Usage
-----

    from sdk.python.lmrh_client import LmrhClient

    client = LmrhClient(
        base_url="https://www.voipguru.org/llm-proxy2",
        api_key="llmp-...",
    )
    client.start()  # spawns background polling task

    # Synchronous usage — pick a provider for "cheap fast chat"
    hint = client.build_hint(
        task="chat",
        prefer="cheapest",      # or "fastest" / "most_reliable"
        model_family="claude",  # optional family hint
    )
    # hint is e.g. "task=chat, cost=economy, provider-hint=claude-oauth"
    # Pass to /v1/messages or /v1/chat/completions:
    headers = {"Authorization": f"Bearer {key}", "LLM-Hint": hint}

    # Inspect raw provider data for custom logic
    snap = client.snapshot()
    for p in snap.providers:
        print(p.name, p.priority, p.circuit, p.models[0].latency_p50_ms)

    client.stop()  # on shutdown


Design choices
--------------

- **In-process polling** rather than per-call: dozens of inference calls
  per second shouldn't trigger dozens of metrics polls. One background
  task refreshes the snapshot every 60s (the recommended cadence per
  /.well-known/lmrh-config polling guidance).
- **ETag-aware**: the client persists the last ETag and sends
  ``If-None-Match`` so steady-state polls return ``304 Not Modified``
  without bandwidth.
- **Graceful degradation**: if the proxy doesn't support v2 (404 on
  /.well-known/lmrh-config), the client still works — ``build_hint``
  just returns whatever the caller asked for, no synthesis. v1.x
  callers are forward-compatible.
- **No dependency on the proxy's internal LMRH dim weights**: the
  client crafts hints using only public dim names (``task``,
  ``cost``, ``latency``, ``region``, ``provider-hint``, ``exclude``).
  If the proxy adds a new dim, callers don't need to recompile.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    raise ImportError(
        "lmrh_client requires httpx. Install with: pip install httpx"
    )

logger = logging.getLogger(__name__)

# Recommended polling cadence per /.well-known/lmrh-config. Stays within
# the proxy's default 4/min rate limit.
DEFAULT_POLL_INTERVAL_SEC = 60


# ── Snapshot data classes (mirror of the proxy's wire format) ──────────


@dataclass(frozen=True)
class ModelMetrics:
    cost_per_1m_input_usd: Optional[float]
    cost_per_1m_output_usd: Optional[float]
    rated_quota_per_1m_input_usd: Optional[float]
    latency_p50_ms: Optional[float]
    latency_p95_ms: Optional[float]
    ttft_p50_ms: Optional[float]
    ttft_p95_ms: Optional[float]
    # v3.3.3+: success_rate / samples reflect USER traffic only.
    # Synthetic probe outcomes don't pollute these aggregates.
    success_rate: Optional[float]
    samples: int
    # v3.3.4+: probe stats — synthetic keep-alive probe outcomes only.
    # Useful for connectivity-health checks ("can the proxy still reach
    # this provider?") that run continuously even when there's no user
    # traffic. None when the proxy didn't probe this provider in the
    # current window (e.g. per-call providers don't get probes by
    # default; rate-limited providers may be in back-off).
    probe_success_rate: Optional[float] = None
    probe_samples: int = 0


@dataclass(frozen=True)
class ModelEntry:
    model_id: str
    kind: str
    context_length: Optional[int]
    native_tools: bool
    native_reasoning: bool
    metrics: ModelMetrics
    # v3.5.0 (LMRHv2.1) — model identity grouping. ``aliases`` is the
    # list of alternate spellings the proxy will accept (so a caller
    # can send any of them and route to this same model). ``family``
    # is the upstream physical model identity (same model regardless
    # of which provider serves it); ``variant`` is the route flavour
    # ("web", "openrouter", "direct", etc.). Multiple entries with
    # the same family + different variants represent multi-route
    # access — pick whichever has the cost / latency profile you want.
    # All three default to safe sentinels so older proxies (no v2.1)
    # produce ModelEntry rows without raising.
    aliases: tuple[str, ...] = ()
    family: Optional[str] = None
    variant: Optional[str] = None


@dataclass(frozen=True)
class ProviderEntry:
    id: str
    name: str
    type: str
    priority: int
    cost_class: str  # "subscription" | "per_call"
    circuit: str
    regions: list[str]
    models: list[ModelEntry]


@dataclass(frozen=True)
class Snapshot:
    version: str
    as_of: str
    window_sec: int
    providers: list[ProviderEntry]
    etag: str

    def find_model(self, model_id: str) -> list[tuple[ProviderEntry, ModelEntry]]:
        """All (provider, model) pairs serving ``model_id``. Returned in
        snapshot order (which the proxy ships sorted by priority asc)."""
        out = []
        for p in self.providers:
            for m in p.models:
                if m.model_id == model_id:
                    out.append((p, m))
        return out


# ── Hint synthesis ────────────────────────────────────────────────────


def _format_hint(parts: dict[str, str]) -> str:
    """Format a dim dict into an RFC 8941 Structured Fields-style hint.

    The proxy's parser is lenient — both ``task=chat;require`` and
    ``task=chat`` are accepted. We emit the simpler form and let
    operator-tagged ``;require`` come from caller-supplied additions.
    """
    return ", ".join(f"{k}={v}" for k, v in parts.items() if v)


def _provider_hint_for_family(family: str) -> Optional[str]:
    """Translate a model-family preference into the proxy's
    ``provider-hint`` dim values. Caller says "claude" → we send
    a list that covers all the providers that can serve claude-*.
    """
    fam = family.lower()
    if fam in ("claude", "anthropic"):
        return "anthropic|claude-oauth|anthropic-oauth"
    if fam in ("openai", "gpt"):
        return "openai|codex-oauth"
    if fam in ("gemini", "google"):
        return "google|vertex"
    if fam == "grok":
        return "grok|grok-web"
    if fam in ("cohere", "embed"):
        return "cohere"
    return None  # Unknown family — let the proxy decide


# ── Client ─────────────────────────────────────────────────────────────


class LmrhClient:
    """Polls /lmrh/providers in a background thread, exposes a
    thread-safe snapshot accessor + hint synthesizer.

    The background loop uses a sync httpx client (simpler than asyncio
    glue for callers that aren't already async). One thread per client
    instance; cheap.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.poll_interval = poll_interval_sec
        self.timeout = timeout
        self._snap: Optional[Snapshot] = None
        self._snap_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._supported = True  # set False on 404 → degrade gracefully

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        """Spawn the background polling thread. Idempotent."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="LmrhClient.poll",
            daemon=True,
        )
        self._thread.start()
        # Block-wait for the first snapshot so callers don't hit a None
        # immediately after start(). 5s ceiling; if proxy is slow, we
        # return anyway and synthesize without data.
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if self._snap is not None:
                break
            time.sleep(0.1)

    def stop(self) -> None:
        """Signal the background loop to exit. Blocks up to 2s."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ── Polling ──────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        last_etag: Optional[str] = None
        while not self._stop.is_set():
            try:
                last_etag = self._poll_once(last_etag)
            except Exception as e:
                logger.warning("lmrh poll failed: %s", e)
            self._stop.wait(self.poll_interval)

    def _poll_once(self, last_etag: Optional[str]) -> Optional[str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if last_etag:
            headers["If-None-Match"] = last_etag
        with httpx.Client(timeout=self.timeout) as client:
            r = client.get(
                f"{self.base_url}/lmrh/providers",
                headers=headers,
            )
        if r.status_code == 404:
            # Proxy doesn't support v2 (or it's flag-disabled). Don't
            # log on every poll; once is enough.
            if self._supported:
                logger.info("lmrh v2 not available at %s", self.base_url)
                self._supported = False
            return last_etag
        if r.status_code == 304:
            return last_etag  # snapshot unchanged
        if r.status_code == 429:
            # Rate-limit hit — back off temporarily by skipping this poll
            logger.debug("lmrh poll rate-limited; will retry next cycle")
            return last_etag
        if not r.is_success:
            logger.warning("lmrh poll HTTP %d", r.status_code)
            return last_etag
        new_snap = _snapshot_from_dict(r.json(), r.headers.get("etag", ""))
        with self._snap_lock:
            self._snap = new_snap
        return new_snap.etag

    # ── Accessors ────────────────────────────────────────────────

    def snapshot(self) -> Optional[Snapshot]:
        """Latest snapshot, or None if not yet polled / proxy doesn't
        support v2."""
        with self._snap_lock:
            return self._snap

    def is_supported(self) -> bool:
        """Whether the proxy has reported a working v2 surface. False
        when the proxy returned 404 on first poll. Callers can use
        this to skip hint synthesis entirely and pass user input
        through verbatim."""
        return self._supported

    # ── Hint synthesis ───────────────────────────────────────────

    def build_hint(
        self,
        *,
        task: Optional[str] = None,
        prefer: Optional[str] = None,
        model_family: Optional[str] = None,
        region: Optional[str] = None,
        require_tools: bool = False,
        require_vision: bool = False,
        cache: Optional[str] = None,
        extra: Optional[dict[str, str]] = None,
    ) -> str:
        """Synthesize an LLM-Hint header value.

        Args:
            task: One of ``"chat"``, ``"reasoning"``, ``"summarize"``,
                ``"code"``, etc.
            prefer: ``"cheapest"`` → adds ``cost=economy``.
                ``"fastest"`` → adds ``latency=interactive``.
                ``"most_reliable"`` → derived: picks the highest
                ``success_rate`` provider visible in the snapshot
                and pins via ``provider-hint``.
            model_family: ``"claude"``, ``"openai"``, etc. Translates
                to a multi-value ``provider-hint``.
            region: ``"us"``, ``"eu"``, ``"asia"`` — pinned via the
                ``region`` dim with ``;require``.
            require_tools / require_vision: capability gates.
            cache: ``"ephemeral"`` / ``"none"`` — LMRH 1.2 §E2.
            extra: any additional dim=value pairs the caller wants to
                stamp directly. Last-write-wins over synthesized values.

        Returns the formatted hint string. Empty string when nothing
        was synthesized.
        """
        parts: dict[str, str] = {}
        if task:
            parts["task"] = task
        if prefer == "cheapest":
            parts["cost"] = "economy"
        elif prefer == "fastest":
            parts["latency"] = "interactive"
        elif prefer == "most_reliable":
            best = self._most_reliable_provider()
            if best:
                parts["provider-hint"] = best.id
        if model_family:
            ph = _provider_hint_for_family(model_family)
            if ph:
                parts["provider-hint"] = ph
        if region:
            parts["region"] = f"{region};require"
        if require_tools:
            parts["tools"] = "required"
        if require_vision:
            parts["vision"] = "required"
        if cache:
            parts["cache"] = cache
        if extra:
            parts.update(extra)
        return _format_hint(parts)

    def _most_reliable_provider(self) -> Optional[ProviderEntry]:
        """Return the provider with the highest success_rate × samples
        (samples weight prevents 1.0 with 1 sample beating 0.99 with
        500 samples). Returns None if no snapshot yet."""
        snap = self.snapshot()
        if snap is None or not snap.providers:
            return None
        best: Optional[ProviderEntry] = None
        best_score = -1.0
        for p in snap.providers:
            if p.circuit != "closed":
                continue
            for m in p.models:
                if m.metrics.success_rate is None or m.metrics.samples < 5:
                    continue
                score = m.metrics.success_rate * (
                    1 + min(m.metrics.samples, 1000) / 1000.0
                )
                if score > best_score:
                    best_score = score
                    best = p
        return best


# ── Wire-format → typed snapshot conversion ───────────────────────────


def _snapshot_from_dict(data: dict, etag: str) -> Snapshot:
    """Parse a /lmrh/providers JSON response into the typed Snapshot."""
    providers = []
    for pd in data.get("providers", []):
        models = []
        for md in pd.get("models", []):
            mm = md.get("metrics", {}) or {}
            models.append(ModelEntry(
                model_id=md.get("model_id", ""),
                kind=md.get("kind", "chat"),
                context_length=md.get("context_length"),
                native_tools=bool(md.get("native_tools", False)),
                native_reasoning=bool(md.get("native_reasoning", False)),
                metrics=ModelMetrics(
                    cost_per_1m_input_usd=mm.get("cost_per_1m_input_usd"),
                    cost_per_1m_output_usd=mm.get("cost_per_1m_output_usd"),
                    rated_quota_per_1m_input_usd=mm.get("rated_quota_per_1m_input_usd"),
                    latency_p50_ms=mm.get("latency_p50_ms"),
                    latency_p95_ms=mm.get("latency_p95_ms"),
                    ttft_p50_ms=mm.get("ttft_p50_ms"),
                    ttft_p95_ms=mm.get("ttft_p95_ms"),
                    success_rate=mm.get("success_rate"),
                    samples=int(mm.get("samples", 0) or 0),
                    # v3.3.4+: optional probe channel. .get() returns None
                    # when the proxy is older and doesn't emit these
                    # fields — SDK degrades gracefully, no version pin.
                    probe_success_rate=mm.get("probe_success_rate"),
                    probe_samples=int(mm.get("probe_samples", 0) or 0),
                ),
                # v3.5.0+: optional model-identity fields. Older proxies
                # (<= LMRHv2.0) omit these and the SDK applies defaults;
                # callers that don't use family/variant just see the
                # canonical model_id + an empty aliases tuple.
                aliases=tuple(md.get("aliases") or ()),
                family=md.get("family"),
                variant=md.get("variant"),
            ))
        providers.append(ProviderEntry(
            id=pd.get("id", ""),
            name=pd.get("name", ""),
            type=pd.get("type", ""),
            priority=int(pd.get("priority", 99)),
            cost_class=pd.get("cost_class", "per_call"),
            circuit=pd.get("circuit", "closed"),
            regions=list(pd.get("regions", [])),
            models=models,
        ))
    return Snapshot(
        version=data.get("version", "2.0"),
        as_of=data.get("as_of", ""),
        window_sec=int(data.get("window_sec", 3600)),
        providers=providers,
        etag=etag,
    )
