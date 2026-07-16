"""v5.21.4 — AIRI prompt-cue classifier for the LMRH ``refuse-tolerance`` dim.

Detects two families of cues in the caller's last user message and
maps them to a ``refuse-tolerance=`` LMRH hint:

  - ``strict`` — creative-writing / policy-sensitive contexts (fiction,
    story, character, roleplay, poem, screenplay, ...). Prefers a
    model that WILL refuse edgy content.
  - ``lenient`` — automation / tool-firing contexts (deploy, ssh, run,
    execute, docker, kubectl, systemd, ...). Prefers a model LESS
    likely to refuse legitimate operational calls.
  - ``None`` — no cue detected; the router runs without a
    ``refuse-tolerance`` bias.

Uses regex word-boundary matching, case-insensitive. Deliberately
conservative — false positives here mean the wrong model gets
biased, which is worse than no bias. If cues from BOTH families
are present, returns ``None`` (ambiguous → skip).

Kept intentionally small + regex-based (not an LLM classifier) so it:
  1. Adds zero latency to the AIRI turn
  2. Has no dependencies
  3. Is testable + auditable in a single grep
"""
from __future__ import annotations

import re
from typing import Optional

# ── Cue vocabularies ─────────────────────────────────────────────────
#
# Word-boundary regex patterns. Matching is case-insensitive at
# compile time (``re.IGNORECASE``). Additions welcome as we see real
# operator prompts drift out of these vocabularies; ordering doesn't
# matter (all patterns OR'd).

_STRICT_CUES: tuple[str, ...] = (
    r"\bfiction\b",
    r"\bstor(y|ies)\b",
    r"\bnovel(s)?\b",
    r"\bpoems?\b",
    r"\bpoetry\b",
    r"\blyrics?\b",
    r"\bscreenplay(s)?\b",
    r"\bcharacter(s)?\b",
    r"\brolepla(y|ying|yed)\b",
    r"\bnarrat(e|es|ing|ive|ives)\b",
    r"\bdialog(ue)?s?\b",
    r"\bfan[- ]?fic(tion)?\b",
    r"\bplot\b",
    r"\bcreative writing\b",
    r"\bwrite (a|the|me a) (story|poem|scene|character|dialogue|screenplay)\b",
    r"\bcompose (a|the) (poem|story|scene)\b",
)

_LENIENT_CUES: tuple[str, ...] = (
    r"\bdeploy(ed|ing|ment)?\b",
    r"\bexecute[ds]?\b",
    r"\brun (the |this |a )?(script|command|job|task|pipeline|deploy)\b",
    r"\bssh\b",
    r"\bcurl\b",
    r"\bapi (call|request|endpoint|key)\b",
    r"\bhttp (request|call)\b",
    r"\bgit (commit|push|pull|rebase|merge|checkout)\b",
    r"\bdocker\b",
    r"\bdocker[- ]compose\b",
    r"\bkubectl\b",
    r"\bkubernetes\b",
    r"\bautomat(e|es|ed|ion|ing)\b",
    r"\bsystemd\b",
    r"\bcron(tab)?\b",
    r"\bbackground (job|task|process)\b",
    r"\brestart (the|a) (service|container|pod|daemon|worker)\b",
    r"\bkill (the|a) (process|container|pid)\b",
    r"\bnginx\b",
    r"\bsudo\b",
    r"\bbash\b",
    r"\brm -rf\b",
    r"\bfix (the|a|this) (bug|error|issue|deploy|build|test)\b",
    r"\bdebug(ging)?\b",
    r"\bpatch (the|a) (file|config|code)\b",
    r"\bapply (the|a) (patch|fix|config|migration)\b",
    r"\brefactor(ing|ed)?\b",
    r"\bmigrat(e|ion|ions|ing)\b",
    r"\btroubleshoot(ing|s)?\b",
)


def _compile_alternation(patterns: tuple[str, ...]) -> re.Pattern:
    """Compile a single OR'd regex from the pattern list. One compile
    at module load; every classification is a single ``.search``."""
    return re.compile("|".join(patterns), re.IGNORECASE)


_STRICT_RE = _compile_alternation(_STRICT_CUES)
_LENIENT_RE = _compile_alternation(_LENIENT_CUES)


def classify_refuse_tolerance(user_text: Optional[str]) -> Optional[str]:
    """Map a user message to a ``refuse-tolerance`` LMRH dim value.

    Returns:
      - ``"strict"`` — creative-writing / policy-sensitive cues detected
      - ``"lenient"`` — automation / tool-firing cues detected
      - ``None`` — no cue detected, OR both families present (ambiguous)

    Empty / whitespace-only input returns ``None``.
    """
    if not user_text or not user_text.strip():
        return None

    has_strict = bool(_STRICT_RE.search(user_text))
    has_lenient = bool(_LENIENT_RE.search(user_text))

    if has_strict and has_lenient:
        # Ambiguous — don't inject a hint. Better neutral than wrong.
        return None
    if has_strict:
        return "strict"
    if has_lenient:
        return "lenient"
    return None


def build_lmrh_hint(refuse_tolerance: Optional[str]) -> Optional[str]:
    """Build an ``LMRH-Hint``-header-shaped string from a classified
    ``refuse-tolerance`` value. Returns ``None`` when nothing to inject.

    Kept trivial so the caller can concatenate multiple dims trivially.
    """
    if not refuse_tolerance:
        return None
    return f"refuse-tolerance={refuse_tolerance}"
