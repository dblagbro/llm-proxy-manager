To: Coordinator Hub team
From: llm-proxy team
Date: 2026-06-04
Re: v5.0.13 landed — ``ComplianceEvent.matched_pattern`` now carries
    the rejected path for ``path_not_allowed`` rows

Closing the loop on the backlog item we promised in the v5.0.12
ship memo (and one before).

Live on tmrwww01 / tmrwww02 / c1conv / smoke as of 2026-06-04. Next
``path_not_allowed`` event in your canary will carry the normalized
rejected path in the ``matched_pattern`` column — no more grepping
the access log to recover what got blocked. Same value as the JSON
403 body's ``requested_path`` field; the audit row is now self-
contained.

If you'd like to confirm against your own observation, the v5.0.12
canary surfaced these audit IDs for ``/v1/v1/messages`` rejections
(those rows still have ``matched_pattern=NULL`` because they
predate v5.0.13 — only new rows get the field):

    comp_0019e9487097822
    comp_0019e94830209f1
    …

A representative new row from the smoke instance has the shape:

    audit_id: comp_…
    event_type: path_not_allowed
    reason_code: path-not-in-allowed_paths
    matched_pattern: /v1/v1/messages     ← new in v5.0.13
    client_user_agent: claude-cli/2.1.118 (external, sdk-cli)
    http_status: 403

Nothing to action — just a heads-up that the next time we (or
you) need to investigate a path-rejection cascade, the audit
table answers the diagnostic on its own.

— llm-proxy team
