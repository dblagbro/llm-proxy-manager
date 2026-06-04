import { useState, useMemo, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Pencil, Eye, ArrowUp, ArrowDown, ArrowUpDown, Key as KeyIcon, EyeOff, ClipboardList } from 'lucide-react'
import { keysApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { HelpHint } from '@/components/ui/HelpHint'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { CopyButton } from '@/components/ui/CopyButton'
import { Spinner } from '@/components/ui/Spinner'
import { useToast } from '@/components/ui/Toast'
import type { ApiKey, KeyType } from '@/types'
import { useAuth } from '@/context/AuthContext'
import { formatTimeForUser } from '@/utils/time'
import { ComplianceFieldsEditor } from '@/components/keys/ComplianceFieldsEditor'
import { ReasonPromptModal } from '@/components/keys/ReasonPromptModal'

const KEY_TYPES: KeyType[] = ['standard', 'claude-code', 'admin-readonly-catalog']

type SortKey = 'name' | 'key_type' | 'total_requests' | 'total_tokens' |
  'total_cost_usd' | 'spending_cap_usd' | 'rate_limit_rpm' | 'created_at'

function sortKeys(rows: ApiKey[], key: SortKey, dir: 'asc' | 'desc'): ApiKey[] {
  const copy = [...rows]
  const mult = dir === 'asc' ? 1 : -1
  copy.sort((a, b) => {
    const av: any = (a as any)[key]
    const bv: any = (b as any)[key]
    // Nulls always sort to the bottom regardless of direction
    if (av == null && bv == null) return 0
    if (av == null) return 1
    if (bv == null) return -1
    if (typeof av === 'number' && typeof bv === 'number') return mult * (av - bv)
    return mult * String(av).localeCompare(String(bv))
  })
  return copy
}

export function APIKeysPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const { user } = useAuth()
  const [showCreate, setShowCreate] = useState(false)
  const [newKey, setNewKey] = useState<{ raw: string; prefix: string } | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  // v5.0.11 — single unified edit modal merges limits + compliance.
  const [editKey, setEditKey] = useState<ApiKey | null>(null)
  const [viewDetails, setViewDetails] = useState<ApiKey | null>(null)
  const [capInput, setCapInput] = useState('')
  const [rpmInput, setRpmInput] = useState('')
  const [form, setForm] = useState({ name: '', key_type: 'standard' as KeyType, rate_limit_rpm: '' })
  // v5.0.0 compliance form state (parallel to `form` so we can wire
  // them into create + the new ComplianceFieldsEditor without mixing
  // value shapes).
  const [createBlockedCompanies, setCreateBlockedCompanies] = useState<string[]>([])
  const [createAllowedPaths, setCreateAllowedPaths] = useState<string[] | null>(null)
  const [createDebugEcho, setCreateDebugEcho] = useState(false)
  const [editBlockedCompanies, setEditBlockedCompanies] = useState<string[]>([])
  const [editAllowedPaths, setEditAllowedPaths] = useState<string[] | null>(null)
  const [editDebugEcho, setEditDebugEcho] = useState(false)
  const [reasonPrompt, setReasonPrompt] = useState<{
    summary: string
    payload: Partial<ApiKey>
  } | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [showBulkConfirm, setShowBulkConfirm] = useState(false)
  const [sortKey, setSortKey] = useState<SortKey>('created_at')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [revealedKeys, setRevealedKeys] = useState<Record<string, string>>({})
  const [modelsModalKey, setModelsModalKey] = useState<ApiKey | null>(null)

  const { data: keys, isLoading } = useQuery({ queryKey: ['apikeys'], queryFn: keysApi.list })

  // v3.10.8 — effective model catalog for the key whose "Copy models"
  // modal is open. Fetched only while the modal is open.
  const { data: keyModels, isLoading: modelsLoading } = useQuery({
    queryKey: ['apikey-models', modelsModalKey?.id],
    queryFn: () => keysApi.models(modelsModalKey!.id),
    enabled: !!modelsModalKey,
  })

  async function copyKeyModels(delimiter: ',' | '\n') {
    const list = keyModels?.models ?? []
    if (!list.length) return
    try {
      await navigator.clipboard.writeText(list.join(delimiter))
      toast.success(
        `Copied ${list.length} model${list.length !== 1 ? 's' : ''} ` +
        `(${delimiter === ',' ? 'CSV' : 'one per line'})`,
      )
    } catch {
      toast.error('Clipboard copy failed')
    }
  }

  const createMutation = useMutation({
    mutationFn: () => keysApi.create({
      name: form.name,
      key_type: form.key_type,
      rate_limit_rpm: form.rate_limit_rpm ? Number(form.rate_limit_rpm) : undefined,
      // v5.0.0 compliance fields. Empty arrays are sent as-is so the
      // server records "explicit no per-key block" vs "no opinion".
      blocked_companies: createBlockedCompanies.length > 0 ? createBlockedCompanies : null,
      allowed_paths: createAllowedPaths,
      debug_echo_enabled: createDebugEcho,
    }),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      // Keep Create modal open so the raw key displays inline;
      // user must click "Done" to dismiss.
      setNewKey({ raw: data.raw_key, prefix: data.key_prefix })
    },
    onError: (e: Error) => toast.error(e.message),
  })

  async function toggleReveal(id: string) {
    if (revealedKeys[id]) {
      setRevealedKeys(r => { const { [id]: _, ...rest } = r; return rest })
      return
    }
    try {
      const resp = await keysApi.reveal(id)
      setRevealedKeys(r => ({ ...r, [id]: resp.raw_key }))
    } catch (e: any) {
      toast.error(e.message || 'Could not reveal — key was created before encryption was added. Delete and create a new one.')
    }
  }

  // v5.0.8 — "Show all keys" toggle. Session-persisted in sessionStorage so
  // it survives page navigation but resets at logout. When ON, every key with
  // can_reveal=true auto-reveals on render; per-row hide still works (it
  // overrides for that row).
  function readShowAllPref(): boolean {
    try { return sessionStorage.getItem('apikeys.show_all') === '1' } catch { return false }
  }
  function writeShowAllPref(v: boolean): void {
    try { sessionStorage.setItem('apikeys.show_all', v ? '1' : '0') } catch { /* noop */ }
  }
  const [showAllKeys, setShowAllKeys] = useState<boolean>(readShowAllPref)

  // v5.0.8 — when the page mounts (or keys arrive) with showAllKeys=true
  // (persisted from a prior session), fire the bulk reveal once.
  useEffect(() => {
    if (!showAllKeys || !keys) return
    const toFetch = keys.filter((k: any) => k.can_reveal && !revealedKeys[k.id])
    if (toFetch.length === 0) return
    let cancelled = false
    ;(async () => {
      const results = await Promise.allSettled(toFetch.map((k: any) => keysApi.reveal(k.id)))
      if (cancelled) return
      const newRevealed: Record<string, string> = {}
      results.forEach((r, i) => {
        if (r.status === 'fulfilled') newRevealed[toFetch[i].id] = r.value.raw_key
      })
      if (Object.keys(newRevealed).length > 0) {
        setRevealedKeys(prev => ({ ...prev, ...newRevealed }))
      }
    })()
    return () => { cancelled = true }
    // Only depends on the key id set; ignore revealedKeys to avoid loops.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showAllKeys, (keys ?? []).map((k: any) => k.id).join(',')])

  async function toggleShowAll() {
    const next = !showAllKeys
    setShowAllKeys(next)
    writeShowAllPref(next)
    if (next && keys) {
      // Reveal everything that can be revealed and isn't already shown.
      const toFetch = keys.filter((k: any) => k.can_reveal && !revealedKeys[k.id])
      const results = await Promise.allSettled(toFetch.map((k: any) => keysApi.reveal(k.id)))
      const newRevealed: Record<string, string> = {}
      results.forEach((r, i) => {
        if (r.status === 'fulfilled') newRevealed[toFetch[i].id] = r.value.raw_key
      })
      if (Object.keys(newRevealed).length > 0) {
        setRevealedKeys(prev => ({ ...prev, ...newRevealed }))
      }
      const failures = results.filter(r => r.status === 'rejected').length
      if (failures > 0) {
        toast.error(`${failures} key${failures === 1 ? '' : 's'} could not be revealed (created before encryption was added)`)
      }
    } else if (!next) {
      // Collapse back — clear revealed state so rows return to prefix display.
      setRevealedKeys({})
    }
  }

  function closeCreateModal() {
    setShowCreate(false)
    setNewKey(null)
    setForm({ name: '', key_type: 'standard', rate_limit_rpm: '' })
    setCreateBlockedCompanies([])
    setCreateAllowedPaths(null)
    setCreateDebugEcho(false)
  }

  // v5.0.11 — one mutation handles both limits + compliance via a
  // partial-update payload. Reason-prompt still gates blocked_companies
  // and allowed_paths changes; limits and debug_echo go straight
  // through.
  const updateKeyMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: Partial<ApiKey> & { reason?: string } }) =>
      keysApi.update(id, payload as any),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['apikeys'] })
      toast.success('Key updated')
      setEditKey(null)
      setReasonPrompt(null)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function _arrayEqual(a: string[] | null | undefined, b: string[] | null | undefined): boolean {
    if (a == null && b == null) return true
    if (a == null || b == null) return false
    if (a.length !== b.length) return false
    return a.every((v, i) => v === b[i])
  }

  function saveEdit() {
    if (!editKey) return
    const original = editKey

    // Limits diff
    const capRaw = capInput.trim()
    const rpmRaw = rpmInput.trim()
    if (capRaw !== '' && (isNaN(Number(capRaw)) || Number(capRaw) < 0)) {
      toast.error('Spending cap must be a positive number or blank')
      return
    }
    if (rpmRaw !== '' && (isNaN(Number(rpmRaw)) || Number(rpmRaw) < 1 || !Number.isInteger(Number(rpmRaw)))) {
      toast.error('Rate limit must be a positive integer or blank')
      return
    }
    const newCap = capRaw === '' ? null : Number(capRaw)
    const newRpm = rpmRaw === '' ? null : Number(rpmRaw)
    const capChanged = (original.spending_cap_usd ?? null) !== newCap
    const rpmChanged = (original.rate_limit_rpm ?? null) !== newRpm

    // Compliance diff
    const nextBlocked = editBlockedCompanies.length > 0 ? editBlockedCompanies : null
    const blockedChanged = !_arrayEqual(original.blocked_companies ?? null, nextBlocked)
    const pathsChanged = !_arrayEqual(original.allowed_paths ?? null, editAllowedPaths)
    const echoChanged = Boolean(original.debug_echo_enabled) !== editDebugEcho

    if (!capChanged && !rpmChanged && !blockedChanged && !pathsChanged && !echoChanged) {
      setEditKey(null)
      return
    }

    const payload: Partial<ApiKey> & { spending_cap_usd?: number; rate_limit_rpm?: number } = {}
    // -1 is the API's sentinel for "clear to null" on limits fields.
    if (capChanged) (payload as any).spending_cap_usd = newCap === null ? -1 : newCap
    if (rpmChanged) (payload as any).rate_limit_rpm = newRpm === null ? -1 : newRpm
    if (blockedChanged) payload.blocked_companies = nextBlocked
    if (pathsChanged)   payload.allowed_paths    = editAllowedPaths
    if (echoChanged)    payload.debug_echo_enabled = editDebugEcho

    if (blockedChanged || pathsChanged) {
      // Decision 6: policy changes require a reason. Hand control to the
      // ReasonPromptModal; the actual PATCH fires from its onConfirm.
      const parts: string[] = []
      if (blockedChanged) parts.push('blocked_companies')
      if (pathsChanged)   parts.push('allowed_paths')
      setReasonPrompt({
        summary: `Changing ${parts.join(' + ')} on key ${original.name || original.key_prefix}.`,
        payload,
      })
      return
    }

    updateKeyMutation.mutate({ id: original.id, payload })
  }

  function confirmReasonAndSave(reason: string) {
    if (!editKey || !reasonPrompt) return
    updateKeyMutation.mutate({
      id: editKey.id,
      payload: { ...reasonPrompt.payload, reason } as any,
    })
  }

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

  const sortedKeys = useMemo(() => sortKeys(keys ?? [], sortKey, sortDir), [keys, sortKey, sortDir])

  function handleSort(col: SortKey) {
    if (col === sortKey) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortKey(col)
      // Default to desc for numeric/date columns, asc for strings
      setSortDir(['name', 'key_type'].includes(col) ? 'asc' : 'desc')
    }
  }

  function SortHeader({ col, label, align = 'left' }: { col: SortKey; label: string; align?: 'left' | 'right' }) {
    const active = sortKey === col
    const Icon = !active ? ArrowUpDown : sortDir === 'asc' ? ArrowUp : ArrowDown
    return (
      <button
        onClick={() => handleSort(col)}
        className={`flex items-center gap-1 text-xs font-semibold uppercase tracking-wide transition-colors
          ${active ? 'text-indigo-600 dark:text-indigo-400' : 'text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'}
          ${align === 'right' ? 'justify-end w-full' : ''}`}
        title={`Sort by ${label}`}
      >
        {align === 'right' ? (<><Icon className="h-3 w-3" />{label}</>) : (<>{label}<Icon className="h-3 w-3" /></>)}
      </button>
    )
  }

  // API base URL (for developer docs on this page)
  const apiBase = typeof window !== 'undefined'
    ? `${window.location.origin}${window.location.pathname.replace(/\/$/, '')}`.replace(/\/api-keys.*$/, '')
    : ''

  // v5.0.11 — unified open: seed limits + compliance state from the key.
  function openEdit(k: ApiKey) {
    setEditKey(k)
    setCapInput(k.spending_cap_usd != null ? String(k.spending_cap_usd) : '')
    setRpmInput(k.rate_limit_rpm != null ? String(k.rate_limit_rpm) : '')
    setEditBlockedCompanies(k.blocked_companies ?? [])
    setEditAllowedPaths(k.allowed_paths ?? null)
    setEditDebugEcho(Boolean(k.debug_echo_enabled))
  }

  function fmtDate(ts: string) {
    return formatTimeForUser(ts, user, 'date')
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
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
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
          {/* v5.0.8 — bulk reveal toggle (session-persisted) */}
          <Button
            size="sm"
            variant="ghost"
            onClick={toggleShowAll}
            title={showAllKeys
              ? 'Hide all key values (collapse back to prefixes)'
              : 'Reveal all key values that can be revealed (per-key 👁 still works)'}
          >
            {showAllKeys ? (
              <><EyeOff className="h-4 w-4 mr-1.5" />Hide keys</>
            ) : (
              <><Eye className="h-4 w-4 mr-1.5" />Show keys</>
            )}
          </Button>
          <Button size="sm" onClick={() => setShowCreate(true)}><Plus className="h-4 w-4 mr-1.5" />Create Key</Button>
        </div>
      </div>

      {/* API base URL + endpoints — visible to devs on the keys page */}
      <Card>
        <CardContent className="py-4">
          <div className="flex items-start gap-4">
            <div className="flex-1 min-w-0">
              <p className="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mb-1">API Base URL</p>
              <div className="flex items-center gap-2">
                <code className="text-sm font-mono text-gray-900 dark:text-gray-100 break-all">{apiBase}</code>
                <CopyButton text={apiBase} />
              </div>
              <div className="mt-3 grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-1 text-xs text-gray-600 dark:text-gray-400 font-mono">
                <div>POST <span className="text-indigo-500">{apiBase}/v1/messages</span> <span className="text-gray-400">(Anthropic format)</span></div>
                <div>POST <span className="text-indigo-500">{apiBase}/v1/chat/completions</span> <span className="text-gray-400">(OpenAI format)</span></div>
                <div>GET  <span className="text-indigo-500">{apiBase}/v1/models</span></div>
                <div>GET  <span className="text-indigo-500">{apiBase}/metrics</span> <span className="text-gray-400">(Prometheus)</span></div>
              </div>
              <p className="mt-2 text-xs text-gray-500 dark:text-gray-400">
                Auth: pass the raw key via <code className="text-indigo-500">x-api-key: &lt;key&gt;</code> header (Anthropic) or <code className="text-indigo-500">Authorization: Bearer &lt;key&gt;</code> (OpenAI).
              </p>
            </div>
          </div>
        </CardContent>
      </Card>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : (keys?.length ?? 0) === 0 ? (
        <Card><CardContent><p className="text-center text-gray-500 dark:text-gray-400 py-10">No API keys yet</p></CardContent></Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            {/* Column header with sort + select-all */}
            <div className="flex items-center gap-4 px-5 py-3 bg-gray-50 dark:bg-gray-800/50 border-b border-gray-100 dark:border-gray-700">
              <input
                type="checkbox"
                checked={allSelected}
                ref={el => { if (el) el.indeterminate = selectedIds.size > 0 && !allSelected }}
                onChange={toggleSelectAll}
                className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                aria-label="Select all API keys"
              />
              <div className="flex-1 min-w-0 flex items-center gap-3">
                <SortHeader col="name" label="Name" />
                <span className="text-gray-300 dark:text-gray-600">·</span>
                <SortHeader col="key_type" label="Type" />
              </div>
              <div className="hidden md:grid grid-cols-5 gap-5 text-right">
                <SortHeader col="total_requests" label="Requests" align="right" />
                <SortHeader col="total_tokens" label="Tokens" align="right" />
                <SortHeader col="total_cost_usd" label="Cost" align="right" />
                <SortHeader col="spending_cap_usd" label="Cap" align="right" />
                <SortHeader col="rate_limit_rpm" label="Rate" align="right" />
              </div>
              <div className="hidden lg:block shrink-0 w-20">
                <SortHeader col="created_at" label="Created" />
              </div>
              <span className="w-[72px] shrink-0" aria-hidden />
            </div>
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {sortedKeys.map(k => (
                <div key={k.id} className={`flex items-center gap-4 px-5 py-4 ${selectedIds.has(k.id) ? 'bg-indigo-50/50 dark:bg-indigo-900/10' : ''}`}>
                  <input
                    type="checkbox"
                    checked={selectedIds.has(k.id)}
                    onChange={() => toggleSelect(k.id)}
                    className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
                    aria-label={`Select ${k.name || k.key_prefix}`}
                  />
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-0.5 flex-wrap">
                      <p className="font-medium text-gray-900 dark:text-gray-100">{k.name || '(unnamed)'}</p>
                      <Badge variant={k.key_type === 'claude-code' ? 'info' : 'default'}>{k.key_type}</Badge>
                      {(k.blocked_companies?.length ?? 0) > 0 && (
                        <span title={`Per-key blocked: ${(k.blocked_companies ?? []).join(', ')}`}>
                          <Badge variant="warning" className="font-mono">
                            Blocks: {(k.blocked_companies ?? []).slice(0, 2).join(',')}
                            {(k.blocked_companies?.length ?? 0) > 2 && '…'}
                          </Badge>
                        </span>
                      )}
                      {k.allowed_paths != null && (
                        <span title={k.allowed_paths.length > 0
                          ? `Allowed paths: ${k.allowed_paths.join(', ')}`
                          : 'Path whitelist is empty — every endpoint is denied.'}>
                          <Badge variant="info">Restricted paths</Badge>
                        </span>
                      )}
                      {k.debug_echo_enabled && (
                        <span title="The sandbox /api/debug/echo-client endpoint is enabled for this key.">
                          <Badge variant="muted">Debug echo</Badge>
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-1.5">
                      {revealedKeys[k.id] ? (
                        <>
                          <code className="text-xs text-gray-800 dark:text-gray-200 font-mono break-all select-all">{revealedKeys[k.id]}</code>
                          <CopyButton text={revealedKeys[k.id]} />
                          <button
                            onClick={() => toggleReveal(k.id)}
                            className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                            title="Hide key"
                          >
                            <EyeOff className="h-3.5 w-3.5" />
                          </button>
                        </>
                      ) : (
                        <>
                          <p className="text-xs text-gray-500 dark:text-gray-400 font-mono">{k.key_prefix}…</p>
                          {(k as any).can_reveal ? (
                            <button
                              onClick={() => toggleReveal(k.id)}
                              className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                              title="Reveal full key (admin-only; obscured by default)"
                            >
                              <Eye className="h-3.5 w-3.5" />
                            </button>
                          ) : (
                            <span
                              className="text-gray-300 dark:text-gray-600 shrink-0 cursor-help"
                              title="This key was created before Fernet encryption was added — its raw value is no longer recoverable. Delete and create a new one if you need to retrieve it."
                            >
                              <KeyIcon className="h-3 w-3" />
                            </span>
                          )}
                        </>
                      )}
                    </div>
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
                    onClick={() => openEdit(k)}
                    className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                    title="Edit limits + compliance policy"
                  >
                    <Pencil className="h-4 w-4" />
                  </button>
                  <button
                    onClick={() => setModelsModalKey(k)}
                    className="text-gray-400 hover:text-indigo-500 transition-colors shrink-0"
                    title="Copy this key's model list"
                  >
                    <ClipboardList className="h-4 w-4" />
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
              <p className="text-sm text-emerald-700 dark:text-emerald-400 font-medium">
                ✓ Key created. You can copy it now or come back later — every key has a 👁 reveal button on its row.
              </p>
              <div className="flex items-center gap-2 bg-gray-100 dark:bg-gray-800 rounded-lg px-4 py-3">
                <code className="flex-1 text-sm font-mono text-gray-900 dark:text-gray-100 break-all select-all">{newKey.raw}</code>
                <CopyButton text={newKey.raw} />
              </div>
              <p className="text-xs text-gray-500 dark:text-gray-400">
                Prefix: <code className="font-mono">{newKey.prefix}…</code> (used to identify the key in the UI and logs). Keys are stored Fernet-encrypted at rest; the reveal button decrypts on demand for admins.
              </p>
            </div>
          ) : (
            <div className="space-y-4">
              <Input
                label="Key Name (optional)"
                tooltip="Human-friendly label that appears in the activity log + dashboards. Pick something that identifies the calling app or environment (‘production-paperless’, ‘devingpt-staging’). The key prefix is what callers actually authenticate with — the name is for your own bookkeeping."
                value={form.name}
                onChange={e => setForm(f => ({ ...f, name: e.target.value }))}
                placeholder="e.g. production-app"
              />
              <div className="flex flex-col gap-1.5">
                <label className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-1">
                  <span>Key Type</span>
                  <HelpHint text="Determines what kinds of providers this key can route to AND whether CoT-E is auto-engaged. ‘standard’ routes anywhere. ‘claude-code’ auto-engages CoT-E on non-reasoning models. ‘claude-pro-max’ pins to subscription-tier providers (no per-call billing). Only the operator's own admin key uses ‘admin’. ‘admin-readonly-catalog’ is a narrow-scope key (v3.7.2) that can edit /api/llm/models/{id} per-model aliases/family/variant — paste it into the coordinator-hub Proxy Catalog Admin Key setting. It CANNOT make inference calls (rejected by /v1/messages + /v1/chat/completions). Despite the name it allows PUT writes; ‘read-only’ in the name refers to ‘no inference’, not ‘no edits’." />
                </label>
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
                {form.key_type === 'admin-readonly-catalog' && (
                  <p className="text-xs text-indigo-600 dark:text-indigo-400">
                    Catalog-scope key for the coordinator-hub Scan Models picker.
                    Can edit per-model aliases/family/variant via PUT /api/llm/models/{'{id}'}.
                    Cannot make inference calls.
                  </p>
                )}
              </div>
              <Input
                label="Rate Limit (requests/minute, blank = unlimited)"
                tooltip="Per-key cap, sliding 60s window, enforced per-node (so a 4-node cluster lets ~4× this rate through). Caller gets HTTP 429 + Retry-After when the window fills. Blank disables — use sparingly; an unlimited key can DoS the upstream subscription quota in minutes if a caller misbehaves."
                type="number"
                value={form.rate_limit_rpm}
                onChange={e => setForm(f => ({ ...f, rate_limit_rpm: e.target.value }))}
              />
              <ComplianceFieldsEditor
                blockedCompanies={createBlockedCompanies}
                setBlockedCompanies={setCreateBlockedCompanies}
                allowedPaths={createAllowedPaths}
                setAllowedPaths={setCreateAllowedPaths}
                debugEchoEnabled={createDebugEcho}
                setDebugEchoEnabled={setCreateDebugEcho}
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

      {/* v5.0.11 — unified Edit Key modal (limits + compliance) */}
      {editKey && (
        <Modal open onClose={() => setEditKey(null)} size="lg">
          <ModalHeader onClose={() => setEditKey(null)}>
            Edit — {editKey.name || editKey.key_prefix}
          </ModalHeader>
          <ModalBody>
            <div className="space-y-4">
              <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">
                Limits
              </p>
              <p className="text-sm text-gray-600 dark:text-gray-400">
                Current spend: <strong>${editKey.total_cost_usd.toFixed(4)}</strong>
              </p>
              <Input
                label="Lifetime spending cap in USD (blank = unlimited)"
                tooltip="Hard ceiling on this key's cumulative paid-provider cost since creation. Subscription routes (claude-oauth / codex-oauth / grok-web) cost $0 and don't count against this. Once hit, requests get HTTP 429 with the cap-exceeded message until you raise the cap or rotate the key."
                type="number"
                min="0"
                step="0.01"
                value={capInput}
                onChange={e => setCapInput(e.target.value)}
                placeholder="e.g. 10.00"
              />
              <Input
                label="Rate limit (requests/minute, blank = unlimited)"
                tooltip="Per-key cap, sliding 60s window, enforced per-node. A 4-node cluster lets ~4× this rate through in aggregate. Caller gets HTTP 429 + Retry-After when the window fills. Blank disables — risky for keys with auto-restarting workflows."
                type="number"
                min="1"
                step="1"
                value={rpmInput}
                onChange={e => setRpmInput(e.target.value)}
                placeholder="e.g. 60"
              />
            </div>
            <ComplianceFieldsEditor
              blockedCompanies={editBlockedCompanies}
              setBlockedCompanies={setEditBlockedCompanies}
              allowedPaths={editAllowedPaths}
              setAllowedPaths={setEditAllowedPaths}
              debugEchoEnabled={editDebugEcho}
              setDebugEchoEnabled={setEditDebugEcho}
            />
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setEditKey(null)}>Cancel</Button>
            <Button onClick={saveEdit} loading={updateKeyMutation.isPending}>Save</Button>
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
            {!(viewDetails as any).can_reveal && (
              <p className="text-xs text-amber-600 dark:text-amber-400 mb-4 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-900/30 rounded-md px-3 py-2">
                This key was created before encryption-at-rest support, so its raw value cannot be retrieved. If it's lost, delete and recreate.
              </p>
            )}
            <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm">
              <dt className="text-gray-500 dark:text-gray-400">ID</dt>
              <dd className="font-mono text-gray-900 dark:text-gray-100">{viewDetails.id}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Prefix</dt>
              <dd className="font-mono text-gray-900 dark:text-gray-100">{viewDetails.key_prefix}…</dd>
              <dt className="text-gray-500 dark:text-gray-400">Type</dt>
              <dd><Badge variant={viewDetails.key_type === 'claude-code' ? 'info' : 'default'}>{viewDetails.key_type}</Badge></dd>
              <dt className="text-gray-500 dark:text-gray-400">Enabled</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.enabled ? 'Yes' : 'No'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Created</dt>
              <dd className="text-gray-900 dark:text-gray-100">{formatTimeForUser(viewDetails.created_at, user)}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Last used</dt>
              <dd className="text-gray-900 dark:text-gray-100">{formatTimeForUser(viewDetails.last_used_at, user)}</dd>
              <dt className="text-gray-500 dark:text-gray-400 border-t border-gray-100 dark:border-gray-700 pt-3">Total requests</dt>
              <dd className="text-gray-900 dark:text-gray-100 border-t border-gray-100 dark:border-gray-700 pt-3">{viewDetails.total_requests.toLocaleString()}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Total tokens</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.total_tokens.toLocaleString()}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Lifetime cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${viewDetails.total_cost_usd.toFixed(4)}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Today's cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${((viewDetails as any).day_cost_usd ?? 0).toFixed(4)}</dd>
              <dt className="text-gray-500 dark:text-gray-400">This hour's cost</dt>
              <dd className="text-gray-900 dark:text-gray-100">${((viewDetails as any).hour_cost_usd ?? 0).toFixed(4)}</dd>
              <dt className="text-gray-500 dark:text-gray-400 border-t border-gray-100 dark:border-gray-700 pt-3">Lifetime cap</dt>
              <dd className="text-gray-900 dark:text-gray-100 border-t border-gray-100 dark:border-gray-700 pt-3">{viewDetails.spending_cap_usd != null ? `$${viewDetails.spending_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Daily hard cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).daily_hard_cap_usd != null ? `$${(viewDetails as any).daily_hard_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Daily soft cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).daily_soft_cap_usd != null ? `$${(viewDetails as any).daily_soft_cap_usd.toFixed(2)}` : '—'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Hourly cap</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).hourly_cap_usd != null ? `$${(viewDetails as any).hourly_cap_usd.toFixed(2)}` : '∞'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Rate limit</dt>
              <dd className="text-gray-900 dark:text-gray-100">{viewDetails.rate_limit_rpm != null ? `${viewDetails.rate_limit_rpm}/min` : '∞'}</dd>
              <dt className="text-gray-500 dark:text-gray-400">Semantic cache</dt>
              <dd className="text-gray-900 dark:text-gray-100">{(viewDetails as any).semantic_cache_enabled ? 'Enabled' : 'Disabled'}</dd>
            </dl>
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setViewDetails(null)}>Close</Button>
            <Button onClick={() => { openEdit(viewDetails); setViewDetails(null) }}>
              <Pencil className="h-4 w-4 mr-1.5" />Edit
            </Button>
          </ModalFooter>
        </Modal>
      )}

      {/* Copy-models Modal */}
      {modelsModalKey && (
        <Modal open onClose={() => setModelsModalKey(null)}>
          <ModalHeader onClose={() => setModelsModalKey(null)}>
            Models for {modelsModalKey.name || modelsModalKey.key_prefix}
          </ModalHeader>
          <ModalBody>
            {modelsLoading ? (
              <div className="flex justify-center py-8"><Spinner /></div>
            ) : !keyModels || keyModels.models.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400 py-4">
                No models available to this key — it has no enabled provider
                it's allowed to route to.
              </p>
            ) : (
              <>
                <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                  {keyModels.count} model{keyModels.count !== 1 ? 's' : ''} this
                  key can route to, across every provider it's allowed to use.
                </p>
                <pre className="max-h-72 overflow-auto rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 text-xs font-mono text-gray-700 dark:text-gray-300 whitespace-pre">
                  {keyModels.models.join('\n')}
                </pre>
              </>
            )}
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setModelsModalKey(null)}>Close</Button>
            <Button
              variant="outline"
              disabled={!keyModels?.models.length}
              onClick={() => copyKeyModels(',')}
            >
              Copy as CSV
            </Button>
            <Button
              disabled={!keyModels?.models.length}
              onClick={() => copyKeyModels('\n')}
            >
              Copy one per line
            </Button>
          </ModalFooter>
        </Modal>
      )}

      {/* Reason prompt for policy changes (decision 6) */}
      <ReasonPromptModal
        open={!!reasonPrompt}
        summary={reasonPrompt?.summary ?? ''}
        loading={updateKeyMutation.isPending}
        onCancel={() => setReasonPrompt(null)}
        onConfirm={confirmReasonAndSave}
      />

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
