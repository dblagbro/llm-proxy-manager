"""v3.7.12 — AI rate limiter recommends specific IP blocks."""
from __future__ import annotations

import pytest

from app.monitoring.ai_rate_limiter import (
    compute_stats,
    build_prompt,
    _verdict_to_action,
)


def _event(severity="info", ip="1.1.1.1"):
    return {
        "timestamp": "2026-05-10T12:00:00Z",
        "event_type": "llm_request",
        "severity": severity,
        "message": "",
        "metadata": {
            "in_tok": 100, "out_tok": 50, "latency_ms": 500,
            "client_ip": ip, "model": "m1", "cost_class": "subscription",
            "request_preview": "hi",
        },
    }


# ── top_source_ips computation ────────────────────────────────────


def test_top_source_ips_counts_correctly():
    """Top 5 IPs by request count, sorted desc."""
    events = (
        [_event(ip="1.1.1.1")] * 10 +
        [_event(ip="2.2.2.2")] * 5 +
        [_event(ip="3.3.3.3")] * 3 +
        [_event(ip="4.4.4.4")] * 1
    )
    s = compute_stats(events)
    top = s["top_source_ips"]
    assert top["1.1.1.1"] == 10
    assert top["2.2.2.2"] == 5
    assert list(top.keys())[0] == "1.1.1.1"  # highest first


def test_top_source_ips_capped_at_five():
    """6 distinct IPs → top_source_ips has only 5 entries."""
    events = []
    for i in range(6):
        events.extend([_event(ip=f"10.0.0.{i}")] * (10 - i))
    s = compute_stats(events)
    assert len(s["top_source_ips"]) == 5


def test_top_source_ips_empty_for_no_ips():
    events = []
    s = compute_stats(events)
    assert s["top_source_ips"] == {}


# ── prompt includes top IPs + block_ip option ─────────────────────


def test_prompt_includes_top_ips_block():
    stats = compute_stats([_event(ip="1.1.1.1")] * 5)
    prompt = build_prompt(stats, [], "k1")
    assert "Top source IPs" in prompt
    assert "1.1.1.1: 5 requests" in prompt


def test_prompt_mentions_block_ip_verdict():
    """The block_ip option must appear in the verdict list + the JSON
    response shape spec."""
    prompt = build_prompt(compute_stats([_event()]), [], "k")
    assert "block_ip" in prompt
    assert '"ip"' in prompt


def test_prompt_handles_empty_top_ips():
    prompt = build_prompt(compute_stats([]), [], "k")
    assert "no source IPs captured" in prompt


# ── verdict→action mapping ────────────────────────────────────────


def test_block_ip_maps_to_block_ip_action():
    assert _verdict_to_action("block_ip") == "block_ip"


def test_other_verdicts_unchanged():
    assert _verdict_to_action("normal") == "none"
    assert _verdict_to_action("watch") == "none"
    assert _verdict_to_action("throttle") == "throttle_rpm"
    assert _verdict_to_action("block") == "disable"


# ── source-level wiring regression ────────────────────────────────


def test_review_row_has_suggested_block_ip():
    from app.models.db import ApiKeyAiReview
    cols = {c.name for c in ApiKeyAiReview.__table__.columns}
    assert "suggested_block_ip" in cols


def test_review_one_key_validates_ip_against_top_list():
    """block_ip verdict must demote to "watch" if the LLM names an IP
    that isn't in the top_source_ips list (hallucination guard)."""
    from pathlib import Path
    src = Path("app/monitoring/ai_rate_limiter.py").read_text()
    assert 'candidate in top_ips' in src
    assert 'demote' in src.lower() or 'watch' in src
    # The demote-to-watch is the fallback when ip is invalid
    assert 'verdict = "watch"' in src


def test_apply_suggestion_inserts_to_blocked_ips():
    """When applied_action == 'block_ip', we must insert into
    blocked_ips table (idempotent — skip if already there)."""
    from pathlib import Path
    src = Path("app/monitoring/ai_rate_limiter.py").read_text()
    assert 'review.suggested_action == "block_ip"' in src
    assert "BlockedIp(" in src
    assert "ai-rate-limiter" in src  # added_by attribution
    # Must invalidate the middleware cache after insertion
    assert "_clear_cache_for_tests" in src


def test_revert_removes_from_blocked_ips():
    """Revert path for block_ip must DELETE from blocked_ips."""
    from pathlib import Path
    src = Path("app/api/ai_rate_limiter.py").read_text()
    assert 'review.applied_action == "block_ip"' in src
    assert "_delete(BlockedIp)" in src


def test_serialize_includes_suggested_block_ip():
    from pathlib import Path
    src = Path("app/api/ai_rate_limiter.py").read_text()
    assert '"suggested_block_ip": r.suggested_block_ip' in src


def test_db_migration_adds_suggested_block_ip_column():
    from pathlib import Path
    src = Path("app/models/database.py").read_text()
    assert "ALTER TABLE api_key_ai_review ADD COLUMN suggested_block_ip TEXT" in src
