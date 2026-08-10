# Project charter — llm-proxy-v2

## What it is
A self-hosted, single-controllable LLM routing gateway. It accepts Anthropic, OpenAI, and OpenAI
Responses request shapes, routes each to the best available upstream provider (via litellm + the
LMRH protocol, with capability/circuit-breaker/compliance gating), and returns the caller's
expected wire format. It maintains provider OAuth sessions, syncs config across a small cluster,
and enforces per-key + system-wide compliance with audit-grade logging.

## Stage
**Production** (used by internal TMR tooling) currently **in active rescue** — see
`docs/current-state.md`.

## Primary users
- Internal TMR tooling and agents (DevinGPT, coordinator hub, paperless, etc.) that route LLM
  traffic through one controllable gateway.
- Downstream / gov-compliance consumers who pull the Docker Hub image
  (`dblagbro/llm-proxy-manager`) and rely on the `app/compliance/` enforcement layer.

## Success criteria
- One place to control provider keys, routing, cost, and compliance.
- Correct wire-format translation across Anthropic/OpenAI shapes.
- Reliability: stays healthy under sustained load without manual restarts (the current gap).
- Compliance enforcement is auditable and verifiable for regulated consumers.

## Non-goals / north star
- **North star (from `design.md`):** an operator can read the system end-to-end in an afternoon —
  optimize for legibility, not cleverness or theoretical max throughput.
- Not a hosted multi-tenant SaaS; not a Kubernetes-first design (portable app behavior, but no
  premature k8s complexity).

## Key references
Architecture: `architecture.md` · Design contract: `design.md` · Onboarding: `docs/project-map.md`
· Live status: `docs/current-state.md` · Roadmap: `docs/roadmap.md` · Compliance spec:
`docs/5.0-compliance-design.md`.
