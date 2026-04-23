import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Eye } from 'lucide-react'
import { keysApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { CopyButton } from '@/components/ui/CopyButton'
import { Spinner } from '@/components/ui/Spinner'
import { useToast } from '@/components/ui/Toast'
import type { ApiKey, KeyType } from '@/types'

const KEY_TYPES: KeyType[] = ['standard', 'claude-code']

export function APIKeysPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const [showCreate, setShowCreate] = useState(false)
  const [newKey, setNewKey] = useState<{ raw: string; prefix: string } | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [editLimits, setEditLimits] = useState<ApiKey | null>(null)
  const [viewDetails, setViewDetails] = useState<ApiKey | null>(null)
  const [capInput, setCapInput] = useState('')
  const [rpmInput, setRpmInput] = useState('')
  const [form, setForm] = useState({ name: '', key_type: 'standard' as KeyType, rate_limit_rpm: '' })
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showBulkConfirm, setShowBulkConfirm] = useState(false)

  const { data: keys, isLoading } = useQuery({ queryKey: ['apikeys'], queryFn: keysApi.list })

  const createMutation = useMutation({
    mutationFn: () => keysApi.create({
      name: form.name,
      key_type: form.key_type,
      rate_limit_rpm: form.rate_limit_rpm ? Number(form.rate_limit_rpm) : undefined,
    }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      // Keep Create modal open so the raw key displays inline;
      // user must click "Done" to dismiss.
      setNewKey({ raw: data.raw_key, prefix: data.key_prefix })
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function closeCreateModal() {
    setShowCreate(false)
    setNewKey(null)
    setForm({ name: '', key_type: 'standard', rate_limit_rpm: '' })
  }

  const updateLimitsMutation = useMutation({
    mutationFn: ({ id, cap, rpm }: { id: string; cap: number | null; rpm: number | null }) =>
      keysApi.update(id, {
        spending_cap_usd: cap === null ? -1 : cap,
        rate_limit_rpm: rpm === null ? -1 : rpm,
      } as any),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      toast.success('Limits updated')
      setEditLimits(null)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => keysApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      toast.success('API key deleted')
    },
    onError: (e: Error) => toast.error(e.message),
  })

  const bulkDeleteMutation = useMutation({
    mutationFn: (ids: string[]) => keysApi.bulkDelete(ids),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      setSelectedIds(new Set())
      setShowBulkConfirm(false)
      toast.success(`${data.deleted} key${data.deleted === 1 ? '' : 's'} deleted`)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function toggleSelect(id: string) {
    setSelectedIds(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  function toggleSelectAll() {
    if (!keys) return
    if (selectedIds.size === keys.length) {
      setSelectedIds(new Set())
    } else {
      setSelectedIds(new Set(keys.map(k => k.id)))
    }
  }

  const allSelected = !!keys && keys.length > 0 && selectedIds.size === keys.length

  function openLimitsEdit(k: ApiKey) {
    setEditLimits(k)
    setCapInput(k.spending_cap_usd != null ? String(k.spending_cap_usd) : '')
    setRpmInput(k.rate_limit_rpm != null ? String(k.rate_limit_rpm) : '')
  }

  function saveLimits() {
    if (!editLimits) return
    const capRaw = capInput.trim()
    const rpmRaw = rpmInput.trim()
    const cap = capRaw === '' ? null : Number(capRaw)
    const rpm = rpmRaw === '' ? null : Number(rpmRaw)
    if (capRaw !== '' && (isNaN(cap!) || cap! < 0)) {
      toast.error('Spending cap must be a positive number or blank')
      return
    }
    if (rpmRaw !== '' && (isNaN(rpm!) || rpm! < 1 || !Number.isInteger(rpm))) {
      toast.error('Rate limit must be a positive integer or blank')
      return
    }
    updateLimitsMutation.mutate({ id: editLimits.id, cap, rpm })
  }

  function fmtDate(ts: string) {
    return new Date(ts).toLocaleDateString()
  }

  function capLabel(k: ApiKey) {
    return k.spending_cap_usd != null ? `$${k.spending_cap_usd.toFixed(2)}` : '∞'
  }

  function capColor(k: ApiKey) {
    if (k.spending_cap_usd == null) return 'text-gray-400'
    const pct = k.total_cost_usd / k.spending_cap_usd
    if (pct >= 1) return 'text-red-600 font-semibold'
    if (pct >= 0.8) return 'text-amber-500 font-semibold'
    return 'text-gray-700 dark:text-gray-300'
  }

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">API Keys</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {keys?.length ?? 0} keys
            {selectedIds.size > 0 && <> · {selectedIds.size} selected</>}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {selectedIds.size > 0 && (
            <Button size="sm" variant="danger" onClick={() => setShowBulkConfirm(true)}>
              <Trash2 className="h-4 w-4 mr-1.5" />Delete {selectedIds.size}
            </Button>
          )}
          <Button size="sm" onClick={() => setShowCreate(true)}><Plus className="h-4 w-4 mr-1.5" />Create Key</Button>
        </div>
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : (keys?.length ?? 0) === 0 ? (
        <Card><CardContent><p className="text-center text-gray-500 py-10">No API keys yet</p></CardContent></Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            {/* Select-all header */}
            <div className="flex items-center gap-4 px-5 py-2.5 bg-gray-50 dark:bg-gray-800/50 border-b border-gray-100 dark:border-gray-700">
              <input
                type="checkbox"
                checked={allSelected}
                ref={el => { if (el) el.indeterminate = selectedIds.size > 0 && !allSelected }}
                onChange={toggleSelectAll}
                className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                aria-label="Select all API keys"
              />
              <span className="text-xs text-gray-500 dark:text-gray-400">
                {selectedIds.size === 0
                  ? 'Select all'
                  : allSelected
                    ? 'All selected — click to clear'
                    : `${selectedIds.size} of ${keys!.length} selected`}
              </span>
            </div>
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {keys!.map(k => (
                <div key={k.id} className={`flex items-center gap-4 px-5 py-4 ${selectedIds.has(k.id) ? 'bg-indigo-50/50 dark:bg-indigo-900/10' : ''}`}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(k.id)}
                    onChange={() => toggleSelect(k.id)}
                    className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                    aria-label={`Select ${k.name || k.key_prefix}`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5">
                      <p className="font-medium text-gray-900 dark:text-gray-100">{k.name || '(unnamed)'}</p>
                      <Badge variant={k.key_type === 'claude-code' ? 'info' : 'default'}>{k.key_type}</Badge>
                    </div>
                    <p className="text-xs text-gray-500 font-mono">{k.key_prefix}…</p>
                  </div>
                  <div className="hidden md:grid grid-cols-5 gap-5 text-right text-sm">
                    <div>
                      <p className="text-xs text-gray-400">Requests</p>
                      <p className="font-medium text-gray-700 dark:text-gray-300">{k.total_requests.toLocaleString()}</p>
                    </div>
                    <div>
                      <p className="text-xs text-gray-400">Tokens</p>
                      <p className="font-medium text-gray-700 dark:text-gray-300">{k.total_tokens.toLocaleString()}</p>
                    </div>
                    <div>
                      <p className="text-xs text-gray-400">Cost</p>
                      <p className="font-medium text-gray-700 dark:text-gray-300">${k.total_cost_usd.toFixed(3)}</p>
                    </div>
                    <div>
                      <p className="text-xs text-gray-400">Cap</p>
                      <p className={`text-sm ${capColor(k)}`}>{capLabel(k)}</p>
                    </div>
                    <div>
                      <p className="text-xs text-gray-400">Rate limit</p>
                      <p className="text-sm text-gray-700 dark:text-gray-300">
                        {k.rate_limit_rpm != null ? `${k.rate_limit_rpm}/min` : '∞'}
                      </p>
                    </div>
                  </div>
                  <p className="text-xs text-gray-400 hidden lg:block shrink-0">Created {fmtDate(k.created_at)}</p>
                  <button
                    onClick={() => setViewDetails(k)}
                    className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                    title="View details"
                  >
                    <Eye className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => openLimitsEdit(k)}
                    className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                    title="Edit limits"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <Button size="sm" variant="danger" onClick={() => setDeleteId(k.id)}>
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Create Modal — shows form first, then the raw key inline after success */}
      <Modal open={showCreate} onClose={newKey ? () => {} : closeCreateModal}>
        <ModalHeader onClose={newKey ? () => {} : closeCreateModal}>
          {newKey ? 'Your New API Key' : 'Create API Key'}
        </ModalHeader>
        <ModalBody>
          {newKey ? (
            <div className="space-y-3">
              <p className="text-sm text-amber-600 dark:text-amber-400 font-medium">
                ⚠ Copy this key now — it will NOT be shown again. Losing it means generating a new one.
              </p>
              <div className="flex items-center gap-2 bg-gray-100 dark:bg-gray-800 rounded-lg px-4 py-3">
                <code className="flex-1 text-sm font-mono text-gray-900 dark:text-gray-100 break-all select-all">{newKey.raw}</code>
                <CopyButton text={newKey.raw} />
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Prefix: <code className="font-mono">{newKey.prefix}…</code> (used to identify the key in the UI and logs)
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              <Input
                label="Key Name (optional)"
                value={form.name}
                onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                placeholder="e.g. production-app"
              />
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Key Type</label>
                <select
                  value={form.key_type}
                  onChange={e => setForm(f => ({ ...f, key_type: e.target.value as KeyType }))}
                  className="px-3 py-2 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
                >
                  {KEY_TYPES.map(t => <option key={t} value={t}>{t}</option>)}
                </select>
                {form.key_type === 'claude-code' && (
                  <p className="text-xs text-amber-500">Claude Code keys automatically enable CoT-E for non-reasoning models.</p>
                )}
              </div>
              <Input
                label="Rate Limit (requests/minute, blank = unlimited)"
                type="number"
                value={form.rate_limit_rpm}
                onChange={e => setForm(f => ({ ...f, rate_limit_rpm: e.target.value }))}
              />
            </div>
          )}
        </ModalBody>
        <ModalFooter>
          {newKey ? (
            <Button onClick={closeCreateModal}>I've saved the key — Done</Button>
          ) : (
            <>
              <Button variant="ghost" onClick={closeCreateModal}>Cancel</Button>
              <Button onClick={() => createMutation.mutate()} loading={createMutation.isPending}>Create Key</Button>
            </>
          )}
        </ModalFooter>
      </Modal>

      {/* Limits Edit Modal */}
      {editLimits && (
        <Modal open onClose={() => setEditLimits(null)}>
          <ModalHeader onClose={() => setEditLimits(null)}>
            Limits — {editLimits.name || editLimits.key_prefix}
          </ModalHeader>
          <ModalBody>
            <div className="space-y-4">
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Current spend: <strong>${editLimits.total_cost_usd.toFixed(4)}</strong>
              </p>
              <Input
                label="Lifetime spending cap in USD (blank = unlimited)"
                type="number"
                min="0"
                step="0.01"
                value={capInput}
                onChange={e => setCapInput(e.target.value)}
                placeholder="e.g. 10.00"
              />
              <Input
                label="Rate limit (requests/minute, blank = unlimited)"
                type="number"
                min="1"
                step="1"
                value={rpmInput}
                onChange={e => setRpmInput(e.target.value)}
                placeholder="e.g. 60"
              />
              <p className="text-xs text-gray-400">
                Spending cap: requests are rejected with HTTP 429 once the key's lifetime cost reaches the limit.
                Rate limit: enforced per-node using a 60-second sliding window.
              </p>
            </div>
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setEditLimits(null)}>Cancel</Button>
            <Button onClick={saveLimits} loading={updateLimitsMutation.isPending}>Save Limits</Button>
          </ModalFooter>
        </Modal>
      )}

      {/* View Details Modal */}
      {viewDetails && (
        <Modal open onClose={() => setViewDetails(null)}>
          <ModalHeader onClose={() => setViewDetails(null)}>
            Key Details — {viewDetails.name || viewDetails.key_prefix}
          </ModalHeader>
          <ModalBody>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <dt className="text-gray-500">ID</dt>
              <dd className="font-mono text-gray-900 dark:text-gray-100">{viewDetails.id}</dd>
              <dt className="text-gray-500">Prefix</dt>
              <dd className="font-mono text-gray-900 dark:text-gray-100">{viewDetails.key_prefix}…</dd>
              <dt className="text-gray-500">Type</dt>
              <dd><Badge variant={viewDetails.key_type === 'claude-code' ? 'info' : 'default'}>{viewDetails.key_type}</Badge></dd>
              <dt className="text-gray-500">Enabled</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.enabled ? 'Yes' : 'No'}</dd>
              <dt className="text-gray-500">Created</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.created_at ? new Date(viewDetails.created_at).toLocaleString() : '—'}</dd>
              <dt className="text-gray-500">Last used</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.last_used_at ? new Date(viewDetails.last_used_at).toLocaleString() : '—'}</dd>
              <dt className="text-gray-500 border-t border-gray-100 dark:border-gray-700 pt-3">Total requests</dt>
              <dd className="text-gray-900 dark:text-gray-100 border-t border-gray-100 dark:border-gray-700 pt-3">{viewDetails.total_requests.toLocaleString()}</dd>
              <dt className="text-gray-500">Total tokens</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.total_tokens.toLocaleString()}</dd>
              <dt className="text-gray-500">Lifetime cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${viewDetails.total_cost_usd.toFixed(4)}</dd>
              <dt className="text-gray-500">Today's cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${((viewDetails as any).day_cost_usd ?? 0).toFixed(4)}</dd>
              <dt className="text-gray-500">This hour's cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${((viewDetails as any).hour_cost_usd ?? 0).toFixed(4)}</dd>
              <dt className="text-gray-500 border-t border-gray-100 dark:border-gray-700 pt-3">Lifetime cap</dt>
              <dd className="text-gray-900 dark:text-gray-100 border-t border-gray-100 dark:border-gray-700 pt-3">{viewDetails.spending_cap_usd != null ? `$${viewDetails.spending_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500">Daily hard cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).daily_hard_cap_usd != null ? `$${(viewDetails as any).daily_hard_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500">Daily soft cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).daily_soft_cap_usd != null ? `$${(viewDetails as any).daily_soft_cap_usd.toFixed(2)}` : '—'}</dd>
              <dt className="text-gray-500">Hourly cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).hourly_cap_usd != null ? `$${(viewDetails as any).hourly_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500">Rate limit</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.rate_limit_rpm != null ? `${viewDetails.rate_limit_rpm}/min` : '∞'}</dd>
              <dt className="text-gray-500">Semantic cache</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).semantic_cache_enabled ? 'Enabled' : 'Disabled'}</dd>
            </dl>
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setViewDetails(null)}>Close</Button>
            <Button onClick={() => { openLimitsEdit(viewDetails); setViewDetails(null) }}>
              <Pencil className="h-4 w-4 mr-1.5" />Edit limits
            </Button>
          </ModalFooter>
        </Modal>
      )}

      <ConfirmDialog
        open={!!deleteId}
        title="Delete API Key"
        message="This key will stop working immediately. Any apps using it will fail."
        confirmLabel="Delete"
        variant="danger"
        onConfirm={() => { deleteMutation.mutate(deleteId!); setDeleteId(null) }}
        onCancel={() => setDeleteId(null)}
      />

      <ConfirmDialog
        open={showBulkConfirm}
        title={`Delete ${selectedIds.size} API Key${selectedIds.size === 1 ? '' : 's'}?`}
        message={`${selectedIds.size} key${selectedIds.size === 1 ? '' : 's'} will stop working immediately. Any apps using them will fail. This cannot be undone.`}
        confirmLabel={`Delete ${selectedIds.size}`}
        variant="danger"
        onConfirm={() => bulkDeleteMutation.mutate(Array.from(selectedIds))}
        onCancel={() => setShowBulkConfirm(false)}
      />
    </div>
  )
}
