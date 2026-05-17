/**
 * AIRI automation panel (v4.0 milestone 4).
 *
 * The scheduled-rule registry + the automation kill switch. Lists every
 * conditional / monitor rule in the active rule-set, with enable/disable,
 * and a master toggle that pauses all scheduled-rule evaluation. Renders
 * only when the `airi_enabled` flag is on. Mobile-responsive.
 *
 * Scheduled rules run on a deterministic schedule with no LLM involved —
 * the LLM only authored each rule once, with operator approval.
 */
import { useState, useEffect, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type Rule = {
  id: string
  name: string
  kind: string
  spec: {
    cadence_min?: number
    condition?: { provider_name?: string; window_min?: number; op?: string; value?: number }
    action?: { type?: string; hours?: number }
  }
  mode: string
  enabled: boolean
  last_run_at: string | null
  last_action: string | null
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

function describe(r: Rule): string {
  const c = r.spec?.condition || {}
  const every = r.spec?.cadence_min ? `every ${r.spec.cadence_min} min` : 'on a schedule'
  const cond = `${c.provider_name ?? '?'} error rate ${c.op ?? '>'} ${c.value ?? '?'}% over ${c.window_min ?? '?'} min`
  if (r.kind === 'monitor') return `${every}: if ${cond} → notify`
  const a = r.spec?.action || {}
  const verb = a.type === 'auto_skip' ? `auto-skip ${a.hours ?? 1}h` : (a.type ?? 'act')
  return `${every}: if ${cond} → ${verb} (${r.mode})`
}

export function AiriAutomationPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [automationOn, setAutomationOn] = useState(false)
  const [rules, setRules] = useState<Rule[]>([])
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
      const [auto, active] = await Promise.all([
        airi('/api/airi/automation'),
        airi('/api/airi/active-ruleset'),
      ])
      setAutomationOn(!!auto.automation_enabled)
      setRules((active.rules || []).filter(
        (r: Rule) => r.kind === 'conditional' || r.kind === 'monitor',
      ))
    } catch (e: any) {
      setMsg(`Could not load automation: ${e.message}`)
    }
  }, [])

  useEffect(() => {
    if (enabled === true && expanded) load()
  }, [enabled, expanded, load])

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

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            Scheduled rules
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            AIRI automation — runs deterministically, no LLM
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 p-4 space-y-4">
          {/* Kill switch */}
          <div className="flex items-center justify-between gap-3 flex-wrap">
            <div>
              <div className="text-sm font-medium text-gray-900 dark:text-gray-100">
                Automation {automationOn ? 'ENABLED' : 'paused'}
              </div>
              <div className="text-xs text-gray-500 dark:text-gray-400">
                When paused, no scheduled rule is evaluated. Resets to off on restart.
              </div>
            </div>
            <button
              onClick={() => act(
                () => airi('/api/airi/automation', {
                  method: 'POST',
                  headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ enabled: !automationOn }),
                }),
                automationOn ? 'Automation paused.' : 'Automation enabled.',
              )}
              disabled={busy}
              className={
                'shrink-0 rounded-md px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50 ' +
                (automationOn ? 'bg-amber-600 hover:bg-amber-700' : 'bg-blue-600 hover:bg-blue-700')
              }
            >
              {automationOn ? 'Pause automation' : 'Enable automation'}
            </button>
          </div>

          {msg && (
            <div className="text-xs text-gray-600 dark:text-gray-300 bg-gray-50 dark:bg-gray-900 rounded px-3 py-2">
              {msg}
            </div>
          )}

          {/* Rule registry */}
          {rules.length === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              No scheduled rules yet — ask AIRI to add one (e.g. “add a rule that
              auto-skips a provider when its error rate exceeds 10%”).
            </p>
          ) : (
            <div className="space-y-2">
              {rules.map((r) => (
                <div
                  key={r.id}
                  className="rounded-lg border border-gray-200 dark:border-gray-700 p-3 space-y-1"
                >
                  <div className="flex items-center justify-between gap-2 flex-wrap">
                    <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
                      {r.name}
                      <span className="ml-2 text-xs font-normal text-gray-500">{r.kind}</span>
                    </span>
                    <button
                      onClick={() => act(
                        () => airi(`/api/airi/rules/${r.id}/toggle`, { method: 'POST' }),
                        `${r.name} ${r.enabled ? 'disabled' : 'enabled'}.`,
                      )}
                      disabled={busy}
                      className={
                        'shrink-0 rounded px-2 py-1 text-xs font-medium disabled:opacity-50 ' +
                        (r.enabled
                          ? 'bg-green-100 dark:bg-green-900/40 text-green-700 dark:text-green-400'
                          : 'bg-gray-100 dark:bg-gray-700 text-gray-500')
                      }
                    >
                      {r.enabled ? 'enabled — click to disable' : 'disabled — click to enable'}
                    </button>
                  </div>
                  <div className="text-xs text-gray-600 dark:text-gray-300">{describe(r)}</div>
                  {(r.last_run_at || r.last_action) && (
                    <div className="text-xs text-gray-500 dark:text-gray-400">
                      {r.last_run_at ? `last evaluated ${r.last_run_at}` : 'not yet evaluated'}
                      {r.last_action ? ` · ${r.last_action}` : ''}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
