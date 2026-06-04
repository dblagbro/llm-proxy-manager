// v5.0.0 — MyCompliancePage. Compliance transparency view.
// v5.0.8 — backend /api/me/compliance returns one of two shapes:
//   view='per_key'  when called with an api_key — original behavior
//                   (effective blocklist for THAT key + its 24h stats)
//   view='system'   when called with a session cookie (admin UI nav)
//                   — system-wide blocklist + fleet-wide 24h stats
// This page renders both shapes. Pre-v5.0.8 it 401-looped on the admin
// UI nav because the endpoint only accepted api_key auth.
import { useQuery } from '@tanstack/react-query'
import { ShieldCheck, AlertCircle, FileText, ExternalLink } from 'lucide-react'
import { complianceApi } from '@/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { useAuth } from '@/context/AuthContext'
import { formatTimeForUser } from '@/utils/time'
import { KNOWN_COMPANIES } from '@/types'

function companyLabel(id: string): string {
  return KNOWN_COMPANIES.find(c => c.id === id)?.label ?? id
}

const COMPLIANCE_HEADERS: Array<{ name: string; desc: string }> = [
  {
    name: 'X-Compliance-Substitution',
    desc: 'Set to "true" when the proxy routed your request to a different model than the one you asked for because of a compliance policy.',
  },
  {
    name: 'X-Compliance-Requested-Model',
    desc: 'The model your client requested (the one that was substituted away from).',
  },
  {
    name: 'X-Compliance-Served-Model',
    desc: 'The model that actually served the response.',
  },
  {
    name: 'X-Compliance-Substitution-Code',
    desc: 'Machine-readable reason — api-key-policy:blocked-company:<name>.',
  },
  {
    name: 'X-Compliance-Refusal-Reason',
    desc: 'Set on 451/503/403 refusals — client-product-banned, no-compliant-provider-available, no-compliant-local-provider, path-not-in-allowed_paths.',
  },
  {
    name: 'X-Compliance-Audit-Id',
    desc: 'ULID for the row in compliance_events that records this decision; quote this when filing a compliance question.',
  },
]

