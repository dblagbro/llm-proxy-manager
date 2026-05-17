/**
 * AIRI change log + revert (v4.0 milestone 3).
 *
 * The audit trail of every change AIRI proposed — pending, applied, rejected
 * or reverted — with one-click revert on an applied change. Renders only when
 * the `airi_enabled` flag is on. Mobile-responsive.
 */
import { useState, useEffect, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type Proposal = {
  id: string
  kind: string
  target: string
  change: { field?: string; from?: unknown; to?: unknown; capped?: boolean }
  dry_run: { summary?: string }
  status: string
  created_by: string
  created_via_prompt: string
  created_at: string
  decided_at: string | null
  decided_by: string | null
}

async function airi(path: string, init?: RequestInit): Promise<any> {
  const res = await fetch(`${getBasePath()}${path}`, { credentials: 'include', ...init })
  const text = await res.text()
  let body: any = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  if (!res.ok) throw new Error((body && body.error) || (body && body.detail) || `HTTP ${res.status}`)
  return body
}

const STATUS_STYLE: Record<string, string> = {
  pending: 'text-blue-700 dark:text-blue-400',
  applied: 'text-green-700 dark:text-green-400',
  rejected: 'text-gray-500',
  reverted: 'text-gray-500',
}

export function AiriChangesPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [proposals, setProposals] = useState<Proposal[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')

  useEffect(() => {
    fetch(`${getBasePath()}/api/airi/status`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => setEnabled(!!d.enabled))
      .catch(() => setEnabled(false))
  }, [])

  const load = useCallback(async () => {
    try {
      const body = await airi('/api/airi/proposals')
      setProposals(body.proposals || [])
    } catch (e: any) {
      setMsg(`Could not load the change log: ${e.message}`)
    }
  }, [])

  useEffect(() => {
    if (enabled === true && expanded) load()
  }, [enabled, expanded, load])

  const revert = useCallback(
    async (id: string) => {
      setBusy(true)
      setMsg('')
      try {
        await airi(`/api/airi/proposals/${id}/revert`, { method: 'POST' })
        setMsg('Change reverted.')
        await load()
      } catch (e: any) {
        setMsg(e.message || 'Revert failed')
      } finally {
        setBusy(false)
      }
    },
    [load],
  )

  if (enabled !== true) return null

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            AIRI change log
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            every proposal AIRI made — with revert
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 p-4 space-y-3">
          <div className="flex items-center gap-2">
            <button
              onClick={load}
              disabled={busy}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1
                         text-xs text-gray-700 dark:text-gray-300
                         hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              Refresh
            </button>
            {msg && (
              <span className="text-xs text-gray-600 dark:text-gray-300">{msg}</span>
            )}
          </div>

          {proposals.length === 0 && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              No changes yet — AIRI has not proposed anything.
            </p>
          )}

          <div className="space-y-2">
            {proposals.map((p) => (
              <div
                key={p.id}
                className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-1"
              >
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                    {p.kind?.replace(/_/g, ' ')} · {p.target}
                  </span>
                  <span
                    className={`text-xs font-semibold ${STATUS_STYLE[p.status] || 'text-gray-500'}`}
                  >
                    {p.status}
                  </span>
                </div>
                <div className="text-xs font-mono text-gray-700 dark:text-gray-300">
                  {p.change?.field}: {String(p.change?.from)} → {String(p.change?.to)}
                  {p.change?.capped ? ' (capped)' : ''}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400">
                  proposed by {p.created_by} · {p.created_at}
                  {p.decided_by ? ` · decided by ${p.decided_by}` : ''}
                </div>
                {p.created_via_prompt && (
                  <div className="text-xs italic text-gray-500 dark:text-gray-400">
                    “{p.created_via_prompt}”
                  </div>
                )}
                {p.status === 'applied' && (
                  <button
                    onClick={() => revert(p.id)}
                    disabled={busy}
                    className="mt-1 rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1
                               text-xs text-gray-700 dark:text-gray-300
                               hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
                  >
                    Revert
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
