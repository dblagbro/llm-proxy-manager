"""Prompt task classifier (Wave 3 #15).

When the caller doesn't provide a `task=` hint in LMRH, classify the
request automatically by embedding the last user turn and comparing
(cosine) against pre-computed task exemplar vectors.

No new Docker dependency — reuses litellm.aembedding() with the same
text-embedding-3-small model used by semantic cache (Wave 1 #3).

Task vectors are computed lazily on first use and kept in-memory for
the life of the process. With 6 task vectors × 1536 dims × float32 ≈
36 KB, memory cost is negligible.
"""
from __future__ import annotations

import asyncio
import logging
import math
from typing import Optional

logger = logging.getLogger(__name__)


# Exemplar phrases used to compute per-task prototype vectors. Each task
# gets 3-5 diverse prompts; we average their embeddings to form the
# prototype. Keep the space small so we never blow the API budget on a
# one-time cold-start.
_TASK_EXEMPLARS: dict[str, list[str]] = {
    "code": [
        "Write a function in Python that reverses a linked list.",
        "How do I implement a JWT authentication middleware in Express?",
        "Debug this SQL query — it's returning duplicate rows.",
        "Refactor this React component to use hooks instead of a class.",
    ],
    "math": [
        "If a train travels 60 miles in 45 minutes, what's its speed in km/h?",
        "Solve for x: 3x² + 5x - 2 = 0.",
        "What is the probability of rolling two sixes with two fair dice?",
        "Calculate the compound interest on $10,000 at 5% over 7 years.",
    ],
    "reasoning": [
        "Given these constraints, is the argument logically valid?",
        "Walk through your reasoning step by step to reach the conclusion.",
        "Which option is most consistent with the evidence and why?",
        "Identify the hidden assumption in the following claim.",
    ],
    "summarize": [
        "Give me a two-paragraph summary of this article.",
        "TL;DR this meeting transcript.",
        "Summarize the key points for a busy executive.",
        "Condense this 10-page document into bullet points.",
    ],
    "analysis": [
        "What are the pros and cons of this architecture?",
        "Compare these three options and recommend one.",
        "Analyze the root cause of this production outage.",
        "What are the implications of this policy change?",
    ],
    "chat": [
        "Hi, how are you?",
        "Tell me something interesting.",
        "Thanks, that helped!",
        "Can you rephrase that more casually?",
    ],
}

# Per-process cache of prototype vectors, computed on first classify() call
_prototypes: dict[str, list[float]] = {}
_prototype_lock = asyncio.Lock()
# Cosine threshold below which we decline to auto-set a task (ambiguous case)
_MIN_CONFIDENCE = 0.25


async def _embed_many(texts: list[str], model: str, dims: int) -> list[list[float]]:
    """Embed a batch of strings. Returns empty list on provider error."""
    try:
        import litellm
        resp = await litellm.aembedding(model=model, input=texts, dimensions=dims)
        data = resp.data if not isinstance(resp.data, list) else resp.data
        return [list(d.embedding) if hasattr(d, "embedding") else list(d["embedding"]) for d in data]
    except Exception as exc:
        logger.warning("classifier.embed_failed %s", exc)
        return []


async def _build_prototypes(model: str, dims: int) -> None:
    """Populate _prototypes once — averaged exemplar vectors per task."""
    async with _prototype_lock:
        if _prototypes:
            return
        all_texts: list[str] = []
        spans: list[tuple[str, int, int]] = []  # (task, start, end) slices
        for task, exemplars in _TASK_EXEMPLARS.items():
            start = len(all_texts)
            all_texts.extend(exemplars)
            spans.append((task, start, len(all_texts)))
        vectors = await _embed_many(all_texts, model, dims)
        if len(vectors) != len(all_texts):
            logger.warning("classifier.prototype_build_failed got=%d expected=%d", len(vectors), len(all_texts))
            return
        for task, start, end in spans:
            _prototypes[task] = _mean(vectors[start:end])
        logger.info("classifier.prototypes_ready tasks=%d dims=%d", len(_prototypes), dims)


def _mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    n = len(vectors)
    d = len(vectors[0])
    out = [0.0] * d
    for v in vectors:
        for i in range(d):
            out[i] += v[i]
    return [x / n for x in out]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


async def classify(prompt: str, model: str, dims: int) -> Optional[tuple[str, float]]:
    """Return (task, confidence) or None if confidence < threshold.

    Never raises — any provider failure just returns None so the caller
    proceeds without a hint (business-as-usual path).
    """
    if not prompt or len(prompt.strip()) < 3:
        return None
    await _build_prototypes(model, dims)
    if not _prototypes:
        return None
    vecs = await _embed_many([prompt], model, dims)
    if not vecs:
        return None
    query = vecs[0]
    best_task: Optional[str] = None
    best_sim = -1.0
    for task, proto in _prototypes.items():
        sim = _cosine(query, proto)
        if sim > best_sim:
            best_sim = sim
            best_task = task
    if best_task is None or best_sim < _MIN_CONFIDENCE:
        return None
    return best_task, best_sim
