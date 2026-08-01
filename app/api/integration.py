"""v5.8.0 / v5.8.1 — FastAPI routes for the AI integration protocol.

Public:
  - ``GET /announce`` — describes the proxy's capability surface.
  - ``POST /api/integration/chat`` — passphrase-gated AI-to-AI chat.

Admin (v5.8.1):
  - ``POST /api/admin/integration/rotate-passphrase`` — auto-generate
    and persist a new passphrase, return it once (operator copies).
  - ``GET /api/admin/integration/dev-handoff`` — returns a markdown
    package the operator can hand to a developer team integrating a
    new project.
"""
from __future__ import annotations

import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import AdminUser, require_admin
from app.models.database import get_db
from app.utils.disconnect_watchdog import watch_for_disconnect

logger = logging.getLogger(__name__)

router = APIRouter(tags=["integration"])

# v5.20.2 — self-update endpoint mounts here so it shares the tag
# group in /docs and the router prefix.
from app.integration.self_update import router as self_update_router
router.include_router(self_update_router)


@router.get("/announce")
async def announce():
    """Public capability document. No auth. Safe to expose."""
    from app.integration.announce import build_announce_payload
    return await build_announce_payload()


class IntegrationChatRequest(BaseModel):
    passphrase: str = Field(..., description="Shared secret for integration access")
    conversation_id: Optional[str] = Field(
        None, description="Session id; pass null on first turn"
    )
    project_name: str = Field(
        "unnamed", description="Name of the integrating project"
    )
    message: str = Field(..., description="Your message to the management AI")


