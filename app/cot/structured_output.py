"""Wave 5 #24 — Structured-output JSON-Schema enforcement with repair loop.

When the caller sends response_format or a json_schema (OpenAI) /
output_format (Anthropic), and the selected provider doesn't natively
guarantee schema conformance, we:

  1. Inject the schema as a strict system-prompt supplement.
  2. Call the model once.
  3. Parse the response as JSON, validate against the schema.
  4. On validation failure: inject the error back as a user turn and
     retry. Max `max_repairs` attempts (default 2).

Streaming isn't supported here — structured-output callers typically
consume JSON in one shot anyway.

All code assumes `litellm` is importable; failures fall back to the
un-validated answer rather than raising, so a broken provider doesn't
360° the endpoint.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


# ── Schema extraction ────────────────────────────────────────────────────────


def extract_openai_schema(body: dict) -> Optional[dict]:
    """Pull a JSON Schema out of an OpenAI-format request body.

    Supports:
      response_format = {"type": "json_object"}                  → None (just "be JSON")
      response_format = {"type": "json_schema", "json_schema": {"schema": {...}, "name": "..."}}
      response_format = {"type": "json_schema", "json_schema": {"schema": {...}, "strict": True}}
    """
    rf = body.get("response_format")
    if not isinstance(rf, dict):
        return None
    if rf.get("type") == "json_schema":
        js = rf.get("json_schema") or {}
        if isinstance(js, dict):
            schema = js.get("schema")
            if isinstance(schema, dict):
                return schema
    if rf.get("type") == "json_object":
        # OpenAI "json_object" mode — no schema, just "must be valid JSON"
        return {"type": "object"}
    return None


def extract_anthropic_schema(body: dict) -> Optional[dict]:
    """Anthropic doesn't have a standardised response_format field; users
    typically use tool-use with a single tool whose input_schema is the
    desired output shape. We treat that pattern as "structured output"
    when the request has exactly one tool and tool_choice forces its use.
    """
    tools = body.get("tools")
    if not isinstance(tools, list) or len(tools) != 1:
        return None
    tool_choice = body.get("tool_choice") or {}
    if not isinstance(tool_choice, dict):
        return None
    if tool_choice.get("type") not in ("tool", "any"):
        return None
    schema = tools[0].get("input_schema")
    return schema if isinstance(schema, dict) else None


# ── Response parse + validate ────────────────────────────────────────────────


def extract_json(text: str) -> Optional[dict]:
    """Extract a JSON object from possibly-fenced, possibly-prose model output."""
    if not text:
        return None
    # Try direct
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    # Try fenced blocks
    m = _FENCE_RE.search(stripped)
    if m:
        try:
            return json.loads(m.group(1))
        except (ValueError, TypeError):
            pass
    # Try the widest brace span
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(stripped[start:end + 1])
        except (ValueError, TypeError):
            pass
    return None


def validate_against_schema(obj, schema: dict) -> Optional[str]:
    """Return None on pass, else a short human-readable error for repair feedback."""
    try:
        import jsonschema
    except ImportError:
        return None  # library missing → trust the model
    try:
        jsonschema.validate(obj, schema)
        return None
    except jsonschema.ValidationError as e:
        path = "/".join(str(p) for p in e.absolute_path) if e.absolute_path else "<root>"
        return f"at {path}: {e.message[:200]}"
    except jsonschema.SchemaError as e:
        logger.warning("structured_output.invalid_schema %s", e)
        return None
    except Exception as e:
        logger.warning("structured_output.validate_failed %s", e)
        return None


# ── Prompt injection ─────────────────────────────────────────────────────────


def build_schema_system_prompt(schema: dict) -> str:
    schema_text = json.dumps(schema, indent=2)[:2000]
    return (
        "You MUST respond with a SINGLE JSON object that conforms to this schema.\n"
        "Do not wrap the JSON in markdown fences. Do not emit any prose before or after.\n"
        "Do not include fields not present in the schema unless they are explicitly allowed.\n\n"
        "Schema:\n"
        f"{schema_text}"
    )


def build_repair_prompt(previous_output: str, validation_error: str) -> str:
    return (
        "Your previous response did NOT conform to the required JSON schema.\n"
        f"Validation error: {validation_error}\n\n"
        "Your previous output was:\n"
        f"{previous_output[:1500]}\n\n"
        "Produce a corrected JSON object that conforms to the schema. Again, "
        "emit ONLY the JSON — no fences, no prose."
    )


# ── Top-level loop ───────────────────────────────────────────────────────────


async def call_with_schema(
    *,
    model: str,
    messages: list[dict],
    schema: dict,
    extra: dict,
    max_repairs: int = 2,
    system_prefix: Optional[str] = None,
) -> tuple[Optional[dict], str, int]:
    """Call the model enforcing `schema`. Returns (parsed_obj_or_None, raw_text, attempts).

    `parsed_obj_or_None` is the validated dict on success, None when validation
    failed on every attempt (caller should decide whether to surface the raw
    text or return 4xx).
    """
    from app.routing.retry import acompletion_with_retry

    schema_system = build_schema_system_prompt(schema)
    working_system = (system_prefix + "\n\n" + schema_system) if system_prefix else schema_system

    working_messages = [{"role": "system", "content": working_system}] + list(messages)
    kwargs = {k: v for k, v in extra.items() if k not in ("system", "stream", "response_format", "tools", "tool_choice")}

    last_text = ""
    for attempt in range(1, max_repairs + 2):  # 1 initial + max_repairs retries
        try:
            resp = await acompletion_with_retry(
                model=model, messages=working_messages, stream=False, **kwargs,
            )
        except Exception as exc:
            logger.warning("structured_output.call_failed attempt=%d %s", attempt, exc)
            raise

        last_text = resp.choices[0].message.content or ""
        parsed = extract_json(last_text)
        if parsed is None:
            err = "response was not parseable as JSON"
        else:
            err = validate_against_schema(parsed, schema)
            if err is None:
                return parsed, last_text, attempt

        if attempt > max_repairs:
            break

        # Feed the validation error back for a repair attempt.
        repair_user = build_repair_prompt(last_text, err)
        working_messages = working_messages + [
            {"role": "assistant", "content": last_text},
            {"role": "user", "content": repair_user},
        ]

    # All attempts exhausted — return None for parsed, let caller decide
    return None, last_text, max_repairs + 1
