# Emergency-stop runbook (v5.2.0+)

## What this is

A fleet-wide kill switch that **halts ALL LLM dispatch** while still letting the gateway answer health checks, admin endpoints, and produce its audit trail. Distinct from the v5.1.0 activity-log toggle: that one suppresses log writes, this one refuses LLM calls.

When engaged:
- Every `POST /v1/messages` returns HTTP 503 + `X-Compliance-Refused: llm-emergency-stop`.
- Every `POST /v1/chat/completions` returns the same.
- Every background `acompletion_with_retry` call (run worker, run compaction, COT branches, structured output, cascade retry) raises `LLMEmergencyStopError` before any upstream call.
- Every blocked request writes a `compliance_events` row; the toggle itself writes a `compliance_policy_changes` row.
- Per-key blocklists are bypassed — this is the master switch, deny is unconditional.
- Cluster sync replicates the flag to peers in ~60s.
- Local node honors immediately (cache invalidates on toggle); peers converge via 30s TTL after sync.

When disengaged:
- Routing returns to normal. Per-key + system policy applies as configured.
- Existing audit rows are unchanged (audit data is never purged by this toggle).

## When to engage

- **Compliance incident**: a tenant key is exfiltrating data and you need to halt fleet-wide while investigating, instead of rolling per-key blocklist edits to every node.
- **Vendor outage cascade**: every fallback in a region is failing and the retry storm is amplifying load.
- **Migration cutover**: pointing all consumers at a new endpoint and want to be sure none are still hitting this proxy mid-cutover.
- **Defense in depth**: drilled into the operator's reflex for compliance-sensitive deployments.

## When to NOT use it

- Routine "we want to stop using Anthropic" — set `blocked_companies` on the affected keys or system-wide instead.
- Routine "we want to ban one specific model" — use `blocked_models` instead.
- Stopping a single misbehaving caller — disable the offending API key.
- Pausing log capture — use the v5.1.0 `compliance.activity_logging_enabled` toggle.

## How to engage / disengage

### Web UI (preferred)

1. Navigate to `/llm-proxy2/compliance` (or your deployment's equivalent path).
2. The first panel is **LLM Emergency Stop**. It shows ROUTING (normal) or ENGAGED (red bordered card).
3. Click **Engage Emergency Stop**, fill in the audit reason ("incident-2026-06-06 — exfil suspected on key X"), confirm.
4. The change is immediate on this node and replicates to peers within ~60s.
5. Verify by hitting `/v1/messages` from a test client — expect 503 with `code: llm-emergency-stop`.

### API (for scripted incident response)

```sh
# Engage
curl -X POST https://YOUR-DEPLOYMENT/api/admin/llm-emergency-stop/toggle \
  -H "Cookie: session=$ADMIN_SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -d '{"enabled": true, "reason": "incident-2026-06-06 — exfil suspected on key X"}'

# Status check (replicates the WebUI's view)
curl https://YOUR-DEPLOYMENT/api/admin/llm-emergency-stop/status \
  -H "Cookie: session=$ADMIN_SESSION_COOKIE"

# Disengage
curl -X POST https://YOUR-DEPLOYMENT/api/admin/llm-emergency-stop/toggle \
  -H "Cookie: session=$ADMIN_SESSION_COOKIE" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false, "reason": "all-clear, monitoring resumed"}'
```

Admin endpoints require an admin user session. Service-account access can be wired via the existing `key_type=admin` flow.

### Direct DB (emergency fallback if API is down)

```sql
INSERT OR REPLACE INTO system_settings (key, value, value_type, updated_at)
VALUES ('compliance.llm_emergency_stop', 'true', 'bool', strftime('%s','now'));
```

You will need to restart the container OR call `app.monitoring.llm_emergency_stop.invalidate_cache()` for the local node to pick it up before the 30s TTL expires. **Prefer the API path** — it writes the audit row automatically and replicates correctly. Direct DB writes skip the audit and require manual cluster propagation.

## How to verify it engaged

```sh
# Should return 503 with llm-emergency-stop reason
curl -s -o /dev/null -w "%{http_code} %{header_x_compliance_refused}\n" \
  -X POST https://YOUR-DEPLOYMENT/v1/messages \
  -H "x-api-key: $YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}]}'
# Expected: 503 llm-emergency-stop
```

Background job verification: tail the proxy logs and look for `LLMEmergencyStopError` raised from `app.routing.retry`. Active runs will surface a controlled error (`error_kind=error_emergency_stop` if your run handler maps it that way).

## Audit trail

Each flip writes a `compliance_policy_changes` row (system-scope, never purgeable). Each blocked request writes a `compliance_events` row with:

| Column | Value |
|---|---|
| `event_type` | `llm_emergency_stop` |
| `reason_code` | `llm-emergency-stop` |
| `http_status` | `503` |
| `requested_model` | from caller body (if available before stop) |
| `api_key_id` | the calling key |

Query:

```sql
-- Toggle history
SELECT changed_at, changed_by_user_id, reason
FROM compliance_policy_changes
WHERE scope='system' AND reason LIKE 'llm_emergency_stop%'
ORDER BY changed_at DESC LIMIT 20;

-- Requests refused during the most recent engagement
SELECT created_at, api_key_id, requested_model, COUNT(*) AS n
FROM compliance_events
WHERE event_type='llm_emergency_stop'
GROUP BY DATE(created_at), api_key_id
ORDER BY created_at DESC;
```

The daily `ComplianceAuditChain` hash sweeper covers both tables; tampering with a closed-day's flip row breaks the chain on every subsequent day.

## Tests

`tests/unit/test_v520_llm_emergency_stop.py` (15 tests):

- Default OFF preserves pre-v5.2 behavior.
- Engage / disengage / no-op flip flow + audit row written each time.
- Cache invalidates eagerly on toggle (no 30s wait for local node).
- Direct DB write propagates after TTL refresh (simulates cluster sync apply).
- `acompletion_with_retry` raises `LLMEmergencyStopError` BEFORE the litellm call (covers background callers).
- Orthogonal to v5.1.0 logging stop (different `SETTING_KEY`).
- Handler helper is no-op when disengaged.
- Handler helper raises HTTPException(503) with `code: llm-emergency-stop` + writes the per-request audit row.

Run: `python3 -m pytest tests/unit/test_v520_llm_emergency_stop.py -v`.

## What this does NOT cover

- It does not stop **inbound** traffic at the load balancer. Clients still reach the proxy and get 503. If you need to stop the inbound flow entirely, nginx / your LB takes over.
- It does not stop the local cache from serving cached responses. The semantic cache (when enabled) returns cached completions without touching upstream. If that's not the behavior you want during an incident, drain the cache via `/api/admin/cache/flush` separately.
- It does not pause cluster sync, key rotation, or other internal-machinery. Only LLM dispatch is gated.
