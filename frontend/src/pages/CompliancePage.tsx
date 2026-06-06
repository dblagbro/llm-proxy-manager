// v5.0.0 — Admin CompliancePage. Surfaces compliance_events,
// compliance_policy_changes, and the cluster preflight readiness card
// (decision 32). Filterable + CSV-exportable per spec §3.2 / §9.3.
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, RefreshCw, ShieldAlert, Server } from 'lucide-react'
import { complianceApi, keysApi, loggingApi } from '@/api'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { useToast } from '@/components/ui/Toast'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { useAuth } from '@/context/AuthContext'
import { formatTimeForUser } from '@/utils/time'
import { KNOWN_COMPANIES } from '@/types'
import type { ComplianceEventType } from '@/types'

const EVENT_TYPES: { id: ComplianceEventType; label: string; variant: 'warning' | 'danger' | 'info' | 'muted' }[] = [
  { id: 'model_substitution',       label: 'model_substitution',       variant: 'warning' },
  { id: 'client_product_refusal',   label: 'client_product_refusal',   variant: 'danger'  },
  { id: 'compliance_no_substitute', label: 'compliance_no_substitute', variant: 'danger'  },
  { id: 'cache_filtered',           label: 'cache_filtered',           variant: 'info'    },
  { id: 'memory_filtered',          label: 'memory_filtered',          variant: 'info'    },
  { id: 'path_not_allowed',         label: 'path_not_allowed',         variant: 'muted'   },
]

function truncate(s: string | null | undefined, n: number): string {
  if (!s) return '—'
  return s.length > n ? s.slice(0, n) + '…' : s
}

