"""v5.14.0 — Callback registry + runner for response-shaping hooks.

Implements the hub-team Tier 1 ask from the 2026-06-30
peer-comparison-roadmap memo. Inspired by:

- LiteLLM's 6-hook lifecycle (``async_pre_call_hook``,
  ``async_post_call_*``, etc.).
- Portkey's typed ``PluginHandler`` contract.

What we ship in v5.14.0:

- The runner (``apply_response_hooks``) and registry
  (``register_hook`` / ``registered_hooks``).
- One built-in hook: ``compliance_substitution_header_hook``,
  migrated from the inline emission code that lived in
  ``app/api/_compliance_handler.py`` since v5.9.3.

What we LEAVE for follow-up ships:

- ``pre_call`` hooks (request mutation / veto). Reserve the slot in
  the protocol so v5.14.1 can drop it in without breaking the
  registry shape.
- Async hooks (current registry expects sync functions returning a
  dict-merge contribution; async would need a small adapter).
- Hub-managed hot-reload directory (settings-file path import is
  the v5.14.0 mechanism).

Footgun mitigations from the 2026-06-30 hub-team memo:

- **LiteLLM /v1/messages bypass class** — the static-grep test
  ``test_v5140_hook_runner_pins_all_endpoints`` pins every
  model-resolving endpoint to a call into ``apply_response_hooks``
  (either directly or via ``_compliance_handler``). Failure-mode is
  test-break-at-merge, not runtime-bypass.
- **Portkey fail-open default** — our default is fail-closed:
  a misbehaving hook produces ``X-Hook-Failure: <name>:<reason>``
  on the response AND, if ``callbacks.fail_closed`` is on (default
  True), gets registered as a degraded hook so it stops firing
  until manually re-enabled.
- **Hook ordering ambiguity** — registration order is execution
  order; an explicit ``priority`` keyword overrides for inter-hook
  dependencies. Stable sort.
- **Per-hook timeout** — default 2s; configurable per-hook. Each
  hook runs inside ``asyncio.wait_for`` with the timeout; timeout
  treated as failure.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Protocol

logger = logging.getLogger(__name__)


class ResponseHookFn(Protocol):
    """A response-shaping hook. Implementations receive the partial
    ``resp_headers`` dict the endpoint is about to send + a small
    context object describing the request + the served route, and
    return either:

    - ``None`` (no change), or
    - a ``dict[str, str]`` of headers to merge into the response.

    Async to match the host endpoint code; sync hooks can wrap their
    body in an ``async def``.
    """

    async def __call__(
        self,
        *,
        handler_id: str,
        resp_headers: dict,
        context: "HookContext",
    ) -> Optional[dict]: ...


@dataclass
class HookContext:
    """Per-request context passed to each hook. Adding fields is
    backwards-compatible (hooks ignore unknown keys via **kwargs); the
    initial set is the minimum hub-side substitution-mirror needs.

    Hub team's 2026-06-30 ask listed: ``requested_model``,
    ``served_model``, ``api_key_id``, ``provider_id``,
    ``compliance_event_id``. Plus ``substituted`` because the built-in
    substitution hook needs it to decide between the three emission
    values (true / false / pass-through).
    """
    requested_model: Optional[str] = None
    served_model: Optional[str] = None
    api_key_id: Optional[str] = None
    provider_id: Optional[str] = None
    compliance_event_id: Optional[str] = None
    substituted: bool = False
    key_record: Any = None  # ORM Provider/ApiKey for hooks that need it
    request: Any = None     # Starlette Request for hooks that need raw headers
    extra: dict = field(default_factory=dict)


@dataclass
class _RegisteredHook:
    name: str
    fn: ResponseHookFn
    priority: int
    timeout_sec: float
    degraded: bool = False
    consecutive_failures: int = 0


_REGISTRY: list[_RegisteredHook] = []
_DEGRADE_AFTER_N_FAILURES = 5


def register_hook(
    name: str,
    fn: ResponseHookFn,
    *,
    priority: int = 0,
    timeout_sec: float = 2.0,
) -> None:
    """Register a response-shaping hook. Idempotent on ``name``:
    re-registering replaces the existing entry (lets hub team hot-
    swap implementations without restarting). Stable sort by
    ``priority`` ascending, ties broken by registration order."""
    global _REGISTRY
    for i, existing in enumerate(_REGISTRY):
        if existing.name == name:
            _REGISTRY[i] = _RegisteredHook(
                name=name, fn=fn, priority=priority,
                timeout_sec=timeout_sec,
            )
            _REGISTRY.sort(key=lambda h: h.priority)
            return
    _REGISTRY.append(_RegisteredHook(
        name=name, fn=fn, priority=priority, timeout_sec=timeout_sec,
    ))
    _REGISTRY.sort(key=lambda h: h.priority)


def unregister_hook(name: str) -> bool:
    """Drop a hook by name. Returns True if found + removed."""
    global _REGISTRY
    before = len(_REGISTRY)
    _REGISTRY = [h for h in _REGISTRY if h.name != name]
    return len(_REGISTRY) < before


def registered_hooks() -> list[dict]:
    """Read-only snapshot for tests + admin UI. Doesn't expose ``fn``
    because Callable objects aren't useful in JSON."""
    return [{
        "name": h.name,
        "priority": h.priority,
        "timeout_sec": h.timeout_sec,
        "degraded": h.degraded,
        "consecutive_failures": h.consecutive_failures,
    } for h in _REGISTRY]


