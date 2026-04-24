"""Image detection and stripping utilities for both Anthropic and OpenAI wire formats."""


def _has_blocks_of_type(messages: list[dict], block_types: tuple[str, ...]) -> bool:
    for m in messages:
        content = m.get("content", "")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") in block_types:
                return True
    return False


def _strip_blocks_of_type(
    messages: list[dict], block_type: str, placeholder: dict,
) -> list[dict]:
    """Replace every block whose `type` == block_type with `placeholder`,
    preserving every other block unchanged. Non-list content is passed
    through untouched."""
    out = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_content = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == block_type:
                new_content.append(placeholder)
            else:
                new_content.append(block)
        out.append({**msg, "content": new_content})
    return out


# ── Anthropic (uses type="image" with a `source` block) ─────────────────────


def has_images_anthropic(messages: list[dict]) -> bool:
    # Accept both for robustness against mis-formatted clients
    return _has_blocks_of_type(messages, ("image", "image_url"))


def strip_images_anthropic(messages: list[dict]) -> list[dict]:
    """Replace Anthropic-format image blocks with a text placeholder.
    The placeholder carries the media type so the model knows an image
    was present without seeing bytes."""
    # We can't keep the media-type per-image since the placeholder is
    # pre-built; use a generic one (this matches prior behavior when the
    # media type was the only per-block info we preserved).
    # Use a callback-style strip that can inspect each block's source:
    out = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_blocks = []
        for block in content:
            if not isinstance(block, dict):
                new_blocks.append(block)
                continue
            if block.get("type") == "image":
                src = block.get("source", {})
                media = src.get("media_type", "image")
                new_blocks.append({"type": "text", "text": f"[Image: {media} — not supported by this provider]"})
            else:
                new_blocks.append(block)
        out.append({**msg, "content": new_blocks})
    return out


# ── OpenAI (uses type="image_url") ──────────────────────────────────────────


def has_images_openai(messages: list[dict]) -> bool:
    return _has_blocks_of_type(messages, ("image_url",))


def strip_images_openai(messages: list[dict]) -> list[dict]:
    """Replace OpenAI-format image_url content items with a text placeholder."""
    return _strip_blocks_of_type(
        messages, "image_url",
        {"type": "text", "text": "[Image — not supported by this provider]"},
    )
