"""Generic secret scanner — stops a NEW credential reaching a public repo.

Why this exists
---------------
``tests/unit/test_v52212_no_secrets_in_source.py`` pins the two literals that
actually leaked (the admin password and ``DEFAULT_BRIDGE_TOKEN``). That guard
is exact-match: it proves those two never come back, and catches nothing else.
The 2026-08-28 audit found them only because a human went looking.

This module is the general case. One implementation, three callers:

  * ``tests/unit/test_v52215_secret_scan.py`` — fails the suite on a finding.
  * ``.githooks/pre-commit``                  — blocks the commit locally.
  * ``.github/workflows/ci.yml``              — blocks the push in CI.

Design notes
------------
A guard people switch off is worth nothing, so the rules are ordered by
confidence and the noisy middle ground is deliberately left out:

  * Vendor-prefixed tokens (``AKIA…``, ``sk-ant-…``, ``ghp_…``) are
    unambiguous. Always reported.
  * Private key blocks are unambiguous. Always reported.
  * Generic ``token = "..."`` assignments are the false-positive minefield —
    this repo has dozens of legitimate fixtures like ``api_key="mock-key"``.
    They are reported only when the value is long AND high-entropy, which no
    hand-written fixture in this tree is.

Escape hatch: append ``pragma: allowlist secret`` to the line. Use it for
redaction fixtures that must contain a realistic-looking token.
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

# --- high-confidence vendor patterns -------------------------------------
# Each of these carries a vendor prefix, so a match is a real credential
# shape rather than a guess.
VENDOR_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("aws-access-key-id", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("anthropic-api-key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{24,}")),
    ("openai-api-key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9]{32,}")),
    ("llm-proxy-key", re.compile(r"llmp-[A-Za-z0-9\-_]{24,}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}")),
    ("slack-token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{12,}")),
    ("google-api-key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("stripe-key", re.compile(r"[rs]k_(?:live|test)_[A-Za-z0-9]{20,}")),
    ("private-key-block", re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_\-]{12,}\.eyJ[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{8,}")),
]

# --- generic high-entropy assignment -------------------------------------
ASSIGN_RE = re.compile(
    r"""(?ix)
    \b(password|passwd|secret|token|api[_-]?key|access[_-]?key|
       private[_-]?key|client[_-]?secret|bridge[_-]?token)\b
    \s*[:=]\s*
    ["']([^"'\s]{16,})["']
    """
)

# Values that are obviously not live credentials.
PLACEHOLDER_RE = re.compile(
    r"(?i)(example|dummy|placeholder|redacted|changeme|sample|fake|mock|"
    r"not-a-|test[-_]?key|your[-_]|xxx|\.\.\.|^\{\{|^\$\{|^<|os\.environ|getenv)"
)

ALLOWLIST_PRAGMA = "pragma: allowlist secret"

# Paths that never hold live credentials, or that exist to describe them.
SKIP_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".venv",
    "venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}
SKIP_SUFFIXES = (
    ".pyc",
    ".pyo",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".pdf",
    ".woff",
    ".woff2",
    ".ttf",
    ".eot",
    ".map",
    ".lock",
    ".svg",
)
# This module names the patterns it hunts for; scanning it finds itself.
SELF = "tools/secret_scan.py"


class Finding(NamedTuple):
    path: str
    line_no: int
    rule: str
    redacted: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.path}:{self.line_no}: [{self.rule}] {self.redacted}"


def shannon_entropy(s: str) -> float:
    """Bits of entropy per character. Random base64 lands ~4.5-6.0;
    hand-written kebab-case fixtures land under ~3.5."""
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _redact(value: str) -> str:
    if len(value) <= 12:
        return value[:3] + "…"
    return f"{value[:6]}…{value[-3:]} ({len(value)} chars)"


def scan_text(path: str, text: str) -> list[Finding]:
    """Scan one file's contents. Pure function — no I/O, easy to unit test."""
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), 1):
        # Skip pathological lines (minified bundles, base64 blobs in docs).
        if len(line) > 2000 or ALLOWLIST_PRAGMA in line:
            continue

        for rule, pattern in VENDOR_PATTERNS:
            match = pattern.search(line)
            if match:
                findings.append(Finding(path, line_no, rule, _redact(match.group(0))))

        assign = ASSIGN_RE.search(line)
        if assign:
            value = assign.group(2)
            if (
                not PLACEHOLDER_RE.search(value)
                and len(value) >= 20
                and shannon_entropy(value) >= 3.9
                and any(c.isdigit() for c in value)
                and any(c.isalpha() for c in value)
            ):
                findings.append(Finding(path, line_no, "high-entropy-assignment", _redact(value)))
    return findings


def iter_files(paths: Iterable[str]) -> Iterable[Path]:
    for raw in paths:
        p = Path(raw)
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        if p.name.endswith(SKIP_SUFFIXES):
            continue
        if p.as_posix() == SELF:
            continue
        yield p


def git_tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=False)
    return out.stdout.split()


def scan_paths(paths: Iterable[str]) -> list[Finding]:
    findings: list[Finding] = []
    for path in iter_files(paths):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        findings.extend(scan_text(path.as_posix(), text))
    return findings


def main(argv: list[str]) -> int:
    paths = argv[1:] or git_tracked_files()
    findings = scan_paths(paths)
    if not findings:
        print(f"secret-scan: clean ({len(list(iter_files(paths)))} files)")
        return 0
    print("secret-scan: POSSIBLE CREDENTIALS FOUND\n", file=sys.stderr)
    for f in findings:
        print(f"  {f}", file=sys.stderr)
    print(
        "\nIf a hit is a fixture and not a live credential, append"
        f" '{ALLOWLIST_PRAGMA}' to that line.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
