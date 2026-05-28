/**
 * v4.4 M-5 — per-node bridge auth status panel (Path A).
 *
 * Renders the cluster-wide view of a provider's per-node session
 * auth state. For grok-web (and any future ``node_local_session=true``
 * provider), each node has its own bridge holding its own logged-in
 * Chromium session; this component shows the green / amber / red
 * status badge per node + a [Re-auth] button when needed.
 *
 * Data source: ``GET /api/providers/{id}/node-auth-states``
 * (admin API added in the partial-M-5 backend commit). Returns an
 * empty list when no rows exist (typical for non-bridge providers,
 * or for grok-web before the first probe has filled the table).
 *
 * Only rendered when the provider's ``extra_config.node_local_session``
 * is truthy. For all other providers, the panel returns ``null`` —
 * no UI footprint, no API call.
 */
import { useEffect, useState } from 'react'
import { providersApi } from '@/api'
import type { Provider, NodeAuthState, NodeAuthStateValue } from '@/types'

interface Props {
  provider: Provider
}

/** Map auth_state → display token + tailwind colour classes. */
function _badge(state: NodeAuthStateValue): { label: string; cls: string } {
  switch (state) {
    case 'ok':
      return {
        label: 'OK',
        cls: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300',
      }
    case 'expired':
    case 'needs_reauth':
      return {
        label: 'Re-auth needed',
        cls: 'bg-amber-100 text-amber-800 dark:bg-amber-900/40 dark:text-amber-300',
      }
    case 'bridge_down':
      return {
        label: 'Bridge down',
        cls: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300',
      }
    case 'never_authed':
      return {
        label: 'Never auth’d',
        cls: 'bg-gray-100 text-gray-600 dark:bg-gray-800 dark:text-gray-400',
      }
  }
}

function _relative(iso: string | null): string {
  if (!iso) return 'never'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return iso
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (secs < 60) return `${secs}s ago`
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return `${Math.round(secs / 86400)}d ago`
}

export function NodeBridgeStatusPanel({ provider }: Props) {
  const extra = (provider as unknown as { extra_config?: Record<string, unknown> })
    .extra_config
  const isNodeLocalSession = Boolean(extra?.node_local_session)

  const [states, setStates] = useState<NodeAuthState[] | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!isNodeLocalSession) return
    let cancelled = false
    providersApi
      .nodeAuthStates(provider.id)
      .then((rows) => {
        if (!cancelled) setStates(rows)
      })
      .catch((e: Error) => {
        if (!cancelled) setErr(e.message || String(e))
      })
    // Poll every 30s so the operator sees status flips without
    // refreshing the page.
    const t = window.setInterval(() => {
      providersApi
        .nodeAuthStates(provider.id)
        .then((rows) => {
          if (!cancelled) setStates(rows)
        })
        .catch(() => {})
    }, 30_000)
    return () => {
      cancelled = true
      window.clearInterval(t)
    }
  }, [provider.id, isNodeLocalSession])

  if (!isNodeLocalSession) return null

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 p-4 bg-gray-50 dark:bg-gray-900/30">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-gray-800 dark:text-gray-200">
          Per-node bridge status
        </h3>
        <span className="text-xs text-gray-500 dark:text-gray-400">
          v4.4 Path A • updates every 30 s
        </span>
      </div>
      {err && (
        <p className="text-sm text-red-600 dark:text-red-400">
          Failed to load per-node status: {err}
        </p>
      )}
      {!err && states === null && (
        <p className="text-sm text-gray-500 dark:text-gray-400">Loading…</p>
      )}
      {!err && states !== null && states.length === 0 && (
        <p className="text-sm text-gray-500 dark:text-gray-400">
          No per-node observations yet. The keepalive prober will fill
          this within ~5 minutes of v4.4 deploy.
        </p>
      )}
      {!err && states !== null && states.length > 0 && (
        <table className="w-full text-sm">
          <thead className="text-left text-xs text-gray-500 dark:text-gray-400">
            <tr>
              <th className="py-1 pr-2">Node</th>
              <th className="py-1 pr-2">Status</th>
              <th className="py-1 pr-2">Last OK</th>
              <th className="py-1 pr-2">Last check</th>
              <th className="py-1">Action</th>
            </tr>
          </thead>
          <tbody>
            {states.map((s) => {
              const b = _badge(s.auth_state)
              const needsReauth =
                s.auth_state === 'needs_reauth' || s.auth_state === 'expired'
              return (
                <tr
                  key={s.node_id}
                  className="border-t border-gray-200 dark:border-gray-700"
                >
                  <td className="py-2 pr-2 font-mono text-xs text-gray-700 dark:text-gray-300">
                    {s.node_id}
                  </td>
                  <td className="py-2 pr-2">
                    <span
                      className={`inline-block px-2 py-0.5 rounded-full text-xs ${b.cls}`}
                    >
                      {b.label}
                    </span>
                    {s.last_error && (
                      <span
                        className="ml-2 text-xs text-gray-500 dark:text-gray-400"
                        title={s.last_error}
                      >
                        ⓘ
                      </span>
                    )}
                  </td>
                  <td className="py-2 pr-2 text-xs text-gray-600 dark:text-gray-400">
                    {_relative(s.last_ok_at)}
                  </td>
                  <td className="py-2 pr-2 text-xs text-gray-600 dark:text-gray-400">
                    {_relative(s.last_check_at)}
                  </td>
                  <td className="py-2">
                    {needsReauth && s.reauth_url ? (
                      <a
                        href={s.reauth_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-xs px-2 py-1 rounded bg-amber-500 hover:bg-amber-600 text-white"
                      >
                        Re-auth
                      </a>
                    ) : needsReauth ? (
                      <span className="text-xs text-gray-500">
                        Re-auth via SSH (no URL yet)
                      </span>
                    ) : (
                      <span className="text-xs text-gray-400">—</span>
                    )}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