def reset_registry_for_tests() -> None:
    """Drop every registered hook. Test fixtures use this between
    cases so a hook from test_A doesn't leak into test_B."""
    global _REGISTRY
    _REGISTRY = []


def _parse_hooks_override_header(
    value: Optional[str],
) -> tuple[set[str], set[str]]:
    """Parse the ``X-Hooks-Override`` request header.

    Format: comma-separated tokens, each starting with ``+`` (force-
    enable) or ``-`` (force-disable). Whitespace tolerated. Unknown
    prefix (no leading + or -) is silently skipped so a caller can
    prepend a comment token like ``+ debug`` without breaking the
    parse. Never raises — the header is best-effort per-request
    tuning, not a contract-level input.

    Returns:
        (force_enabled_names, force_disabled_names)

    Example:
        >>> _parse_hooks_override_header("+refusal_debug, -compliance_substitution_callback_hook")
        ({"refusal_debug"}, {"compliance_substitution_callback_hook"})
    """
    force_enabled: set[str] = set()
    force_disabled: set[str] = set()
    if not value or not isinstance(value, str):
        return force_enabled, force_disabled
    for token in value.split(","):
        token = token.strip()
        if not token:
            continue
        if token.startswith("+"):
            name = token[1:].strip()
            if name:
                force_enabled.add(name)
        elif token.startswith("-"):
            name = token[1:].strip()
            if name:
                force_disabled.add(name)
    return force_enabled, force_disabled


def _extract_hooks_override(context: "HookContext") -> tuple[set[str], set[str]]:
    """Pull the override header off the request and gate on the key's
    ``debug_echo_enabled`` flag. Returns empty sets when:

    - No request is threaded onto the context (unusual — non-HTTP path)
    - No ``X-Hooks-Override`` header is present
    - The API key does not have ``debug_echo_enabled=True``

    The gate is important: without it, any caller could disable
    compliance-related hooks per-request. Binding to ``debug_echo_enabled``
    matches the existing v5.8.x sandbox/validation-key intent — operator
    explicitly opts a key into debug-mode features.
    """
    request = getattr(context, "request", None)
    if request is None:
        return set(), set()
    try:
        raw = request.headers.get("x-hooks-override") or request.headers.get(
            "X-Hooks-Override"
        )
    except Exception:
        return set(), set()
    if not raw:
        return set(), set()
    key_record = getattr(context, "key_record", None)
    if key_record is None or not getattr(key_record, "debug_echo_enabled", False):
        # Header present but gate not passed — log at info so operator
        # sees the attempted override in monitoring, but don't 4xx (the
        # main request path should still succeed with default hook
        # behavior).
        logger.info(
            "response_hook.override_ignored_no_debug_echo api_key_id=%s "
            "hooks_override_header_present=1 (v5.20.3)",
            getattr(key_record, "id", None),
        )
        return set(), set()
    return _parse_hooks_override_header(raw)


