import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Plus, RefreshCw, Search, ChevronDown, ChevronUp, Trash2, Edit2, ToggleLeft, ToggleRight, Play, FileText } from 'lucide-react'
import { providersApi, clusterApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Spinner } from '@/components/ui/Spinner'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { CircuitBreakerBadge } from '@/components/providers/CircuitBreakerBadge'
import { useToast } from '@/components/ui/Toast'
import type { Provider, ProviderType, ProviderFormData, ModelCapability } from '@/types'
import { clsx } from 'clsx'

function ProviderModels({ providerId }: { providerId: string }) {
  const { data: caps, isLoading } = useQuery<ModelCapability[]>({
    queryKey: ['capabilities', providerId],
    queryFn: () => providersApi.capabilities(providerId),
  })
  if (isLoading) return <div className="text-xs text-gray-400 py-2">Loading models…</div>
  if (!caps || caps.length === 0) return (
    <p className="text-xs text-gray-400 py-2">No models indexed — click <strong>Scan Models</strong> to discover them.</p>
  )
  return (
    <div className="mt-1">
      <p className="text-xs text-gray-400 mb-2 font-medium">{caps.length} model{caps.length !== 1 ? 's' : ''} indexed</p>
      <div className="overflow-x-auto">
        <table className="w-full text-xs border-collapse">
          <thead>
            <tr className="text-left text-gray-400 border-b border-gray-200 dark:border-gray-700">
              <th className="pb-1 pr-4 font-medium">Model ID</th>
              <th className="pb-1 pr-4 font-medium">Tasks</th>
              <th className="pb-1 pr-4 font-medium">Cost</th>
              <th className="pb-1 pr-4 font-medium">Latency</th>
              <th className="pb-1 pr-4 font-medium">Context</th>
              <th className="pb-1 font-medium">Features</th>
            </tr>
          </thead>
          <tbody>
            {caps.map(c => (
              <tr key={c.id} className="border-b border-gray-100 dark:border-gray-800 last:border-0">
                <td className="py-1 pr-4 font-mono text-gray-700 dark:text-gray-300 whitespace-nowrap">{c.model_id}</td>
                <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.tasks.join(', ') || '—'}</td>
                <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.cost_tier}</td>
                <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.latency}</td>
                <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.context_length >= 1000 ? `${Math.round(c.context_length / 1000)}k` : c.context_length}</td>
                <td className="py-1 text-gray-500 dark:text-gray-500 whitespace-nowrap">
                  {c.native_reasoning && <span title="Reasoning" className="mr-1">🧠</span>}
                  {c.native_tools && <span title="Tool use" className="mr-1">🔧</span>}
                  {c.native_vision && <span title="Vision" className="mr-1">👁</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const PROVIDER_TYPES: ProviderType[] = ['anthropic', 'openai', 'google', 'vertex', 'grok', 'ollama', 'compatible']

type ProviderForm = Omit<ProviderFormData, 'extra_config'> & { api_key?: string; extra_config: Record<string, unknown>; hold_down_sec: number | null; failure_threshold: number | null }

function emptyForm(): ProviderForm {
  return {
    name: '',
    provider_type: 'openai',
    api_key: '',
    base_url: '',
    default_model: '',
    priority: 10,
    enabled: true,
    timeout_sec: 60,
    exclude_from_tool_requests: false,
    hold_down_sec: null,
    failure_threshold: null,
    extra_config: {},
  }
}

export function ProvidersPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const [showModal, setShowModal] = useState(false)
  const [editing, setEditing] = useState<Provider | null>(null)
  const [form, setForm] = useState<ProviderForm>(emptyForm())
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<Record<string, { success: boolean; response?: string; error?: string }>>({})
  const [testingId, setTestingId] = useState<string | null>(null)

  const { data: providers, isLoading } = useQuery({ queryKey: ['providers'], queryFn: providersApi.list, refetchInterval: 30_000 })
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: clusterApi.health, refetchInterval: 15_000 })

  const saveMutation = useMutation({
    mutationFn: (data: ProviderForm) =>
      editing ? providersApi.update(editing.id, data) : providersApi.create(data),
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
    setForm(emptyForm())
    setShowModal(true)
  }

  function openEdit(p: Provider) {
    setEditing(p)
    setForm({
      name: p.name,
      provider_type: p.provider_type,
      api_key: '',
      base_url: p.base_url ?? '',
      default_model: p.default_model ?? '',
      priority: p.priority,
      enabled: p.enabled,
      timeout_sec: p.timeout_sec,
      exclude_from_tool_requests: p.exclude_from_tool_requests,
      hold_down_sec: p.hold_down_sec ?? null,
      failure_threshold: p.failure_threshold ?? null,
      extra_config: p.extra_config ?? {},
    })
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

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Providers</h1>
          <p className="text-sm text-gray-500 mt-0.5">{providers?.length ?? 0} configured</p>
        </div>
        <Button onClick={openCreate} size="sm"><Plus className="h-4 w-4 mr-1.5" />Add Provider</Button>
      </div>

      <div className="relative max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
        <input
          value={search}
          onChange={e => setSearch(e.target.value)}
          placeholder="Filter providers…"
          className="w-full pl-9 pr-3 py-2 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
        />
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
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    <CircuitBreakerBadge state={(cb?.state as 'closed' | 'open' | 'half-open') ?? 'closed'} />
                    {test && (
                      <Badge variant={test.success ? 'success' : 'danger'}>
                        {test.success ? 'OK' : 'Error'}
                      </Badge>
                    )}
                    {open ? <ChevronUp className="h-4 w-4 text-gray-400" /> : <ChevronDown className="h-4 w-4 text-gray-400" />}
                  </div>
                </div>

                {open && (
                  <div className="border-t border-gray-100 dark:border-gray-700 px-5 py-4 space-y-4">
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
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input
              label="Name"
              value={form.name ?? ''}
              onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
              required
            />
            <div className="flex flex-col gap-1.5">
              <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Provider Type</label>
              <select
                value={form.provider_type ?? 'openai'}
                onChange={e => setForm(f => ({ ...f, provider_type: e.target.value as ProviderType }))}
                className="px-3 py-2 bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
              >
                {PROVIDER_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            </div>
            <Input
              label={editing ? 'API Key (leave blank to keep current)' : 'API Key'}
              type="password"
              value={form.api_key ?? ''}
              onChange={e => setForm(f => ({ ...f, api_key: e.target.value }))}
              required={!editing}
            />
            <Input
              label="Base URL (optional)"
              value={form.base_url ?? ''}
              onChange={e => setForm(f => ({ ...f, base_url: e.target.value }))}
              placeholder="https://api.example.com"
            />
            <Input
              label="Default Model"
              value={form.default_model ?? ''}
              onChange={e => setForm(f => ({ ...f, default_model: e.target.value }))}
              placeholder="e.g. gpt-4o"
            />
            <Input
              label="Priority (lower = preferred)"
              type="number"
              value={String(form.priority ?? 10)}
              onChange={e => setForm(f => ({ ...f, priority: Number(e.target.value) }))}
            />
            <Input
              label="Timeout (seconds)"
              type="number"
              value={String(form.timeout_sec ?? 60)}
              onChange={e => setForm(f => ({ ...f, timeout_sec: Number(e.target.value) }))}
            />
            <Input
              label="Hold-down after failure (seconds, blank = global 120s)"
              type="number"
              value={form.hold_down_sec == null ? '' : String(form.hold_down_sec)}
              onChange={e => setForm(f => ({ ...f, hold_down_sec: e.target.value === '' ? null : Number(e.target.value) }))}
              placeholder="120"
            />
            <Input
              label="Failure threshold before trip (blank = global 3)"
              type="number"
              value={form.failure_threshold == null ? '' : String(form.failure_threshold)}
              onChange={e => setForm(f => ({ ...f, failure_threshold: e.target.value === '' ? null : Number(e.target.value) }))}
              placeholder="3"
            />
            <div className="flex items-center gap-3 mt-5">
              <label className="text-sm text-gray-700 dark:text-gray-300">Exclude from tool requests</label>
              <input
                type="checkbox"
                checked={!!form.exclude_from_tool_requests}
                onChange={e => setForm(f => ({ ...f, exclude_from_tool_requests: e.target.checked }))}
                className="h-4 w-4 accent-indigo-600"
              />
            </div>
          </div>
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
