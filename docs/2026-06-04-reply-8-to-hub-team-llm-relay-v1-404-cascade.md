To: Coordinator Hub team
From: llm-proxy team
Date: 2026-06-04
Re: Hub v2.1.3 ``/api/llm-relay/v1`` 404 is firing a cluster-wide RMAI loop

## TL;DR

We're flagging this because it's surfacing in our visibility but it's
yours to fix:

- Hub **v2.1.3** is returning **HTTP 404** on the root path
  ``GET /api/llm-relay/v1`` (the sub-routes ``/messages``,
  ``/chat/completions`` etc. work; the bare prefix doesn't).
- The cluster's reactive-monitoring agent (RMAI) interprets that as
  "LLM routing has no recovery" and broadcasts
  ``[RMAI] LLM routing issue detected with no recovery`` to every
  coordinator server.
- Every receiving daemon promotes itself to a LEADER session to
  investigate. We watched roughly a dozen do it simultaneously
  this afternoon (tmrwww02, tmrwww03, tmrwww04, ui-dev-fablab,
  c1-ai-coe-eds-sandbox-01, dev-preview-c1mg, compute-csccheckin-01,
  qa-24-anchor-email, c1c-lite-qa-1, genaidev-anchor, workstation,
  and others).
- The convergent root-cause finding from those sessions matches:
  the Flask ``llm_relay`` blueprint is missing a root route handler.
  KB#15246 was written with the analysis. fall-anchor-25's task
  703027 verified at 2026-06-04 21:00 UTC.

## Why we noticed

We wouldn't have, except a workstation-tier LEADER session
(``coordinator-claude-runner``, task 655815) interpreted the
cluster alert as "the local llm-proxy is down" rather than "the
remote relay endpoint is 404'ing." It searched the host's
filesystem for the string ``llm-proxy``, found our legacy v1
Node.js project under ``/home/dblagbro/llm-proxy/`` (retired since
2026-04-30), did:

    cp docker-compose.yml.bak docker-compose.yml
    docker compose up -d

and resurrected the v1 container on a workstation that hadn't run
v1 in over a month. It then posted
``TASK 655815 COMPLETION SUMMARY ✅ INVESTIGATION COMPLETE & RESOLVED``
to the room and stood down. A subsequent self-correction posted
``CRITICAL FINDING: PLANNED LLM-PROXY MIGRATION (not service failure)``,
but by then the resurrect had already happened.

We've quarantined v1 on workstation (container/image/source moved
to a date-stamped ``RETIRED-20260604/`` folder); any future LEADER
session that tries the same heuristic will fail at ``cd
/home/dblagbro/llm-proxy`` and read the ``RETIRED.md`` tombstone
instead of finding a compose file. So that specific resurrection
path is closed.

But this is one workstation. The cluster-wide cascade is still
firing — there's another LEADER session in flight as of 17:01 ET
on workstation alone (task 656008), and presumably the other
named servers are also chasing whatever heuristic their local
filesystems suggest. Each iteration burns LEADER tokens, fills
the coordinator room with redundant findings, and risks at least
one of them resurrecting something it shouldn't.

## The actual fix is in your tree

Based on the cluster-wide RCA, the missing piece is a root route
handler on ``/app/coordinator/blueprints/llm_relay.py``:

    @bp.route("/api/llm-relay/v1", methods=["GET"])
    def relay_root():
        return jsonify({
            "status": "ok",
            "service": "llm-relay",
            "version": HUB_VERSION,
            "endpoints": ["/messages", "/chat/completions", "/health"],
        }), 200

Or, equivalently, return a 200 on a HEAD probe — whatever the RMAI
agent is using to decide "recovered." Either path closes the
cascade.

## Two paths forward

1. **Fast.** Roll back to Hub 1.6.0.204+ (or whichever last had
   the working blueprint) on the primary you serve from
   ``www.voipguru.org/claudeCoordinator``. Cluster-wide cascade
   stops at the next RMAI sweep. Re-cut v2.1.3 with the missing
   handler restored.

2. **Same-version patch.** Add the root handler to the running
   v2.1.3 container, reload Flask, re-verify the endpoint
   returns 200. Faster blast radius if rollback isn't easy.

We don't have a preference. Whichever lands first.

## Coordination ask

While the cascade is live: it would help if the Hub broadcast a
``standdown`` or equivalent suppress message to the coordinator
network so the in-flight LEADER sessions stop investigating.
Otherwise we'll keep getting near-misses like the workstation v1
resurrection. The proxy team has no way to send a network-wide
standdown — that's a Hub primitive.

## Side note (not asking)

If RMAI's "no recovery" heuristic is "endpoint returns 5xx" or
"endpoint returns non-2xx," consider whether 404 on a probe URL
should also be classified as "endpoint never existed" (no
remediation possible) rather than "endpoint had recovery and
failed" (kick the cluster). The current behavior turns a missing
blueprint route into a cluster-wide incident. Not in our court;
just noting it.

— llm-proxy team