async def apply_response_hooks(
    *,
    handler_id: str,
    resp_headers: dict,
    context: HookContext,
) -> dict:
    """Iterate the registry, run each hook, merge contributions into
    ``resp_headers``. Mutates ``resp_headers`` in place AND returns it
    (callers can ignore the return value).

    Failure semantics: per-hook timeout (default 2s) + per-hook
    exception → emit ``X-Hook-Failure: <name>:<short_reason>`` and
    increment ``consecutive_failures``. After
    ``_DEGRADE_AFTER_N_FAILURES`` (5) consecutive failures the hook
    is marked degraded and skipped until something re-registers it.

    v5.20.3 — per-request hook override via ``X-Hooks-Override`` request
    header. Format: ``+hook_name`` (force-enable a degraded hook for
    this request only) / ``-hook_name`` (force-disable an otherwise-
    healthy hook). Comma-separated. Requires the API key to have
    ``debug_echo_enabled=True`` — matches the sandbox/validation-key
    convention from v5.8.x. Emits ``X-Hooks-Applied`` (comma-separated
    list of hooks that actually ran) and ``X-Hooks-Skipped`` (list of
    hooks that were force-disabled) on the response for observability.
    Ported pattern from ccproxy (2026-06-30 peer-comparison-roadmap).
    """
    if not _REGISTRY:
        return resp_headers
    _force_enabled, _force_disabled = _extract_hooks_override(context)
    _applied: list[str] = []
    _skipped_by_override: list[str] = []
    for hook in list(_REGISTRY):
        if hook.name in _force_disabled:
            _skipped_by_override.append(hook.name)
            continue
        if hook.degraded and hook.name not in _force_enabled:
            continue
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                hook.fn(
                    handler_id=handler_id,
                    resp_headers=resp_headers,
                    context=context,
                ),
                timeout=hook.timeout_sec,
            )
            if isinstance(result, dict):
                resp_headers.update(result)
            hook.consecutive_failures = 0
            _applied.append(hook.name)
        except asyncio.TimeoutError:
            hook.consecutive_failures += 1
            elapsed_ms = int((time.time() - t0) * 1000)
            resp_headers["X-Hook-Failure"] = (
                f"{hook.name}:timeout:{elapsed_ms}ms"
            )
            logger.warning(
                "response_hook.timeout name=%s elapsed_ms=%d",
                hook.name, elapsed_ms,
            )
            if hook.consecutive_failures >= _DEGRADE_AFTER_N_FAILURES:
                hook.degraded = True
                logger.warning(
                    "response_hook.degraded name=%s — re-register to re-enable",
                    hook.name,
                )
        except Exception as exc:
            hook.consecutive_failures += 1
            resp_headers["X-Hook-Failure"] = (
                f"{hook.name}:exception:{type(exc).__name__}"
            )
            logger.warning(
                "response_hook.exception name=%s err=%s",
                hook.name, exc,
            )
            if hook.consecutive_failures >= _DEGRADE_AFTER_N_FAILURES:
                hook.degraded = True
                logger.warning(
                    "response_hook.degraded name=%s — re-register to re-enable",
                    hook.name,
                )
    # v5.20.3 — observability headers so the operator can see exactly
    # which hooks contributed to the response. Only emitted when the
    # override header was honored (i.e., the sandbox key gate passed);
    # otherwise the response is identical to pre-v5.20.3.
    if _force_enabled or _force_disabled:
        if _applied:
            resp_headers["X-Hooks-Applied"] = ",".join(_applied)
        if _skipped_by_override:
            resp_headers["X-Hooks-Skipped"] = ",".join(_skipped_by_override)
    return resp_headers


# ── Built-in hook registration -- called at startup ─────────────────


def register_builtin_hooks() -> None:
    """Wire the in-tree hooks at boot. Called from ``app/main.py``
    lifespan. Hub-registered hooks are loaded separately and AFTER
    these (so a hub-side substitution-mirror sees the built-in
    contribution and can append to it)."""
    from app.compliance.substitution_hook import (
        compliance_substitution_header_hook,
    )
    register_hook(
        "compliance_substitution_header_hook",
        compliance_substitution_header_hook,
        priority=0,
        timeout_sec=2.0,
    )
    # v5.18.0 — outbound POST to hub's substitution-callback receiver.
    # Fires ONLY when context.substituted is True. No-op when the URL
    # setting is empty (default). Timeout is per-attempt inside the
    # hook (2s httpx.AsyncClient); the outer hook-runner timeout wraps
    # the whole thing to bound blast-radius on the response path.
    from app.compliance.substitution_callback_hook import (
        compliance_substitution_callback_hook,
    )
    register_hook(
        "compliance_substitution_callback_hook",
        compliance_substitution_callback_hook,
        priority=10,  # runs AFTER the header hook so the response is stamped first
        timeout_sec=5.5,  # 2s + 1s + 2s = 5s worst case; 0.5s slack
    )
