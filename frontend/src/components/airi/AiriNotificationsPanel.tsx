/**
 * AIRI notification preferences panel (v4.0.3).
 *
 * Each operator subscribes their own email to AIRI notifications and tunes
 * which categories (monitor / automation) and what minimum severity reach
 * them. The shared alert mailbox always receives notifications regardless —
 * this panel is the personal, additive subscription. Renders only when the
 * `airi_enabled` flag is on. Mobile-responsive.
 */
import { useState, useEffect, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type Pref = {
  configured: boolean
  email: string | null
  enabled: boolean
  categories: { monitor: boolean; automation: boolean }
  min_severity: string
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

const SEVERITIES = ['info', 'warning', 'critical']

export function AiriNotificationsPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(false)
  const [pref, setPref] = useState<Pref | null>(null)
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
      setPref(await airi('/api/airi/notification-prefs'))
      setMsg('')
    } catch (e: any) {
      setMsg(`Could not load preferences: ${e.message}`)
    }
  }, [])

  useEffect(() => {
    if (enabled === true && expanded && pref === null) load()
  }, [enabled, expanded, pref, load])

  const patch = (p: Partial<Pref>) => setPref((cur) => (cur ? { ...cur, ...p } : cur))

  const save = useCallback(async () => {
    if (!pref) return
    setBusy(true)
    setMsg('')
    try {
      const saved = await airi('/api/airi/notification-prefs', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: pref.email, enabled: pref.enabled,
          categories: pref.categories, min_severity: pref.min_severity,
        }),
      })
      setPref(saved)
      setMsg('Saved.')
    } catch (e: any) {
      setMsg(e.message || 'Save failed')
    } finally {
      setBusy(false)
    }
  }, [pref])

  if (enabled !== true) return null

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            My notifications
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            email me when AIRI rules fire — per-user
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700 p-4 space-y-4">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            The shared alert mailbox always receives AIRI notifications. This is your
            own additional subscription — set your email to also get them personally.
          </p>

          {pref === null ? (
            <p className="text-sm text-gray-500 dark:text-gray-400">Loading…</p>
          ) : (
            <>
              {/* email */}
              <div className="space-y-1">
                <label className="text-sm font-medium text-gray-800 dark:text-gray-100">
                  Your email
                </label>
                <input
                  type="email"
                  value={pref.email || ''}
                  disabled={busy}
                  onChange={(e) => patch({ email: e.target.value })}
                  placeholder="you@example.com"
                  className="w-full rounded-md border border-gray-300 dark:border-gray-600
                             bg-white dark:bg-gray-900 px-3 py-2 text-sm
                             text-gray-900 dark:text-gray-100"
                />
                {!pref.email && (
                  <p className="text-xs text-gray-400">
                    No personal email set — you currently get no personal AIRI emails.
                  </p>
                )}
              </div>

              {/* enabled */}
              <label className="flex items-center gap-2 text-sm text-gray-800 dark:text-gray-100">
                <input
                  type="checkbox"
                  checked={pref.enabled}
                  disabled={busy}
                  onChange={(e) => patch({ enabled: e.target.checked })}
                  className="h-4 w-4"
                />
                Subscription enabled
              </label>

              {/* categories */}
              <div className="space-y-1.5">
                <div className="text-sm font-medium text-gray-800 dark:text-gray-100">
                  Categories
                </div>
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={pref.categories.monitor}
                    disabled={busy}
                    onChange={(e) =>
                      patch({ categories: { ...pref.categories, monitor: e.target.checked } })
                    }
                    className="h-4 w-4"
                  />
                  Monitor alerts — a monitor rule fired
                </label>
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={pref.categories.automation}
                    disabled={busy}
                    onChange={(e) =>
                      patch({ categories: { ...pref.categories, automation: e.target.checked } })
                    }
                    className="h-4 w-4"
                  />
                  Automation actions — a scheduled rule acted or tripped a breaker
                </label>
              </div>

              {/* min severity */}
              <div className="flex items-center gap-2 flex-wrap">
                <label className="text-sm font-medium text-gray-800 dark:text-gray-100">
                  Minimum severity
                </label>
                <select
                  value={pref.min_severity}
                  disabled={busy}
                  onChange={(e) => patch({ min_severity: e.target.value })}
                  className="rounded-md border border-gray-300 dark:border-gray-600
                             bg-white dark:bg-gray-900 px-2 py-1.5 text-sm
                             text-gray-700 dark:text-gray-300"
                >
                  {SEVERITIES.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </div>

              <div className="flex items-center gap-3 flex-wrap">
                <button
                  onClick={save}
                  disabled={busy}
                  className="rounded-md bg-blue-600 px-4 py-1.5 text-sm font-medium text-white
                             hover:bg-blue-700 disabled:opacity-50"
                >
                  Save
                </button>
                {msg && (
                  <span className="text-xs text-gray-600 dark:text-gray-300">{msg}</span>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  )
}