export function MyCompliancePage() {
  const { user } = useAuth()
  const { data, isLoading, error } = useQuery({
    queryKey: ['my-compliance'],
    queryFn: complianceApi.me,
  })

  if (isLoading) {
    return <div className="p-6 flex justify-center"><Spinner /></div>
  }

  if (error) {
    return (
      <div className="p-6 max-w-3xl">
        <Card>
          <CardContent>
            <div className="flex items-center gap-2 text-amber-600 dark:text-amber-400">
              <AlertCircle className="h-5 w-5" />
              <p className="text-sm">Could not load compliance state: {(error as Error).message}</p>
            </div>
          </CardContent>
        </Card>
      </div>
    )
  }

  if (!data) return null

  // Branch by view shape.
  const isSystemView = data.view === 'system'
  const effective = data.effective_blocked_companies
  const systemBlocked = data.system_blocked_companies
  const perKeyBlocked = isSystemView ? [] : data.per_key_blocked_companies

  // Counters differ between views — normalize.
  const substitutions24h = isSystemView
    ? data.fleet_recent_substitutions_24h
    : data.recent_substitutions_24h
  const refusals24h = isSystemView
    ? data.fleet_recent_451_count_24h
    : data.recent_451_count_24h

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
          <ShieldCheck className="h-5 w-5 text-indigo-500" />
          {isSystemView ? 'Compliance (system-wide view)' : 'My Compliance'}
        </h1>
        <p className="text-sm text-gray-400 mt-0.5">
          {isSystemView
            ? `Operator view (signed in as ${data.viewer_username}). Showing the proxy's system-wide policy and fleet-wide 24-hour activity. To see a specific key's posture, call this endpoint with the key's bearer token.`
            : `Showing the policy that applies to the API key you called with (${data.api_key_name}) and what happened in the last 24 hours.`}
        </p>
      </div>

      {/* Effective blocklist */}
      <Card>
        <CardHeader>
          <CardTitle>
            {isSystemView ? 'System-wide blocked companies' : 'Effective blocked companies'}
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          {effective.length === 0 ? (
            <p className="text-sm text-gray-400">
              {isSystemView
                ? 'No companies are blocked system-wide. Per-key blocks may still apply individually.'
                : 'No companies are blocked for this key.'}
            </p>
          ) : (
            <div className="flex flex-wrap gap-1.5">
              {effective.map(id => (
                <Badge key={id} variant="warning" className="font-mono">
                  {companyLabel(id)}
                </Badge>
              ))}
            </div>
          )}

          {!isSystemView && (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 pt-2 border-t border-gray-200 dark:border-gray-700">
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1">From this API key</p>
                {perKeyBlocked.length > 0 ? (
                  <div className="flex flex-wrap gap-1">
                    {perKeyBlocked.map(id => (
                      <span key={id} className="text-xs font-mono text-gray-700 dark:text-gray-300">{id}</span>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-gray-400">none</p>
                )}
              </div>
              <div>
                <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-1">From system policy</p>
                {systemBlocked.length > 0 ? (
                  <div className="flex flex-wrap gap-1">
                    {systemBlocked.map(id => (
                      <span key={id} className="text-xs font-mono text-gray-700 dark:text-gray-300">{id}</span>
                    ))}
                  </div>
                ) : (
                  <p className="text-xs text-gray-400">none</p>
                )}
              </div>
            </div>
          )}

          {isSystemView && (
            <p className="text-xs text-gray-400 pt-2 border-t border-gray-200 dark:border-gray-700">
              {data.keys_with_per_key_policy} API key{data.keys_with_per_key_policy === 1 ? '' : 's'} carry an additional per-key blocklist on top of the system policy.
            </p>
          )}
        </CardContent>
      </Card>

      {/* Allowed paths + debug echo — per-key view only */}
      {!isSystemView && (
        <Card>
          <CardHeader><CardTitle>Other policy</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            <div>
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Allowed paths</p>
              {data.allowed_paths == null ? (
                <p className="text-xs text-gray-400">Unrestricted — this key can hit any endpoint.</p>
              ) : data.allowed_paths.length === 0 ? (
                <p className="text-xs text-amber-500">
                  Restricted with an empty whitelist — every endpoint is denied. Contact your admin.
                </p>
              ) : (
                <ul className="text-xs font-mono text-gray-700 dark:text-gray-300 space-y-0.5">
                  {data.allowed_paths.map(p => <li key={p}>· {p}</li>)}
                </ul>
              )}
            </div>
            <div>
              <p className="text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Debug echo endpoint</p>
              <Badge variant={data.debug_echo_enabled ? 'info' : 'muted'}>
                {data.debug_echo_enabled ? 'enabled' : 'disabled'}
              </Badge>
              <p className="text-xs text-gray-400 mt-1">
                When enabled, /api/debug/echo-client returns the same compliance decision your real call would receive, without making an inference.
              </p>
            </div>
          </CardContent>
        </Card>
      )}

      {/* 24h activity */}
      <Card>
        <CardHeader>
          <CardTitle>{isSystemView ? 'Fleet-wide activity (last 24h)' : 'Last 24 hours'}</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <p className="text-xs uppercase tracking-wide text-gray-400">Substitutions</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                {substitutions24h.toLocaleString()}
              </p>
              <p className="text-xs text-gray-400">
                Times a request was served by a different model than asked for, due to compliance.
              </p>
            </div>
            <div>
              <p className="text-xs uppercase tracking-wide text-gray-400">451 Unavailable For Legal Reasons</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">
                {refusals24h.toLocaleString()}
              </p>
              <p className="text-xs text-gray-400">
                Requests refused because the caller's User-Agent identified a banned client product.
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Last policy change */}
      <Card>
        <CardHeader><CardTitle>Last policy change</CardTitle></CardHeader>
        <CardContent>
          {data.last_policy_change == null ? (
            <p className="text-sm text-gray-400">
              {isSystemView
                ? 'No policy change has been recorded on this proxy yet.'
                : 'No policy change recorded for this key.'}
            </p>
          ) : (
            <dl className="grid grid-cols-3 gap-x-4 gap-y-2 text-sm">
              <dt className="text-gray-400">When</dt>
              <dd className="col-span-2 text-gray-900 dark:text-gray-100">
                {formatTimeForUser(data.last_policy_change.changed_at, user)}
              </dd>
              {isSystemView && 'scope' in data.last_policy_change && (
                <>
                  <dt className="text-gray-400">Scope</dt>
                  <dd className="col-span-2 text-gray-900 dark:text-gray-100 font-mono">
                    {data.last_policy_change.scope}
                  </dd>
                </>
              )}
              <dt className="text-gray-400">By</dt>
              <dd className="col-span-2 text-gray-900 dark:text-gray-100">
                {data.last_policy_change.changed_by_user_id ?? 'unknown'}
              </dd>
              <dt className="text-gray-400">Reason</dt>
              <dd className="col-span-2 text-gray-900 dark:text-gray-100 whitespace-pre-wrap">
                {data.last_policy_change.reason ?? '(no reason recorded)'}
              </dd>
            </dl>
          )}
          {data.compliance_disclaimer_url && (
            <p className="text-xs mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
              <a
                href={data.compliance_disclaimer_url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-indigo-600 dark:text-indigo-400 hover:underline inline-flex items-center gap-1"
              >
                <FileText className="h-3.5 w-3.5" />
                Read the compliance disclaimer
                <ExternalLink className="h-3 w-3" />
              </a>
            </p>
          )}
        </CardContent>
      </Card>

      {/* Header reference */}
      <Card>
        <CardHeader>
          <CardTitle>X-Compliance-* response headers</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-gray-400 mb-3">
            Every response your client receives includes these headers when a compliance decision was made. Use them to detect substitution in your own code without parsing the body.
          </p>
          <dl className="space-y-3">
            {COMPLIANCE_HEADERS.map(h => (
              <div key={h.name}>
                <dt className="text-xs font-mono text-indigo-600 dark:text-indigo-400">{h.name}</dt>
                <dd className="text-xs text-gray-400 mt-0.5">{h.desc}</dd>
              </div>
            ))}
          </dl>
        </CardContent>
      </Card>
    </div>
  )
}
