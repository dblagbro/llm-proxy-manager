"""v4.4.31 — Cursor subscription provider helpers.

Lightweight shape-detection + credential parsing for the
``cursor-oauth`` provider type. The real chat dispatch is delegated
to the Cursor-To-OpenAI sidecar (see ``app/api/_messages_dispatch.py``
where ``cursor-oauth`` routes through the standard OpenAI path with
``base_url`` pointing at the sidecar).

This module mirrors the ``codex_oauth`` / ``claude_oauth`` pair —
the OAuth flow lives in ``cursor_oauth_flow.py``; this module holds
the token-shape detector + paste-fallback parser.

Token shape: ``user_<id_segment>::<JWT>`` (Cursor's deep-link login
issues a cookie of this form). The JWT is ``HS256`` signed by Cursor
and carries the user id + plan tier in its payload; we don't decode
it (no claims we need at v1 — chat dispatch just forwards the cookie
as the OpenAI Bearer header into the sidecar, which converts to the
proper ConnectRPC headers for Cursor's backend).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional


TOKEN_PREFIX = "user_"


@dataclass
class CursorOAuthCredentials:
    access_token: str
    refresh_token: Optional[str] = None
    expires_at: Optional[float] = None


class CredentialParseError(ValueError):
    """Raised by parse_credentials when the blob can't be interpreted."""


def normalize_messages_for_bridge(messages: list) -> list:
    """v5.3.6 — emulate OpenAI Chat Completions tolerance for messages
    whose ``content`` is a list (multimodal / parts shape).

    The JiuZ-Chn ``cursor-to-openai`` bridge enforces ``content: string``
    strictly — anything else is rejected with::

        Error: request.messages.content: string expected
        at generateCursorBody (src/utils/utils.js:79)

    Real OpenAI accepts either ``string`` OR ``list[part]``. To keep
    callers (claude-cli, opencode-bin, our own coordinator-hub bot)
    working against cursor-oauth providers without forcing every caller
    to know about the bridge's stricter shape, this helper coerces
    every ``role:*`` message's ``content`` list to a single joined
    string before dispatch.

    Coercion rules:
    - ``str`` → unchanged
    - ``list`` of typed parts:
      - ``{type:"text", text:...}`` (OpenAI) → ``text`` field
      - ``{type:"image_url", ...}`` (OpenAI) → ``[image]`` placeholder
      - ``{type:"input_text"|"output_text", text:...}`` (Responses
        shape) → ``text`` field
      - other shapes are stringified via ``json.dumps`` so the bridge
        sees SOMETHING, never silently dropped.
    - empty / falsy list → empty string (the cursor-oauth
      ``_oauth_chat_translate`` path stamps a placeholder upstream so
      this rarely fires)
    - ``None`` → unchanged (assistant tool_calls turns legitimately
      carry ``content: null``)

    This is PROVIDER-ADAPTER emulation scope — we're matching the
    bridge's specific shape constraint, NOT silently rewriting caller
    intent. The text content is preserved verbatim; only the SHAPE
    changes.
    """
    import json as _json
    if not isinstance(messages, list):
        return messages
    out: list = []
    for m in messages:
        if not isinstance(m, dict):
            out.append(m)
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        parts: list[str] = []
        for blk in content:
            if isinstance(blk, str):
                parts.append(blk)
                continue
            if not isinstance(blk, dict):
                parts.append(_json.dumps(blk))
                continue
            t = blk.get("type")
            if t in ("text", "input_text", "output_text", "summary_text"):
                txt = blk.get("text") or ""
                if txt:
                    parts.append(txt)
            elif t == "image_url" or t == "image":
                parts.append("[image]")
            elif t == "refusal":
                refusal_text = blk.get("refusal") or ""
                if refusal_text:
                    parts.append(f"[refusal: {refusal_text}]")
            else:
                # Unknown shape — preserve via JSON dump so the bridge
                # sees SOMETHING. Never silently drop, so a future block
                # type addition (proxy or upstream) surfaces in the
                # bridge's request log instead of vanishing.
                parts.append(_json.dumps(blk))
        new_msg = dict(m)
        new_msg["content"] = "\n".join(parts)
        out.append(new_msg)
    return out


def looks_like_cursor_token(s: str) -> bool:
    """Cheap shape-test the dispatcher uses to flag a malformed
    Provider.api_key before sending it upstream. Cursor cookies look
    like ``user_01ABC...::eyJ...``. We accept the JWT-only form too for
    paste-fallback when an operator drops the ``user_`` segment by
    accident — the sidecar will reject it with a clear error if so.
    """
    if not s:
        return False
    if s.startswith(TOKEN_PREFIX) and "::" in s:
        return True
    # Bare JWT — three dot-separated segments
    if s.count(".") == 2 and s.startswith("eyJ"):
        return True
    return False


def parse_credentials(raw: str) -> CursorOAuthCredentials:
    """Accept either a bare ``user_xxx::<JWT>`` cookie or a JSON blob
    that wraps one. Used by the providers_oauth paste-fallback path
    (``oauth_credentials_blob``). Mirrors codex_oauth.parse_credentials
    shape.
    """
    raw = (raw or "").strip()
    if not raw:
        raise CredentialParseError("Empty credentials")

    # JSON blob — accept a few shapes operators might paste:
    #   { "access_token": "user_..." }
    #   { "accessToken": "user_..." }    (sidecar's response field name)
    #   { "tokens": { "access_token": "user_..." } }
    if raw.startswith("{"):
        try:
            j = json.loads(raw)
        except json.JSONDecodeError as e:
            raise CredentialParseError(f"Couldn't parse JSON: {e}") from None
        if not isinstance(j, dict):
            raise CredentialParseError("JSON root must be an object")
        tok = (
            j.get("access_token")
            or j.get("accessToken")
            or (j.get("tokens") or {}).get("access_token")
            or (j.get("tokens") or {}).get("accessToken")
        )
        if not isinstance(tok, str) or not tok:
            raise CredentialParseError(
                "JSON didn't contain access_token / accessToken"
            )
        return CursorOAuthCredentials(access_token=tok)

    # Bare token
    if looks_like_cursor_token(raw):
        return CursorOAuthCredentials(access_token=raw)

    raise CredentialParseError(
        "Doesn't look like a Cursor cookie. Expected ``user_<id>::<JWT>`` "
        "or a JSON blob containing ``access_token``. Run the deep-link "
        "exchange via the Generate Auth URL button if you don't have one yet."
    )
