/**
 * AIRI rule-set manager (v4.0 milestone 2).
 *
 * Shows the active rule-set and its rules, lets the operator edit threshold
 * values, save the current set under a new name, restore a saved set, or
 * restore the Default. Renders only when the `airi_enabled` flag is on.
 *
 * Milestone 2: rules are stored config — editing them here does not yet
 * change live supervisor behaviour (that lands in a later milestone).
 */
import { useState, useEffect, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type Rule = {
  id: string
  name: string
  kind: string
  spec: { setting?: string; value?: number }
  mode: string
  enabled: boolean
}
type RulesetSummary = { id: string; name: string; is_default: boolean; is_active: boolean }
type ActiveRuleset = { id: string; name: string; is_default?: boolean; rules: Rule[] }

async function airi(path: string, init?: RequestInit): Promise<any> {
  const res = await fetch(`${getBasePath()}${path}`, { credentials: 'include', ...init })
  const text = await res.text()
  let body: any = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = text
  }
  if (!res.ok) {
    throw new Error((body && body.detail) || (body && body.error) || `HTTP ${res.status}`)
  }
  return body
}

export function AiriRulesPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [active, setActive] = useState<ActiveRuleset | null>(null)
  const [rulesets, setRulesets] = useState<RulesetSummary[]>([])
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [edits, setEdits] = useState<Record<string, string>>({})

  useEffect(() => {
    fetch(`${getBasePath()}/api/airi/status`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => setEnabled(!!d.enabled))
      .catch(() => setEnabled(false))
  }, [])

  const load = useCallback(async () => {
    try {
      const [a, list] = await Promise.all([
        airi('/api/airi/active-ruleset'),
        airi('/api/airi/rulesets'),
      ])
      setActive(a)
      setRulesets(list.rulesets || [])
      setEdits({})
    } catch (e: any) {
      setMsg(`Could not load rule-sets: ${e.message}`)
    }
  }, [])

  useEffect(() => {
    if (enabled === true) load()
  }, [enabled, load])

  const act = useCallback(
    async (fn: () => Promise<any>, ok: string) => {
      setBusy(true)
      setMsg('')
      try {
        await fn()
        setMsg(ok)
        await load()
      } catch (e: any) {
        setMsg(e.message || 'Action failed')
      } finally {
        setBusy(false)
      }
    },
    [load],
  )

  if (enabled !== true) return null

  const saveAs = () => {
    const name = window.prompt('Save the current rule-set as:')?.trim()
    if (!name) return
    act(
      () => airi('/api/airi/rulesets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      }),
      `Saved rule-set "${name}".`,
    )
  }

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Rule-sets
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {active ? `active: ${active.name}` : 'AIRI supervisor rules'}
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 p-4 space-y-4">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Rule-sets are stored configuration. Editing a rule here does not yet change
            live supervisor behaviour — that arrives in a later milestone.
          </p>

          {/* Rule-set controls */}
          <div className="flex flex-wrap items-center gap-2">
            <button
              onClick={saveAs}
              disabled={busy}
              className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white
                         hover:bg-blue-700 disabled:opacity-50"
            >
              Save as…
            </button>
            <button
              onClick={() => act(
                () => airi('/api/airi/rulesets/restore-default', { method: 'POST' }),
                'Restored the Default rule-set.',
              )}
              disabled={busy}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1.5
                         text-sm text-gray-700 dark:text-gray-300
                         hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              Restore Default
            </button>
            <select
              value=""
              disabled={busy}
              onChange={(e) => {
                const id = e.target.value
                if (!id) return
                const rs = rulesets.find((r) => r.id === id)
                act(
                  () => airi(`/api/airi/rulesets/${id}/activate`, { method: 'POST' }),
                  `Activated rule-set "${rs?.name ?? id}".`,
                )
              }}
              className="rounded-md border border-gray-300 dark:border-gray-600
                         bg-white dark:bg-gray-900 px-2 py-1.5 text-sm
                         text-gray-700 dark:text-gray-300 disabled:opacity-50"
            >
              <option value="">Restore a saved set…</option>
              {rulesets.map((rs) => (
                <option key={rs.id} value={rs.id}>
                  {rs.name}
                  {rs.is_active ? ' (active)' : ''}
                </option>
              ))}
            </select>
          </div>

          {msg && (
            <div className="text-xs text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-900
                            rounded px-3 py-2">
              {msg}
            </div>
          )}

          {/* Rules of the active set */}
          {active && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-left text-gray-500 dark:text-gray-400 border-b
                                 border-gray-200 dark:border-gray-700">
                    <th className="py-1.5 pr-3 font-medium">Rule</th>
                    <th className="py-1.5 pr-3 font-medium">Value</th>
                    <th className="py-1.5 font-medium"></th>
                  </tr>
                </thead>
                <tbody>
                  {active.rules.map((r) => {
                    const current = String(r.spec?.value ?? '')
                    const draft = edits[r.id] ?? current
                    const dirty = draft !== current
                    return (
                      <tr key={r.id} className="border-b border-gray-100 dark:border-gray-700/50">
                        <td className="py-2 pr-3 text-gray-900 dark:text-gray-100">{r.name}</td>
                        <td className="py-2 pr-3">
                          {r.kind === 'threshold' ? (
                            <input
                              type="number"
                              value={draft}
                              disabled={busy}
                              onChange={(e) =>
                                setEdits((m) => ({ ...m, [r.id]: e.target.value }))
                              }
                              className="w-24 rounded border border-gray-300 dark:border-gray-600
                                         bg-white dark:bg-gray-900 px-2 py-1 text-sm
                                         text-gray-900 dark:text-gray-100"
                            />
                          ) : (
                            <span className="text-gray-500">{current}</span>
                          )}
                        </td>
                        <td className="py-2">
                          {r.kind === 'threshold' && dirty && (
                            <button
                              onClick={() =>
                                act(
                                  () => airi(`/api/airi/rules/${r.id}`, {
                                    method: 'PATCH',
                                    headers: { 'Content-Type': 'application/json' },
                                    body: JSON.stringify({ value: Number(draft) }),
                                  }),
                                  `Updated "${r.name}".`,
                                )
                              }
                              disabled={busy}
                              className="rounded bg-blue-600 px-2 py-1 text-xs font-medium
                                         text-white hover:bg-blue-700 disabled:opacity-50"
                            >
                              Save
                            </button>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
