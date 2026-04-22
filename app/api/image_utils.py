"""Image detection and stripping utilities for both Anthropic and OpenAI wire formats."""


def has_images_anthropic(messages: list[dict]) -> bool:
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image", "image_url"):
                    return True
    return False


def strip_images_anthropic(messages: list[dict]) -> list[dict]:
    """Replace Anthropic-format image blocks with text placeholders."""
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


def has_images_openai(messages: list[dict]) -> bool:
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image_url":
                    return True
    return False


def strip_images_openai(messages: list[dict]) -> list[dict]:
    """Replace OpenAI-format image_url content items with text placeholders."""
    out = []
    for msg in messages:
        content = msg.get("content", "")
        if not isinstance(content, list):
            out.append(msg)
            continue
        new_parts = []
        for part in content:
            if not isinstance(part, dict):
                new_parts.append(part)
                continue
            if part.get("type") == "image_url":
                new_parts.append({"type": "text", "text": "[Image — not supported by this provider]"})
            else:
                new_parts.append(part)
        out.append({**msg, "content": new_parts})
    return out