@router.post("/api/integration/chat")
async def integration_chat(
    body: IntegrationChatRequest,
    request: Request,
    # v5.21.14 — db BEFORE _watchdog (LIFO cleanup closes get_db last, after
    # the watchdog stops). Prevents disconnect-cancel from leaking a pool slot
    # during session.close(). Same fix as cluster.py v5.21.12. Do NOT reorder.
    db: AsyncSession = Depends(get_db),
    _watchdog: None = Depends(watch_for_disconnect),
):
    """Passphrase-gated AI-to-AI chat. The management LLM may mint
    an API key via the ``create_api_key`` tool; the minted key is
    returned in ``provisioned``.

    Returns 401 if the integration is disabled OR the passphrase is
    wrong OR the passphrase isn't configured server-side (fail-closed
    so a misconfigured deploy isn't silently open)."""
    from app.integration.chat import handle_chat
    try:
        result = await handle_chat(
            db,
            passphrase=body.passphrase,
            conversation_id=body.conversation_id,
            project_name=body.project_name,
            message=body.message,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("integration_chat.unhandled err=%r", exc)
        raise HTTPException(500, f"Integration chat error: {exc}")
    return result


# ── v5.8.1 admin endpoints ────────────────────────────────────────────


@router.post("/api/admin/integration/rotate-passphrase")
async def rotate_passphrase(
    db: AsyncSession = Depends(get_db),
    _user: AdminUser = Depends(require_admin),
):
    """Generate a new passphrase, persist via the existing settings
    path (so it cluster-syncs), and return the plaintext ONCE.

    The operator clicks 'Rotate' in the UI; the new value renders in a
    modal with a copy button. Closing the modal forgets the plaintext;
    re-rotation is the only way to recover a lost passphrase."""
    new_pass = "integ-" + secrets.token_urlsafe(24)
    from app import config_runtime
    await config_runtime.persist(db, {"integration_passphrase": new_pass})
    logger.info("integration.passphrase_rotated by %s", _user.username)
    return {"passphrase": new_pass, "rotated_by": _user.username}


@router.get("/api/admin/integration/dev-handoff")
async def dev_handoff(
    request: Request,
    _user: AdminUser = Depends(require_admin),
):
    """Build a self-contained markdown package the operator can hand
    to a developer team. Contains: the announce + chat URLs (computed
    from the request's own scheme/host), the CURRENT passphrase, a
    Python integration sample, a curl sample, and limit reminders.

    The passphrase is rendered in plaintext in the response body — the
    operator is expected to copy/paste once and not store the response.
    UI button doesn't persist this anywhere; it's display-and-forget.
    """
    from app.config import settings
    # v5.8.3 — when the request comes through nginx with a sub-path
    # rewrite (e.g. /llm-proxy/ → /), FastAPI's request.base_url
    # reflects only the rewritten side. The X-Forwarded-Prefix
    # header (set by the upstream nginx block) carries the original
    # prefix so the dev-handoff URLs match the cluster the operator
    # actually wants to share — typically /llm-proxy/announce, not
    # the root-level /announce that v5.8.3 also exposes for short
    # URLs.
    base = str(request.base_url).rstrip("/")
    prefix = (request.headers.get("x-forwarded-prefix") or "").rstrip("/")
    announce_url = f"{base}{prefix}/announce"
    chat_url = f"{base}{prefix}/api/integration/chat"
    passphrase = settings.integration_passphrase or "(NOT SET — set via Settings → AI Integration first)"

    markdown = _build_dev_handoff_markdown(
        announce_url=announce_url,
        chat_url=chat_url,
        passphrase=passphrase,
        default_budget=settings.integration_default_daily_budget_usd,
        max_budget=settings.integration_max_daily_budget_usd,
        max_messages=settings.integration_max_messages_per_session,
    )
    return {
        "markdown": markdown,
        "announce_url": announce_url,
        "chat_url": chat_url,
        "passphrase": passphrase,
        "enabled": bool(settings.integration_enabled),
        "limits": {
            "default_daily_budget_usd": settings.integration_default_daily_budget_usd,
            "max_daily_budget_usd": settings.integration_max_daily_budget_usd,
            "max_messages_per_session": settings.integration_max_messages_per_session,
        },
    }


def _build_dev_handoff_markdown(
    *,
    announce_url: str,
    chat_url: str,
    passphrase: str,
    default_budget: float,
    max_budget: float,
    max_messages: int,
) -> str:
    """Render the markdown package. Pure function — easy to unit-test
    + the same renderer is used by the UI 'Copy' button."""
    return f"""# Integrating your AI project with llm-proxy v2

Welcome. The proxy exposes two URLs you need:

- **Capability discovery**: `{announce_url}` (no auth — read it first)
- **Integration negotiation**: `{chat_url}` (POST, passphrase-gated)

The first describes what's available. The second is where your AI
talks to our management AI to negotiate a real API key — capabilities
needed, daily budget, MCP tool policy, etc. The management AI mints
the key for you on agreement.

## Step 1 — read the announce document

```bash
curl -s {announce_url} | jq .
```

Pay attention to the `endpoints`, `routing_features`, `mcp_tools_available`,
and `integration.first_turn_guidance` sections.

## Step 2 — talk to the management AI

Pass our shared passphrase + a description of your project. The
management AI works best when you front-load everything on turn 1
(it will mint the key in one turn if it has enough info):

**Shared passphrase**: `{passphrase}`

(Keep this secret. Rotate at any time via the proxy admin UI.)

```python
import httpx, json

PASSPHRASE = "{passphrase}"  # see admin for current value
URL = "{chat_url}"

resp = httpx.post(URL, json={{
    "passphrase": PASSPHRASE,
    "project_name": "your-project-name",
    "message": (
        "I am building <one-line purpose>. AI use case: "
        "<chat | agent | embedding | tool-use>. I need <model class>. "
        "MCP tools: <yes inject all | yes inject these N | no — I have "
        "my own tool surface>. Expected volume <N> req/day. Daily "
        "budget $<N> (max ${max_budget:.2f})."
    ),
}}, timeout=120).json()

print(resp["response"])  # the management AI's reply
if resp["provisioned"]:
    api_key = resp["provisioned"]["api_key"]
    # Save it somewhere safe. It's only returned once.
```

Or with curl:

```bash
curl -s -X POST {chat_url} \\
  -H "Content-Type: application/json" \\
  -d '{{"passphrase": "{passphrase}", "project_name": "your-project", "message": "...front-loaded requirements..."}}'
```

## Step 3 — use the key

The minted key works as either header:

- `x-api-key: <key>` (Anthropic-compatible clients)
- `Authorization: Bearer <key>` (OpenAI-compatible clients)

Routes to `/v1/messages`, `/v1/chat/completions`, `/v1/responses`,
`/v1/embeddings`. See the announce doc for the full surface.

## Limits

| Limit | Value |
|---|---|
| Default daily budget | ${default_budget:.2f} |
| Max daily budget (hard cap) | ${max_budget:.2f} |
| Max chat messages per session | {max_messages} |

If you need a higher cap or admin-tier capabilities, the management
AI will refuse — escalate to the operator instead.

## Multi-turn (if turn 1 isn't enough)

The management AI may ask clarifying questions. Pass back the
`conversation_id` from the previous response to continue:

```python
resp2 = httpx.post(URL, json={{
    "passphrase": PASSPHRASE,
    "conversation_id": resp["conversation_id"],
    "project_name": "your-project-name",
    "message": "<answer to the management AI's question>",
}}).json()
```

Sessions older than 1 hour or with more than {max_messages} turns
are recycled — start fresh with `conversation_id: null`.

---

Generated for project handoff. Discard after use.
"""
