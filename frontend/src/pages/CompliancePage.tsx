// v5.0.0 — Admin CompliancePage. Surfaces compliance_events,
// compliance_policy_changes, and the cluster preflight readiness card
// (decision 32). Filterable + CSV-exportable per spec §3.2 / §9.3.
import { useMemo, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Download, RefreshCw, ShieldAlert, Server } from 'lucide-react'
import { complianceApi, keysApi, loggingApi, type RetentionEntry } from '@/api'
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
      {/* v5.1.1 / Batch C2 — time-range bulk purge */}
      <ActivityLogPurgePanel />
      {/* v5.1.2 / Batch C3 — retention period editable */}
      <RetentionPanel />
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


// ── v5.1.1 / Batch C2 — Time-range bulk purge ──────────────────────
function ActivityLogPurgePanel() {
  const qc = useQueryClient()
  const toast = useToast()
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [reason, setReason] = useState('')
  const [confirmOpen, setConfirmOpen] = useState(false)

  const purgeMut = useMutation({
    mutationFn: () => {
      // Convert local datetime-local inputs to UTC Unix seconds.
      const s = new Date(start).getTime() / 1000
      const e = new Date(end).getTime() / 1000
      return loggingApi.purge(s, e, reason)
    },
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['compliance-events'] })
      const peersFailed = r.peers.filter(p => p.status !== 'ok').length
      if (peersFailed > 0) {
        toast.error(
          `Local deleted ${r.local.deleted}; ${peersFailed}/${r.peers.length} peers failed — retry recommended`,
        )
      } else {
        toast.success(
          `Purged ${r.local.deleted} local${
            r.peers.length ? ` + ${r.peers.reduce((a,p)=>a+p.deleted,0)} across ${r.peers.length} peers` : ''
          }`,
        )
      }
      setStart(''); setEnd(''); setReason('')
      setConfirmOpen(false)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const startMs = start ? new Date(start).getTime() : NaN
  const endMs = end ? new Date(end).getTime() : NaN
  const windowDays = (endMs - startMs) / 86400000
  const validWindow = !!start && !!end && endMs > startMs && windowDays <= 90
  const canSubmit = validWindow && reason.trim().length > 0 && !purgeMut.isPending

  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity Log — Time-range purge</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Permanently delete activity_log rows whose <code className="font-mono text-[10px]">created_at</code> falls in the window.
          Fans out to all peers via HMAC. Window is capped at 90 days. <strong>compliance_events</strong> and <strong>compliance_policy_changes</strong> are
          NOT touched — the audit chain stays intact. Every purge writes a system-scope audit row recording who, when, the window, the row count, and your reason.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Start (local time)</span>
            <input
              type="datetime-local"
              value={start}
              onChange={e => setStart(e.target.value)}
              className="px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-xs font-medium text-gray-700 dark:text-gray-300">End (local time)</span>
            <input
              type="datetime-local"
              value={end}
              onChange={e => setEnd(e.target.value)}
              className="px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
            />
          </label>
        </div>
        <label className="flex flex-col gap-1 mt-3">
          <span className="text-xs font-medium text-gray-700 dark:text-gray-300">Reason (required, captured in audit row)</span>
          <input
            type="text"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="e.g. PII captured during 2026-06-06 cursor-bridge debugging session"
            className="px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
            maxLength={2000}
          />
        </label>
        <div className="mt-3 flex items-center gap-3 flex-wrap">
          {validWindow && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Window: <strong>{windowDays.toFixed(1)} days</strong>
            </span>
          )}
          {start && end && endMs <= startMs && (
            <span className="text-xs text-red-600 dark:text-red-400">End must be after start.</span>
          )}
          {windowDays > 90 && (
            <span className="text-xs text-red-600 dark:text-red-400">Window exceeds 90-day cap.</span>
          )}
          <div className="ml-auto">
            <Button
              size="sm"
              variant="danger"
              onClick={() => setConfirmOpen(true)}
              disabled={!canSubmit}
              loading={purgeMut.isPending}
            >
              Purge window
            </Button>
          </div>
        </div>

        <ConfirmDialog
          open={confirmOpen}
          title="Purge activity log window?"
          message={
            `${windowDays.toFixed(1)} days of activity_log rows will be permanently deleted across this node ` +
            `AND every reachable peer. compliance audit tables are untouched. An audit row is written recording ` +
            `this action.`
          }
          confirmLabel="Purge"
          variant="danger"
          loading={purgeMut.isPending}
          onConfirm={() => purgeMut.mutate()}
          onCancel={() => setConfirmOpen(false)}
        />
      </CardContent>
    </Card>
  )
}


