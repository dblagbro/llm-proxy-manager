---
name: project-bootstrap
description: Establishes goals, architecture, project memory, Git, testing, container, cloud, and delivery foundations before feature development begins.
---

Act as the technical product lead, principal architect, and delivery planner. Use planning mode. Read `AGENTS.md` and all existing project guidance first. If the agent system is missing or materially incomplete, run or recommend `agent-system-setup` before proceeding.

## Discover before deciding

Inspect the repository, Git history, existing tests, build and run paths, dependencies, CI, containers, infrastructure code, open issues, release history, and current documentation. Do not ask questions the repository can answer.

Classify the current stage: concept; PoC; demo; alpha/beta; production; maintenance or rescue.

Identify: intended users and problem; success criteria and non-goals; current alternatives and differentiation hypothesis; functional and non-functional requirements; data sensitivity and compliance concerns; expected scale and reliability needs; supported environments; GitHub, Docker Hub, cloud, and self-hosting targets; whether Kubernetes is justified now, later, or not at all.

Ask one grouped set of only material product or architecture questions that cannot be answered from evidence. Otherwise proceed with explicit assumptions.

## Build the project operating plan

Create or update existing equivalents rather than duplicating documents:

- project charter and current stage in `docs/project.md`
- architecture, boundaries, interfaces, data flow, and dependency direction in `docs/architecture.md`
- concise onboarding map in `docs/project-map.md`
- milestone roadmap and acceptance criteria in `docs/roadmap.md`
- current status in `docs/current-state.md`
- test strategy and quality gates in `docs/testing.md`
- operational and deployment guidance when relevant
- security assumptions and threat boundaries when relevant
- initial competitive analysis with a review date
- ADRs for consequential decisions
- README and contributor/run instructions

## Delivery foundation

Plan and, after approval, establish the smallest appropriate foundation for the current stage:

- Git initialization or cleanup, `.gitignore`, branch and commit conventions
- secret-safe configuration and example environment files
- reproducible install, build, run, test, lint, format, and migration commands
- fast CI with caching and clear artifacts
- unit, integration, API, and Playwright foundations as applicable
- logging, health checks, error handling, and basic observability
- Docker and Compose only when they improve repeatability
- Kubernetes-ready application behavior without premature Kubernetes complexity: externalized configuration; graceful startup and shutdown; health/readiness endpoints; no hidden local-state assumptions; idempotent migrations and jobs
- backup and restore requirements for persistent data
- release/versioning strategy
- explicit definition of done

Use the appropriate provider's well-architected principles when AWS, Google Cloud, Azure, or another platform is present. Keep the application core portable where it creates value, but allow provider-specific adapters rather than forcing a lowest-common-denominator design.

## Market checkpoint

Perform current research when the product direction depends on external projects or market demand. Record dated evidence, direct alternatives, open-source substitutes, licensing, maintenance health, security history, migration cost, and a recommendation to build, integrate, fork, partner, pivot, or stop. Do not make an irreversible product decision without review.

## Output and control point

Provide: (1) Current-state assessment; (2) Assumptions and unanswered decisions; (3) Proposed architecture and stage-appropriate scope; (4) Project map; (5) Test and release strategy; (6) Git/Docker/cloud/Kubernetes path; (7) Risk register; (8) First milestone with measurable acceptance criteria; (9) Files to create or update.

Pause before implementation. After approval, create only the agreed foundation and documentation, verify it from a clean environment, and do not push or deploy.
