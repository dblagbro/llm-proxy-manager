import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Plus, RefreshCw, Search, ChevronDown, ChevronUp, Trash2, Edit2, ToggleLeft, ToggleRight, Play, FileText } from 'lucide-react'
import { providersApi, clusterApi, monitoringApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { CircuitBreakerBadge } from '@/components/providers/CircuitBreakerBadge'
import { useToast } from '@/components/ui/Toast'
import type { Provider } from '@/types'
import { ProviderModels } from '@/components/providers/ProviderModels'
import { useAuth } from '@/context/AuthContext'
import { formatTimeForUser } from '@/utils/time'
import { ProviderForm, type ProviderFormState, emptyProviderForm, providerToForm } from '@/components/providers/ProviderForm'
import { clsx } from 'clsx'


export function ProvidersPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [editing, setEditing] = useState<Provider | null>(null)
  const [form, setForm] = useState<ProviderFormState>(emptyProviderForm())
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; response?: string; error?: string }>>({})
  const [testingId, setTestingId] = useState<string | null>(null)

  const { data: providers, isLoading } = useQuery({ queryKey: ['providers'], queryFn: providersApi.list, refetchInterval: 30_000 })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: clusterApi.health, refetchInterval: 15_000 })
  // v3.0.6: per-provider metrics (24h window) shown inline on each card.
  // Lookup by provider_id; fall back to zeros when no data yet.
  const { data: metrics } = useQuery({
    queryKey: ['metrics', 24],
    queryFn: () => monitoringApi.metrics(24),
    refetchInterval: 60_000,
  })
  const metricsByProvider: Record<string, {
    requests: number; success_rate: number; avg_latency_ms: number; total_cost_usd: number; total_tokens: number;
  }> = {}
  for (const m of metrics?.providers ?? []) {
    metricsByProvider[m.provider_id] = m
  }

  // Sort dropdown for the providers list — cards aren't column-clickable so
  // we expose the sort dimension as a small select. Default: priority asc
  // (matches routing precedence).
  const [sortBy, setSortBy] = useState<'priority' | 'name' | 'requests' | 'success_rate' | 'avg_latency_ms' | 'total_cost_usd'>('priority')

  const saveMutation = useMutation({
    mutationFn: async (data: ProviderFormState) => {
      // v2.7.1 / v3.0.15: OAuth providers (claude-oauth or codex-oauth) with an
      // authorize_url + callback go through their type-specific exchange/rotate
      // endpoints instead of the plain POST/PUT.
      const isOAuthExchange =
        data.oauth_state && data.oauth_callback &&
        (data.provider_type === 'claude-oauth' || data.provider_type === 'codex-oauth')
      if (!editing && isOAuthExchange) {
        const payload = {
          state: data.oauth_state,
          callback: data.oauth_callback,
          name: data.name,
          default_model: data.default_model || undefined,
          base_url: data.base_url || undefined,
          priority: data.priority,
          enabled: data.enabled,
          timeout_sec: data.timeout_sec,
          exclude_from_tool_requests: data.exclude_from_tool_requests,
          hold_down_sec: data.hold_down_sec,
          failure_threshold: data.failure_threshold,
          extra_config: data.extra_config,
        }
        return data.provider_type === 'codex-oauth'
          ? providersApi.codexOauthExchange(payload)
          : providersApi.oauthExchange(payload)
      }
      // v2.7.7 / v3.0.15: re-auth in place when editing with state+callback.
      if (editing && isOAuthExchange) {
        const rotatePayload = { state: data.oauth_state, callback: data.oauth_callback }
        if (data.provider_type === 'codex-oauth') {
          await providersApi.codexOauthRotate(editing.id, rotatePayload)
        } else {
          await providersApi.oauthRotate(editing.id, rotatePayload)
        }
        return providersApi.update(editing.id, data)
      }
      return editing ? providersApi.update(editing.id, data) : providersApi.create(data)
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      toast.success(editing ? 'Provider updated' : 'Provider created')
      closeModal()
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const toggleMutation = useMutation({
    mutationFn: (id: string) => providersApi.toggle(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['providers'] }),
    onError: (e: Error) => toast.error(e.message),
  })

  const clearAuthFailMutation = useMutation({
    mutationFn: (id: string) => providersApi.clearAuthFailure(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      toast.success('Auth-failure flag cleared')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => providersApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      toast.success('Provider deleted')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const scanMutation = useMutation({
    mutationFn: (id: string) => providersApi.scanModels(id),
    onSuccess: (data, id) => {
      qc.invalidateQueries({ queryKey: ['providers'] })
      qc.invalidateQueries({ queryKey: ['capabilities', id] })
      if (data.warning) {
        toast.error(data.warning)
      } else {
        toast.success(`Scanned ${data.scanned} model${data.scanned !== 1 ? 's' : ''}`)
      }
      setExpanded(id)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function openCreate() {
    setEditing(null)
    setForm(emptyProviderForm())
    setShowModal(true)
  }

  function openEdit(p: Provider) {
    setEditing(p)
    setForm(providerToForm(p))
    setShowModal(true)
  }

  function closeModal() {
    setShowModal(false)
    setEditing(null)
  }

  async function handleTest(id: string) {
    setTestingId(id)
    try {
      const res = await providersApi.test(id)
      setTestResults(prev => ({ ...prev, [id]: res }))
    } catch (e: unknown) {
      setTestResults(prev => ({ ...prev, [id]: { success: false, error: (e as Error).message } }))
    } finally {
      setTestingId(null)
    }
  }

  const filtered = (providers ?? []).filter(p =>
    p.name.toLowerCase().includes(search.toLowerCase()) ||
    p.provider_type.toLowerCase().includes(search.toLowerCase())
  )
  // v3.0.6: sort by metrics dimension. Priority is asc (lower priority
  // number = higher routing precedence); name is alpha asc; everything
  // else is desc (more interesting first).
  filtered.sort((a, b) => {
    const ma = metricsByProvider[a.id]
    const mb = metricsByProvider[b.id]
    switch (sortBy) {
      case 'priority': return (a.priority ?? 0) - (b.priority ?? 0)
      case 'name':     return a.name.localeCompare(b.name)
      case 'requests': return (mb?.requests ?? 0) - (ma?.requests ?? 0)
      case 'success_rate': return (mb?.success_rate ?? 0) - (ma?.success_rate ?? 0)
      case 'avg_latency_ms': return (mb?.avg_latency_ms ?? 0) - (ma?.avg_latency_ms ?? 0)
      case 'total_cost_usd': return (mb?.total_cost_usd ?? 0) - (ma?.total_cost_usd ?? 0)
    }
  })

  // v2.7.8 BUG-010: identify priorities shared by ≥2 enabled providers so the
  // UI can surface a yellow warning ("ties broken by created_at order").
  const priorityTies = new Set<number>()
  {
    const counts = new Map<number, number>()
    for (const p of (providers ?? [])) {
      if (!p.enabled) continue
      counts.set(p.priority, (counts.get(p.priority) ?? 0) + 1)
    }
    for (const [pri, n] of counts) if (n > 1) priorityTies.add(pri)
  }

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Providers</h1>
          <p className="text-sm text-gray-500 mt-0.5">{providers?.length ?? 0} configured</p>
        </div>
        <Button onClick={openCreate} size="sm"><Plus className="h-4 w-4 mr-1.5" />Add Provider</Button>
      </div>

      <div className="flex items-center gap-3 flex-wrap">
        <div className="relative max-w-sm flex-1 min-w-[14rem]">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Filter providers…"
            className="w-full pl-9 pr-3 py-2 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
          />
        </div>
        <label className="text-xs text-gray-500 dark:text-gray-400 flex items-center gap-2 shrink-0">
          Sort by
          <select
            value={sortBy}
            onChange={e => setSortBy(e.target.value as typeof sortBy)}
            className="text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-md px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-indigo-500"
          >
            <option value="priority">Priority</option>
            <option value="name">Name</option>
            <option value="requests">Requests (24h)</option>
            <option value="success_rate">Success rate (24h)</option>
            <option value="avg_latency_ms">Avg latency (24h)</option>
            <option value="total_cost_usd">Cost (24h)</option>
          </select>
        </label>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : filtered.length === 0 ? (
        <Card><CardContent><p className="text-center text-gray-500 py-10">No providers found</p></CardContent></Card>
      ) : (
        <div className="space-y-3">
          {filtered.map(p => {
            const cb = health?.circuitBreakers?.[p.id]
            const test = testResults[p.id]
            const open = expanded === p.id
            return (
              <Card key={p.id}>
                <div
                  className="flex items-center gap-3 px-5 py-4 cursor-pointer"
                  onClick={() => setExpanded(open ? null : p.id)}
                >
                  <div className={clsx('h-2.5 w-2.5 rounded-full shrink-0', p.enabled ? 'bg-green-500' : 'bg-gray-400')} />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-gray-900 dark:text-gray-100">{p.name}</p>
                    <p className="text-xs text-gray-500">{p.provider_type} · {p.default_model ?? 'no default model'} · priority {p.priority}</p>
                    {/* v3.0.6: per-provider 24h metrics inline. Hidden when no
                        traffic to that provider, so configured-but-unused
                        providers stay clean. */}
                    {(() => {
                      const m = metricsByProvider[p.id]
                      if (!m || m.requests === 0) return null
                      const latency = m.avg_latency_ms > 1000
                        ? `${(m.avg_latency_ms / 1000).toFixed(1)}s`
                        : `${Math.round(m.avg_latency_ms)}ms`
                      const cost = m.total_cost_usd === 0
                        ? '$0.00'
                        : m.total_cost_usd < 0.01
                          ? `$${m.total_cost_usd.toFixed(5)}`
                          : `$${m.total_cost_usd.toFixed(3)}`
                      const successColor = m.success_rate >= 95
                        ? 'text-green-600 dark:text-green-400'
                        : m.success_rate >= 80
                          ? 'text-amber-500'
                          : 'text-red-500'
                      return (
                        <p className="text-xs text-gray-500 mt-1 font-mono">
                          24h: {m.requests.toLocaleString()} req
                          <span className="mx-1.5">·</span>
                          <span className={successColor}>{m.success_rate.toFixed(1)}%</span>
                          <span className="mx-1.5">·</span>
                          {latency}
                          <span className="mx-1.5">·</span>
                          {m.total_tokens.toLocaleString()} tok
                          <span className="mx-1.5">·</span>
                          {cost}
                        </p>
                      )
                    })()}
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {/* v2.7.8 BUG-002: needs re-auth (admin must re-key).
                        Click expands the panel where the "Mark Re-Authed"
                        button + last-error detail live. */}
                    {p.auth_failed && (
                      <span title={p.auth_failed.last_error || 'auth failure'}>
                        <Badge
                          variant="danger"
                          onClick={(e) => { e.stopPropagation(); setExpanded(p.id) }}
                          className="cursor-pointer"
                        >
                          Needs re-auth
                        </Badge>
                      </span>
                    )}
                    {/* v2.7.8 BUG-010: priority tie warning */}
                    {p.enabled && priorityTies.has(p.priority) && (
                      <span title={`Multiple enabled providers share priority ${p.priority}; tiebreaker is creation order.`}>
                        <Badge variant="warning">Priority tie</Badge>
                      </span>
                    )}
                    {/*
                      Single status badge, most-specific-wins:
                      1. Last test result (if run this session)
                      2. Circuit-breaker state (live)
                    */}
                    {test ? (
                      <span title="Last test result this session">
                        <Badge variant={test.success ? 'success' : 'danger'}>
                          {test.success ? 'Test OK' : 'Test failed'}
                        </Badge>
                      </span>
                    ) : (
                      <CircuitBreakerBadge state={(cb?.state as 'closed' | 'open' | 'half-open') ?? 'closed'} />
                    )}
                    {open ? <ChevronUp className="h-4 w-4 text-gray-400" /> : <ChevronDown className="h-4 w-4 text-gray-400" />}
                  </div>
                </div>

                {open && (
                  <div className="border-t border-gray-100 dark:border-gray-700 px-5 py-4 space-y-4">
                    {/* v2.8.0: re-auth drill-in panel — only when auth_failed is set */}
                    {p.auth_failed && (
                      <div className="rounded-md border border-red-200 dark:border-red-900/50 bg-red-50/60 dark:bg-red-950/30 p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="min-w-0 flex-1">
                            <p className="text-sm font-medium text-red-700 dark:text-red-300">
                              Provider failed authentication — needs re-auth
                            </p>
                            <p className="text-xs text-gray-600 dark:text-gray-400 mt-0.5">
                              Failed since {formatTimeForUser(p.auth_failed.since * 1000, user)}
                              {' · '}
                              {(p.provider_type === 'claude-oauth' || p.provider_type === 'codex-oauth')
                                ? 'Open Edit and click Generate New Auth URL to re-authorize.'
                                : 'Open Edit and paste a fresh API key.'}
                            </p>
                            <pre className="mt-2 text-[11px] font-mono text-red-700 dark:text-red-400 whitespace-pre-wrap break-all">{p.auth_failed.last_error}</pre>
                          </div>
                          <div className="flex flex-col gap-1.5 shrink-0">
                            <Button
                              size="sm"
                              variant="outline"
                              onClick={() => openEdit(p)}
                            >
                              <Edit2 className="h-3.5 w-3.5 mr-1" />Edit / Re-auth
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              onClick={() => clearAuthFailMutation.mutate(p.id)}
                              loading={clearAuthFailMutation.isPending}
                              title="Clear the failed flag without changing the key. Useful when admin re-keyed externally."
                            >
                              Mark re-authed
                            </Button>
                          </div>
                        </div>
                      </div>
                    )}
                    <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                      <div><p className="text-xs text-gray-400 mb-1">Base URL</p><p className="truncate text-gray-700 dark:text-gray-300">{p.base_url || '—'}</p></div>
                      <div><p className="text-xs text-gray-400 mb-1">Timeout</p><p className="text-gray-700 dark:text-gray-300">{p.timeout_sec}s</p></div>
                      <div>
                        <p className="text-xs text-gray-400 mb-1">Hold-down</p>
                        <p className="text-gray-700 dark:text-gray-300">
                          {cb?.hold_down_remaining ? <span className="text-amber-500">{Math.ceil(cb.hold_down_remaining)}s remaining</span> : p.hold_down_sec ? `${p.hold_down_sec}s` : `${120}s (global)`}
                        </p>
                      </div>
                      <div>
                        <p className="text-xs text-gray-400 mb-1">Fail threshold</p>
                        <p className="text-gray-700 dark:text-gray-300">{p.failure_threshold ?? '3 (global)'}</p>
                      </div>
                    </div>
                    {!test?.success && test?.error && <p className="text-xs text-red-400 bg-red-900/10 rounded p-2">{test.error}</p>}
                    <ProviderModels providerId={p.id} />
                    <div className="flex gap-2 flex-wrap">
                      <Button size="sm" variant="outline" onClick={() => handleTest(p.id)} loading={testingId === p.id}>
                        <Play className="h-3.5 w-3.5 mr-1" />Test
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => scanMutation.mutate(p.id)} loading={scanMutation.isPending}>
                        <RefreshCw className="h-3.5 w-3.5 mr-1" />Scan Models
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => toggleMutation.mutate(p.id)} loading={toggleMutation.isPending}>
                        {p.enabled ? <ToggleRight className="h-3.5 w-3.5 mr-1" /> : <ToggleLeft className="h-3.5 w-3.5 mr-1" />}
                        {p.enabled ? 'Disable' : 'Enable'}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => openEdit(p)}>
                        <Edit2 className="h-3.5 w-3.5 mr-1" />Edit
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => navigate(`/activity?provider=${p.id}`)}>
                        <FileText className="h-3.5 w-3.5 mr-1" />Logs
                      </Button>
                      <Button size="sm" variant="danger" onClick={() => setDeleteId(p.id)}>
                        <Trash2 className="h-3.5 w-3.5 mr-1" />Delete
                      </Button>
                    </div>
                  </div>
                )}
              </Card>
            )
          })}
        </div>
      )}

      {/* Create/Edit Modal */}
      <Modal open={showModal} onClose={closeModal} size="lg">
        <ModalHeader onClose={closeModal}>{editing ? 'Edit Provider' : 'Add Provider'}</ModalHeader>
        <ModalBody>
          <ProviderForm form={form} onChange={setForm} editing={!!editing} />
        </ModalBody>
        <ModalFooter>
          <Button variant="ghost" onClick={closeModal}>Cancel</Button>
          <Button onClick={() => saveMutation.mutate(form)} loading={saveMutation.isPending}>
            {editing ? 'Save Changes' : 'Create Provider'}
          </Button>
        </ModalFooter>
      </Modal>

      <ConfirmDialog
        open={!!deleteId}
        title="Delete Provider"
        message="This will permanently remove the provider and all its data. Are you sure?"
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { deleteMutation.mutate(deleteId!); setDeleteId(null) }}
        onCancel={() => setDeleteId(null)}
      />
    </div>
  )
}