// ── v5.1.2 / Batch C3 — Retention editable ────────────────────────
function RetentionPanel() {
  const qc = useQueryClient()
  const toast = useToast()
  const { data: state, isLoading } = useQuery({
    queryKey: ['retention-state'],
    queryFn: loggingApi.retention,
  })
  const [infoDays,    setInfoDays]    = useState<string>('')
  const [warningDays, setWarningDays] = useState<string>('')
  const [errorDays,   setErrorDays]   = useState<string>('')
  const [reason,      setReason]      = useState<string>('')

  const saveMut = useMutation({
    mutationFn: () => {
      const body: {
        info_days?: number | null
        warning_days?: number | null
        error_days?: number | null
        clear_info?: boolean
        clear_warning?: boolean
        clear_error?: boolean
        reason?: string
      } = { reason: reason || undefined }
      // Send only fields the operator changed. Empty string + the
      // explicit "use default" radio means "clear" (handled by the
      // clear_* flag); a typed number sends that value.
      if (infoDays === 'clear')    body.clear_info = true
      else if (infoDays !== '')    body.info_days = Number(infoDays)
      if (warningDays === 'clear') body.clear_warning = true
      else if (warningDays !== '') body.warning_days = Number(warningDays)
      if (errorDays === 'clear')   body.clear_error = true
      else if (errorDays !== '')   body.error_days = Number(errorDays)
      return loggingApi.setRetention(body)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['retention-state'] })
      toast.success('Retention updated; next prune sweep honors the new values')
      setInfoDays(''); setWarningDays(''); setErrorDays(''); setReason('')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  if (isLoading || !state) {
    return (
      <Card>
        <CardContent className="py-3 flex items-center justify-center">
          <Spinner />
        </CardContent>
      </Card>
    )
  }

  function row(
    label: string, color: 'gray' | 'amber' | 'red',
    entry: RetentionEntry,
    value: string, setter: (v: string) => void,
  ) {
    const pill = (
      color === 'gray'  ? 'bg-gray-100 dark:bg-gray-800 text-gray-700 dark:text-gray-300' :
      color === 'amber' ? 'bg-amber-100 dark:bg-amber-900/40 text-amber-800 dark:text-amber-300' :
                          'bg-red-100   dark:bg-red-900/40   text-red-800   dark:text-red-300'
    )
    return (
      <div className="grid grid-cols-1 sm:grid-cols-12 gap-2 items-center py-2 border-b border-gray-200 dark:border-gray-700 last:border-0">
        <div className="sm:col-span-3">
          <span className={`inline-block text-xs font-semibold uppercase px-2 py-0.5 rounded ${pill}`}>{label}</span>
        </div>
        <div className="sm:col-span-3 text-xs text-gray-500 dark:text-gray-400">
          env default <strong>{entry.env_default}d</strong> ·
          override <strong>{entry.override ?? '—'}</strong> ·
          effective <strong>{entry.effective_days}d</strong>
        </div>
        <div className="sm:col-span-6 flex items-center gap-2">
          <input
            type="number" min={1} max={36500}
            value={value === 'clear' ? '' : value}
            onChange={e => setter(e.target.value)}
            placeholder={`new days (default ${entry.env_default})`}
            className="flex-1 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
          />
          <label className="flex items-center gap-1 text-xs text-gray-500 dark:text-gray-400">
            <input
              type="checkbox"
              checked={value === 'clear'}
              onChange={e => setter(e.target.checked ? 'clear' : '')}
            />
            use default
          </label>
        </div>
      </div>
    )
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Activity Log Retention</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-xs text-gray-500 dark:text-gray-400 mb-3">
          Per-severity retention. Each severity-tier of activity_log row is auto-deleted
          after its number of days. Override the env default per tier; clearing the override
          falls back to env. Edits land in <code className="font-mono text-[10px]">system_settings</code>{' '}
          (cluster-synced) and the next prune sweep honors them within ~24 hours.
        </p>
        <div className="rounded border border-gray-200 dark:border-gray-700">
          {row('info',    'gray',  state.info,    infoDays,    setInfoDays)}
          {row('warning', 'amber', state.warning, warningDays, setWarningDays)}
          {row('error',   'red',   state.error,   errorDays,   setErrorDays)}
        </div>
        <div className="mt-3 flex flex-col sm:flex-row gap-2 items-stretch">
          <input
            type="text"
            value={reason}
            onChange={e => setReason(e.target.value)}
            placeholder="Reason (captured in audit row)"
            className="flex-1 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
            maxLength={2000}
          />
          <Button
            size="sm"
            onClick={() => saveMut.mutate()}
            loading={saveMut.isPending}
            disabled={infoDays === '' && warningDays === '' && errorDays === ''}
          >
            Save retention edits
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
