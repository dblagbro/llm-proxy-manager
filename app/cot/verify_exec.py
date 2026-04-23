"""Reflexion-style verification — parse and (optionally) execute.

The VERIFY_SYSTEM prompt asks the model to emit lines like:
    1. `docker ps | grep rabbitmq` → container "rabbitmq" in output
    2. `curl -sI http://localhost:8080/health` → HTTP/1.1 200 OK

This module:
1. Parses those lines into structured {step, command, expected} records.
2. For a SAFE subset of commands (HTTP GET, DNS resolve, TCP port open) it
   executes in-process via Python stdlib so no shell/subprocess is ever
   spawned — the proxy container stays minimal and the attack surface is
   limited to what urllib/socket already do.
3. Marks whether each step's actual output contains the expected substring.

Commands outside the safe subset are returned unexecuted with status="skipped"
so agent frameworks / clients can execute them themselves if they wish.
"""
from __future__ import annotations

import logging
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_STEP_RE = re.compile(
    r"^\s*(?P<num>\d+)\.\s*`(?P<cmd>[^`]+)`\s*(?:→|->|—|-)\s*(?P<exp>.+?)\s*$"
)
_HTTP_RE = re.compile(r"^curl\s+(?:-\S+\s+)*(?P<url>https?://\S+)\s*$")
_HOST_RE = re.compile(r"^(?:host|nslookup|dig)\s+(?P<target>\S+)(?:\s+.*)?$")
_TCP_RE = re.compile(r"^nc\s+-z\s+(?P<host>\S+)\s+(?P<port>\d+)\s*$")
_HEAD_RE = re.compile(r"^curl\s+(?:-\S+\s+)*-?I\s+(?P<url>https?://\S+)\s*$")


@dataclass
class VerifyStep:
    number: int
    command: str
    expected: str
    actual: str = ""
    status: str = "pending"  # pending | pass | fail | skipped | error
    duration_ms: float = 0.0
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "number": self.number,
            "command": self.command,
            "expected": self.expected,
            "actual": self.actual,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 1),
            "error": self.error,
        }


def parse_verify_block(text: str) -> list[VerifyStep]:
    """Extract numbered backtick-wrapped commands with their expected results."""
    steps: list[VerifyStep] = []
    for line in text.splitlines():
        m = _STEP_RE.match(line)
        if not m:
            continue
        steps.append(VerifyStep(
            number=int(m.group("num")),
            command=m.group("cmd").strip(),
            expected=m.group("exp").strip(),
        ))
    return steps


async def execute_step(step: VerifyStep, timeout_sec: float = 5.0) -> VerifyStep:
    """Execute a step via Python stdlib if it matches the safe subset.

    Returns the step mutated in place (for chainability).
    """
    import asyncio
    import time

    cmd = step.command.strip().rstrip(";").strip()
    t0 = time.monotonic()

    # HTTP HEAD (curl -sI / -I / -X HEAD)
    m = _HEAD_RE.match(cmd)
    if m:
        step.actual, step.error = await asyncio.get_event_loop().run_in_executor(
            None, _http_head, m.group("url"), timeout_sec
        )
        step.duration_ms = (time.monotonic() - t0) * 1000
        _grade(step)
        return step

    # HTTP GET
    m = _HTTP_RE.match(cmd)
    if m:
        step.actual, step.error = await asyncio.get_event_loop().run_in_executor(
            None, _http_get, m.group("url"), timeout_sec
        )
        step.duration_ms = (time.monotonic() - t0) * 1000
        _grade(step)
        return step

    # DNS resolve (host / nslookup / dig)
    m = _HOST_RE.match(cmd)
    if m:
        step.actual, step.error = _resolve(m.group("target"))
        step.duration_ms = (time.monotonic() - t0) * 1000
        _grade(step)
        return step

    # TCP port probe (nc -z)
    m = _TCP_RE.match(cmd)
    if m:
        step.actual, step.error = _tcp_probe(m.group("host"), int(m.group("port")), timeout_sec)
        step.duration_ms = (time.monotonic() - t0) * 1000
        _grade(step)
        return step

    # Not in the executable subset — mark skipped so caller can forward to
    # an agent framework that has a real shell.
    step.status = "skipped"
    step.actual = ""
    return step


def _http_get(url: str, timeout: float) -> tuple[str, str]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "llm-proxy-verify/1"})
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            body = r.read(4096).decode("utf-8", errors="replace")
            return f"HTTP {r.status} {r.reason}\n{body[:2000]}", ""
    except urllib.error.HTTPError as e:
        # HTTP errors still give us useful info
        return f"HTTP {e.code} {e.reason}", ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", str(e)


def _http_head(url: str, timeout: float) -> tuple[str, str]:
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "llm-proxy-verify/1"}, method="HEAD"
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            hdrs = "\n".join(f"{k}: {v}" for k, v in r.headers.items())
            return f"HTTP {r.status} {r.reason}\n{hdrs[:1500]}", ""
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} {e.reason}", ""
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "", str(e)


def _resolve(target: str) -> tuple[str, str]:
    try:
        infos = socket.getaddrinfo(target, None, type=socket.SOCK_STREAM)
        addrs = sorted({ai[4][0] for ai in infos})
        return "\n".join(addrs), ""
    except OSError as e:
        return "", str(e)


def _tcp_probe(host: str, port: int, timeout: float) -> tuple[str, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return f"connected to {host}:{port}", ""
    except OSError as e:
        return "", str(e)


def _grade(step: VerifyStep) -> None:
    """Decide pass/fail/error by checking expected against actual."""
    if step.error and not step.actual:
        step.status = "error"
        return
    expected_lower = step.expected.lower()
    actual_lower = step.actual.lower()
    # Pull key tokens out of the expected phrase — status codes, quoted strings,
    # or named things. Fall back to substring match.
    code_match = re.search(r"\b(1\d\d|2\d\d|3\d\d|4\d\d|5\d\d)\b", step.expected)
    if code_match and code_match.group() in step.actual:
        step.status = "pass"
        return
    quoted = re.findall(r'"([^"]+)"', step.expected)
    for q in quoted:
        if q.lower() in actual_lower:
            step.status = "pass"
            return
    # Fallback: expected phrase substring (first ~5 words)
    expected_short = " ".join(expected_lower.split()[:5])
    if expected_short and expected_short in actual_lower:
        step.status = "pass"
        return
    step.status = "fail"


def render_executed_block(steps: list[VerifyStep]) -> str:
    """Render executed steps as markdown for the thinking block."""
    if not steps:
        return "_(no verification steps)_"
    lines = ["## Verification (executed)"]
    emoji = {"pass": "✓", "fail": "✗", "error": "⚠", "skipped": "…", "pending": "·"}
    for s in steps:
        mark = emoji.get(s.status, "·")
        head = f"{mark} **{s.number}.** `{s.command}`"
        if s.status == "skipped":
            lines.append(f"{head} — _not executable in proxy sandbox (client-side)_")
            lines.append(f"   expected: {s.expected}")
            continue
        lines.append(f"{head} — {s.status} ({s.duration_ms:.0f}ms)")
        lines.append(f"   expected: {s.expected}")
        if s.actual:
            snippet = s.actual[:400].replace("\n", " ")
            lines.append(f"   actual:   {snippet}")
        if s.error:
            lines.append(f"   error:    {s.error}")
    return "\n".join(lines)


def has_failures(steps: list[VerifyStep]) -> bool:
    return any(s.status in ("fail", "error") for s in steps)
