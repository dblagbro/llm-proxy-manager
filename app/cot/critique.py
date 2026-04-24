"""
Critique parsing & verification heuristics for the CoT-E pipeline.

Extracted from ``app/cot/pipeline.py`` in the 2026-04-23 refactor so the
orchestrator stays focused on flow control. All functions here are pure
(no I/O, no async) and are imported by ``pipeline.py`` at the top-level
module scope.
"""
from __future__ import annotations

import json as _json
import re

# Infrastructure CLI tools whose presence signals a verifiable answer
INFRA_TOOLS: frozenset[str] = frozenset({
    "docker", "kubectl", "systemctl", "journalctl", "service ",
    "rabbitmqctl", "rabbitmq-plugins", "rabbitmq-diagnostics",
    "asterisk", "fs_cli", "opensips", "osipsctl",
    "nginx", "apache2ctl", "httpd",
    "certbot", "acme.sh",
    "iptables", "ufw", "firewall-cmd",
    "ip route", "ip addr", "ip link", "nmcli", "netstat", "ss -",
    "mysql", "mysqladmin", "psql", "redis-cli", "mongosh",
    "curl -", "wget ", "ssh ", "scp ", "rsync ",
    "supervisorctl", "pm2 ", "gunicorn", "uwsgi",
})

SHELL_CODE_BLOCK = re.compile(r"```(?:bash|sh|shell|zsh|fish|powershell)", re.IGNORECASE)


def parse_score(critique: str) -> int:
    m = re.search(r"SCORE:\s*(\d+)", critique, re.IGNORECASE)
    return int(m.group(1)) if m else 5


def parse_gaps(critique: str) -> str:
    m = re.search(r"GAPS:\s*(.+)", critique, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def parse_critique(critique: str) -> dict:
    """Parse the JSON rubric from a critique response.

    Returns a dict with keys: factual_issues (list), missing_coverage (list),
    sufficient_for_user (bool). Falls back to the legacy SCORE:/GAPS: format
    if JSON parsing fails.
    """
    cleaned = critique.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = _json.loads(cleaned[start:end + 1])
            return {
                "factual_issues": list(obj.get("factual_issues") or []),
                "missing_coverage": list(obj.get("missing_coverage") or []),
                "sufficient_for_user": bool(obj.get("sufficient_for_user", False)),
            }
        except (ValueError, TypeError):
            pass
    score = parse_score(critique)
    gaps_line = parse_gaps(critique)
    gaps_empty = gaps_line.lower() in ("", "none", "n/a")
    return {
        "factual_issues": [],
        "missing_coverage": [] if gaps_empty else [gaps_line],
        "sufficient_for_user": score >= 8 or gaps_empty,
    }


def should_verify(answer: str) -> bool:
    """Heuristic: True if the answer likely contains infrastructure
    commands worth verifying.

    Two independent signals — either is sufficient:
      1. A fenced code block with a shell language marker (bash/sh/zsh/…)
      2. Two or more distinct infra CLI tool names present in the answer
    """
    if SHELL_CODE_BLOCK.search(answer):
        return True
    text_lower = answer.lower()
    hits = sum(1 for tool in INFRA_TOOLS if tool in text_lower)
    return hits >= 2