export function CompliancePage() {
  const { user } = useAuth()
  const [apiKeyId, setApiKeyId] = useState<string>('')
  const [eventType, setEventType] = useState<string>('')
  const [start, setStart] = useState<string>('')
  const [end, setEnd] = useState<string>('')
  const [blockedCompany, setBlockedCompany] = useState<string>('')
  const [showReadiness, setShowReadiness] = useState(false)

  // Key list for the api_key dropdown (so the user sees names not opaque IDs).
  const { data: keys } = useQuery({ queryKey: ['apikeys'], queryFn: keysApi.list })

  const filters = useMemo(() => ({
    api_key_id: apiKeyId || null,
    event_type: eventType || null,
    start: start || null,
    end: end || null,
    blocked_company: blockedCompany || null,
  }), [apiKeyId, eventType, start, end, blockedCompany])

  const { data: events, isLoading: eventsLoading, refetch } = useQuery({
    queryKey: ['compliance-events', filters],
    queryFn: () => complianceApi.events(filters),
  })

  const { data: policyChanges } = useQuery({
    queryKey: ['compliance-policy-changes', apiKeyId],
    queryFn: () => complianceApi.policyChanges(apiKeyId || null, 50),
  })

  const { data: readiness, isLoading: readinessLoading, refetch: refetchReadiness } = useQuery({
    queryKey: ['compliance-readiness'],
    queryFn: complianceApi.clusterReady,
    enabled: showReadiness,
  })

  const csvHref = complianceApi.eventsCsvUrl(filters)

  function keyName(id: string | null | undefined): string {
    if (!id) return '—'
    const k = keys?.find(x => x.id === id)
    return k ? (k.name || k.key_prefix) : id.slice(0, 8) + '…'
  }

  return (
    <div className="p-6 space-y-6 max-w-7xl">
      {/* v5.1.0 / Batch C1 — activity-log on/off panic button */}
      <LoggingControlsPanel />
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100 flex items-center gap-2">
            <ShieldAlert className="h-5 w-5 text-indigo-500" />
            Compliance Events
          </h1>
          <p className="text-sm text-gray-400 mt-0.5">
            Audit log of every model substitution, 451 refusal, cache/memory filter, and path-denied event.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="outline"
            onClick={() => { setShowReadiness(true); refetchReadiness() }}
          >
            <Server className="h-4 w-4 mr-1.5" />Cluster preflight
          </Button>
          <a href={csvHref} download>
            <Button size="sm" variant="outline">
              <Download className="h-4 w-4 mr-1.5" />Export CSV
            </Button>
          </a>
          <Button size="sm" onClick={() => refetch()}>
            <RefreshCw className="h-4 w-4 mr-1.5" />Refresh
          </Button>
        </div>
      </div>

      {/* Cluster readiness card (toggleable) */}
      {showReadiness && (
        <Card>
          <CardHeader><CardTitle>Cluster preflight — ready for policy change?</CardTitle></CardHeader>
          <CardContent>
            {readinessLoading ? (
              <div className="flex justify-center py-4"><Spinner /></div>
            ) : !readiness ? (
              <p className="text-sm text-gray-400">Could not load cluster readiness.</p>
            ) : (
              <div className="space-y-4">
                <div className="flex items-center gap-2">
                  <Badge variant={readiness.ready_for_policy_change ? 'success' : 'danger'}>
                    {readiness.ready_for_policy_change ? 'READY' : 'NOT READY'}
                  </Badge>
                  <span className="text-sm text-gray-600 dark:text-gray-400">
                    cluster size {readiness.cluster_size}, quorum {readiness.quorum_size},
                    {readiness.current_compliance_state_consistent ? ' state consistent' : ' STATE DIVERGED'}
                  </span>
                </div>
                <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2 border-t border-gray-200 dark:border-gray-700">
                  <div>
                    <p className="text-xs uppercase tracking-wide text-gray-400">Active streams</p>
                    <p className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                      {readiness.active_streams_cluster_wide.toLocaleString()}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wide text-gray-400">Active requests</p>
                    <p className="text-lg font-semibold text-gray-900 dark:text-gray-100">
                      {readiness.active_requests_cluster_wide.toLocaleString()}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs uppercase tracking-wide text-gray-400">Oldest active</p>
                    <p className="text-sm text-gray-700 dark:text-gray-300">
                      {readiness.oldest_active_request_started_at
                        ? formatTimeForUser(readiness.oldest_active_request_started_at, user)
                        : 'none'}
                    </p>
                  </div>
                </div>
                <div className="border-t border-gray-200 dark:border-gray-700 pt-2">
                  <p className="text-xs font-semibold uppercase tracking-wide text-gray-400 mb-2">Peers</p>
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-left text-gray-400">
                        <th className="py-1">Name</th>
                        <th className="py-1">Status</th>
                        <th className="py-1">Last sync</th>
                        <th className="py-1">State hash</th>
                        <th className="py-1 text-right">Streams</th>
                        <th className="py-1 text-right">Requests</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                      {readiness.peers.map(p => (
                        <tr key={p.id}>
                          <td className="py-1.5 text-gray-700 dark:text-gray-300">{p.name}</td>
                          <td className="py-1.5">
                            <Badge variant={p.status === 'healthy' ? 'success' : 'warning'}>
                              {p.status}
                            </Badge>
                          </td>
                          <td className="py-1.5 text-gray-600 dark:text-gray-400">
                            {p.last_sync_at ? formatTimeForUser(p.last_sync_at, user) : '—'}
                          </td>
                          <td className="py-1.5 font-mono text-gray-600 dark:text-gray-400">
                            {truncate(p.compliance_state_hash, 12)}
                          </td>
                          <td className="py-1.5 text-right text-gray-700 dark:text-gray-300">
                            {p.active_streams ?? 0}
                          </td>
                          <td className="py-1.5 text-right text-gray-700 dark:text-gray-300">
                            {p.active_requests ?? 0}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {/* Filters */}
      <Card>
        <CardContent>
          <div className="grid grid-cols-1 md:grid-cols-5 gap-3">
            <div className="flex flex-col gap-1">
              <label htmlFor="filter-key" className="text-xs font-semibold text-gray-400 uppercase tracking-wide">API key</label>
              <select
                id="filter-key"
                value={apiKeyId}
                onChange={e => setApiKeyId(e.target.value)}
                className="px-2 py-1.5 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              >
                <option value="">All keys</option>
                {(keys ?? []).map(k => (
                  <option key={k.id} value={k.id}>{k.name || k.key_prefix}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="filter-event" className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Event type</label>
              <select
                id="filter-event"
                value={eventType}
                onChange={e => setEventType(e.target.value)}
                className="px-2 py-1.5 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              >
                <option value="">All events</option>
                {EVENT_TYPES.map(t => (
                  <option key={t.id} value={t.id}>{t.label}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="filter-company" className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Blocked company</label>
              <select
                id="filter-company"
                value={blockedCompany}
                onChange={e => setBlockedCompany(e.target.value)}
                className="px-2 py-1.5 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              >
                <option value="">All companies</option>
                {KNOWN_COMPANIES.map(c => (
                  <option key={c.id} value={c.id}>{c.label}</option>
                ))}
              </select>
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="filter-start" className="text-xs font-semibold text-gray-400 uppercase tracking-wide">Start</label>
              <input
                id="filter-start"
                type="datetime-local"
                value={start}
                onChange={e => setStart(e.target.value)}
                className="px-2 py-1.5 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="filter-end" className="text-xs font-semibold text-gray-400 uppercase tracking-wide">End</label>
              <input
                id="filter-end"
                type="datetime-local"
                value={end}
                onChange={e => setEnd(e.target.value)}
                className="px-2 py-1.5 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none"
              />
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Events table */}
      <Card>
        <CardContent className="p-0">
          {eventsLoading ? (
            <div className="flex justify-center py-10"><Spinner /></div>
          ) : !events || events.events.length === 0 ? (
            <p className="text-sm text-gray-400 text-center py-10">No compliance events match these filters.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead className="bg-gray-50 dark:bg-gray-800/50 border-b border-gray-200 dark:border-gray-700">
                  <tr className="text-left text-gray-400 uppercase tracking-wide">
                    <th className="px-3 py-2">Audit ID</th>
                    <th className="px-3 py-2">API key</th>
                    <th className="px-3 py-2">Event</th>
                    <th className="px-3 py-2">When</th>
                    <th className="px-3 py-2">Requested</th>
                    <th className="px-3 py-2">Served</th>
                    <th className="px-3 py-2">Provider</th>
                    <th className="px-3 py-2">Blocked co.</th>
                    <th className="px-3 py-2">Reason</th>
                    <th className="px-3 py-2">User-Agent</th>
                    <th className="px-3 py-2 text-right">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                  {events.events.map(ev => {
                    const variant = EVENT_TYPES.find(t => t.id === ev.event_type)?.variant ?? 'muted'
                    return (
                      <tr key={ev.audit_id}>
                        <td className="px-3 py-2 font-mono text-gray-600 dark:text-gray-400" title={ev.audit_id}>
                          {truncate(ev.audit_id, 10)}
                        </td>
                        <td className="px-3 py-2 text-gray-700 dark:text-gray-300" title={ev.api_key_id}>
                          {keyName(ev.api_key_id)}
                        </td>
                        <td className="px-3 py-2">
                          <Badge variant={variant}>{ev.event_type}</Badge>
                        </td>
                        <td className="px-3 py-2 text-gray-600 dark:text-gray-400 whitespace-nowrap">
                          {formatTimeForUser(ev.requested_at, user)}
                        </td>
                        <td className="px-3 py-2 font-mono text-gray-700 dark:text-gray-300">
                          {ev.requested_model ?? '—'}
                        </td>
                        <td className="px-3 py-2 font-mono text-gray-700 dark:text-gray-300">
                          {ev.served_model ?? '—'}
                        </td>
                        <td className="px-3 py-2 font-mono text-gray-600 dark:text-gray-400">
                          {ev.served_provider_id ? truncate(ev.served_provider_id, 12) : '—'}
                        </td>
                        <td className="px-3 py-2 font-mono text-gray-700 dark:text-gray-300">
                          {ev.blocked_company ?? '—'}
                        </td>
                        <td className="px-3 py-2 text-gray-600 dark:text-gray-400">
                          {ev.reason_code ?? '—'}
                        </td>
                        <td
                          className="px-3 py-2 text-gray-600 dark:text-gray-400 max-w-[180px] truncate"
                          title={ev.client_user_agent ?? ''}
                        >
                          {truncate(ev.client_user_agent, 28)}
                        </td>
                        <td className="px-3 py-2 text-right text-gray-700 dark:text-gray-300 font-mono">
                          {ev.http_status ?? '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* Policy changes audit */}
      <Card>
        <CardHeader><CardTitle>Recent policy changes</CardTitle></CardHeader>
        <CardContent>
          {!policyChanges || policyChanges.changes.length === 0 ? (
            <p className="text-sm text-gray-400">No policy changes recorded yet.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="text-left text-gray-400 uppercase tracking-wide">
                <tr>
                  <th className="py-1">When</th>
                  <th className="py-1">API key</th>
                  <th className="py-1">Field</th>
                  <th className="py-1">By</th>
                  <th className="py-1">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {policyChanges.changes.map(c => (
                  <tr key={c.id}>
                    <td className="py-1.5 text-gray-600 dark:text-gray-400 whitespace-nowrap pr-3">
                      {formatTimeForUser(c.changed_at, user)}
                    </td>
                    <td className="py-1.5 text-gray-700 dark:text-gray-300 pr-3">{keyName(c.api_key_id)}</td>
                    <td className="py-1.5 font-mono text-gray-700 dark:text-gray-300 pr-3">{c.field}</td>
                    <td className="py-1.5 text-gray-600 dark:text-gray-400 pr-3">{c.changed_by ?? 'unknown'}</td>
                    <td className="py-1.5 text-gray-700 dark:text-gray-300 whitespace-pre-wrap">
                      {c.reason ?? '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}


// ── v5.1.0 / Batch C1 — Activity-log toggle ─────────────────────────
function LoggingControlsPanel() {
  const qc = useQueryClient()
  const toast = useToast()
  const { user } = useAuth()
  const { data: status, isLoading } = useQuery({
    queryKey: ['logging-status'],
    queryFn: loggingApi.status,
    refetchInterval: 30_000,
  })
  const [reason, setReason] = useState('')
  const [confirmOff, setConfirmOff] = useState(false)

  const toggleMut = useMutation({
    mutationFn: (enabled: boolean) => loggingApi.toggle(enabled, reason || undefined),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['logging-status'] })
      toast.success(`Activity logging ${r.enabled ? 'ENABLED' : 'DISABLED'}` + (r.noop ? ' (no-op)' : ''))
      setReason('')
      setConfirmOff(false)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  if (isLoading || !status) {
    return (
      <Card>
        <CardContent className="py-3 flex items-center justify-center">
          <Spinner />
        </CardContent>
      </Card>
    )
  }

  const on = status.enabled
  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity Logging</CardTitle>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col sm:flex-row sm:items-start gap-4">
          <div className="flex-1 min-w-0">
            <div className="flex items-center gap-2 flex-wrap">
              <span className={
                'inline-flex items-center gap-1.5 text-sm font-semibold px-2 py-0.5 rounded ' +
                (on
                  ? 'bg-emerald-100 dark:bg-emerald-900/40 text-emerald-800 dark:text-emerald-300'
                  : 'bg-red-100 dark:bg-red-900/40 text-red-800 dark:text-red-300 ring-2 ring-red-500/40')
              }>
                {on ? '● ON' : '◯ OFF'}
              </span>
              <span className="text-sm text-gray-700 dark:text-gray-300">
                {on
                  ? 'capturing full request + response metadata to activity_log + SSE dashboard'
                  : 'NO new activity log entries are being persisted or streamed. Compliance audit (this page) is unaffected.'}
              </span>
            </div>
            {status.last_flip && (
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1.5">
                Last flip: {formatTimeForUser(status.last_flip.changed_at, user)}{' '}
                by <strong>{status.last_flip.changed_by ?? 'unknown'}</strong>
                {status.last_flip.reason && (
                  <> — <span className="italic">{status.last_flip.reason}</span></>
                )}
              </p>
            )}
            <div className="mt-3 flex flex-col sm:flex-row gap-2">
              <input
                type="text"
                placeholder={on
                  ? 'Reason for turning OFF (encouraged for audit)…'
                  : 'Reason for turning ON…'}
                value={reason}
                onChange={e => setReason(e.target.value)}
                className="flex-1 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
                maxLength={2000}
              />
              {on ? (
                <Button
                  size="sm" variant="danger"
                  onClick={() => setConfirmOff(true)}
                  loading={toggleMut.isPending}
                >
                  Turn OFF
                </Button>
              ) : (
                <Button
                  size="sm"
                  onClick={() => toggleMut.mutate(true)}
                  loading={toggleMut.isPending}
                >
                  Turn ON
                </Button>
              )}
            </div>
          </div>
        </div>

        <ConfirmDialog
          open={confirmOff}
          title="Disable activity logging?"
          message={
            'Activity log capture will halt until re-enabled. Existing rows are NOT deleted; the daily ' +
            'compliance audit chain continues for this page. This flip is itself recorded in compliance_policy_changes.'
          }
          confirmLabel="Disable logging"
          variant="danger"
          loading={toggleMut.isPending}
          onConfirm={() => toggleMut.mutate(false)}
          onCancel={() => setConfirmOff(false)}
        />
      </CardContent>
    </Card>
  )
}
