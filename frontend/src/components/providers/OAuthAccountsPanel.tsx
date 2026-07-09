/**
 * v5.15.2 (#508) — Per-provider OAuth Accounts panel.
 *
 * Renders on the ProviderForm for OAuth-flavored providers (cursor-oauth,
 * codex-oauth, claude-oauth). Lets the operator:
 *
 * - List enabled + soft-deleted accounts on this provider
 * - Add a new account (label + paste-fallback access_token + optional
 *   refresh_token + expires_at)
 * - Toggle enabled / disabled per account
 * - Rename the label
 * - Soft-delete an account (safe: doesn't purge; can be restored by
 *   POSTing a new one with the same tokens)
 *
 * v5.15.1 flipped dispatch to read from this table (via
 * `apply_fanout_to_kwargs` in messages.py). Every dispatch that lands on
 * a provider with ≥1 enabled account here uses the picker to spread
 * load — this panel is the operator's window into that picker.
 *
 * The account's access_token + refresh_token are rendered but masked by
 * default (click-to-reveal). Same posture as api_key on the parent
 * ProviderForm. If we ever wire tenant-scoped RBAC (#511), this panel
 * becomes read-only for non-admin users.
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import { api } from '@/api/client'
import { getBasePath } from '@/lib/basePath'
import type { Provider } from '@/types'

const OAUTH_TYPES = new Set(['cursor-oauth', 'codex-oauth', 'claude-oauth'])

interface OAuthAccount {
  id: string
  provider_id: string
  label: string
  access_token: string
  refresh_token: string | null
  oauth_expires_at: number | null
  enabled: boolean
  last_used_at: number | null
  utilization_pct: number | null
  captured_via: string | null
  created_at: string | null
  updated_at: string | null
}

interface Props {
  provider: Provider
  onUpdated?: () => void
}

function fmtExpiresAt(ts: number | null): string {
  if (!ts) return '—'
  const d = new Date(ts * 1000)
  return d.toISOString().slice(0, 10)
}

function fmtLastUsed(ts: number | null): string {
  if (!ts) return 'never'
  const ageSec = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (ageSec < 60) return `${ageSec}s ago`
  if (ageSec < 3600) return `${Math.floor(ageSec / 60)}m ago`
  if (ageSec < 86400) return `${Math.floor(ageSec / 3600)}h ago`
  return `${Math.floor(ageSec / 86400)}d ago`
}

function mask(token: string): string {
  if (!token) return ''
  if (token.length <= 12) return '•'.repeat(token.length)
  return `${token.slice(0, 6)}…${token.slice(-4)}`
}

export function OAuthAccountsPanel({ provider, onUpdated }: Props) {
  const toast = useToast()
  const [accounts, setAccounts] = useState<OAuthAccount[]>([])
  const [loading, setLoading] = useState(true)
  const [reveal, setReveal] = useState<Record<string, boolean>>({})
  const [adding, setAdding] = useState(false)
  const [newLabel, setNewLabel] = useState('')
  const [newAccess, setNewAccess] = useState('')
  const [newRefresh, setNewRefresh] = useState('')
  const [newExpiresAt, setNewExpiresAt] = useState('')

  const isOAuthProvider = OAUTH_TYPES.has(provider.provider_type)
  const basePath = getBasePath()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const rows = await api.get<OAuthAccount[]>(
        `/api/admin/providers/${provider.id}/oauth-accounts`,
      )
      setAccounts(rows)
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Failed to load OAuth accounts')
    } finally {
      setLoading(false)
    }
  }, [provider.id, toast])

  useEffect(() => {
    if (isOAuthProvider && provider.id) {
      load()
    }
  }, [isOAuthProvider, provider.id, load])

  async function handleAdd() {
    if (!newLabel.trim() || !newAccess.trim()) {
      toast.error('Label and access_token are required')
      return
    }
    setAdding(true)
    try {
      const expTs = newExpiresAt ? Math.floor(new Date(newExpiresAt).getTime() / 1000) : null
      const body: Record<string, unknown> = {
        label: newLabel.trim(),
        access_token: newAccess.trim(),
        refresh_token: newRefresh.trim() || null,
        oauth_expires_at: expTs,
        enabled: true,
        captured_via: 'manual_paste',
      }
      await api.post<OAuthAccount>(
        `/api/admin/providers/${provider.id}/oauth-accounts`,
        body,
      )
      toast.success('Account added')
      setNewLabel('')
      setNewAccess('')
      setNewRefresh('')
      setNewExpiresAt('')
      await load()
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Add failed')
    } finally {
      setAdding(false)
    }
  }

  async function handleToggle(acc: OAuthAccount) {
    try {
      await api.patch<OAuthAccount>(
        `/api/admin/providers/${provider.id}/oauth-accounts/${acc.id}`,
        { enabled: !acc.enabled },
      )
      toast.success(acc.enabled ? 'Disabled' : 'Enabled')
      await load()
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Toggle failed')
    }
  }

  async function handleRename(acc: OAuthAccount) {
    const next = window.prompt('New label:', acc.label)
    if (!next || next === acc.label) return
    try {
      await api.patch<OAuthAccount>(
        `/api/admin/providers/${provider.id}/oauth-accounts/${acc.id}`,
        { label: next.trim() },
      )
      toast.success('Renamed')
      await load()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Rename failed')
    }
  }

  async function handleDelete(acc: OAuthAccount) {
    if (!window.confirm(`Soft-delete account "${acc.label}"?`)) return
    try {
      await fetch(
        `${basePath}/api/admin/providers/${provider.id}/oauth-accounts/${acc.id}`,
        { method: 'DELETE', credentials: 'include' },
      )
      toast.success('Deleted')
      await load()
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Delete failed')
    }
  }

  const enabledCount = useMemo(() => accounts.filter(a => a.enabled).length, [accounts])
  const strategyName = provider.oauth_account_strategy || 'least_utilized (default)'

  if (!isOAuthProvider) return null

  return (
    <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            OAuth Accounts <span className="text-xs font-normal text-gray-500 dark:text-gray-400">
              — v5.15.1 per-account fan-out
            </span>
          </h3>
          <p className="text-xs text-gray-600 dark:text-gray-400 mt-1">
            {enabledCount} enabled · pick strategy: <code className="text-xs">{strategyName}</code>
          </p>
        </div>
      </div>

      {loading ? (
        <p className="text-sm text-gray-500 dark:text-gray-400">Loading…</p>
      ) : accounts.length === 0 ? (
        <p className="text-sm text-gray-500 dark:text-gray-400 italic">
          No accounts yet — legacy <code>api_key</code> is used until you add one.
        </p>
      ) : (
        <table className="w-full text-xs mt-2">
          <thead>
            <tr className="text-left border-b border-gray-200 dark:border-gray-700">
              <th className="py-1 pr-2">Label</th>
              <th className="py-1 pr-2">Access token</th>
              <th className="py-1 pr-2">Expires</th>
              <th className="py-1 pr-2">Last used</th>
              <th className="py-1 pr-2">Util%</th>
              <th className="py-1 pr-2">Enabled</th>
              <th className="py-1 pr-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {accounts.map((a) => (
              <tr key={a.id} className={`border-b border-gray-100 dark:border-gray-800 ${a.enabled ? '' : 'opacity-50'}`}>
                <td className="py-1 pr-2 font-mono">{a.label}</td>
                <td className="py-1 pr-2 font-mono">
                  <button
                    className="text-blue-600 dark:text-blue-400 hover:underline"
                    onClick={() => setReveal(prev => ({ ...prev, [a.id]: !prev[a.id] }))}
                    type="button"
                  >
                    {reveal[a.id] ? a.access_token : mask(a.access_token)}
                  </button>
                </td>
                <td className="py-1 pr-2">{fmtExpiresAt(a.oauth_expires_at)}</td>
                <td className="py-1 pr-2">{fmtLastUsed(a.last_used_at)}</td>
                <td className="py-1 pr-2">{a.utilization_pct?.toFixed(0) ?? '—'}</td>
                <td className="py-1 pr-2">
                  <input
                    type="checkbox"
                    checked={a.enabled}
                    onChange={() => handleToggle(a)}
                    className="cursor-pointer"
                  />
                </td>
                <td className="py-1 pr-2">
                  <button
                    onClick={() => handleRename(a)}
                    className="text-blue-600 dark:text-blue-400 hover:underline mr-2"
                    type="button"
                  >
                    Rename
                  </button>
                  <button
                    onClick={() => handleDelete(a)}
                    className="text-red-600 dark:text-red-400 hover:underline"
                    type="button"
                  >
                    Delete
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="mt-4 p-3 border border-dashed border-gray-300 dark:border-gray-600 rounded">
        <h4 className="text-xs font-semibold mb-2 text-gray-900 dark:text-gray-100">Add account</h4>
        <div className="grid grid-cols-2 gap-2">
          <input
            type="text"
            placeholder="Label (e.g. work@example.com)"
            value={newLabel}
            onChange={(e) => setNewLabel(e.target.value)}
            className="text-xs p-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
          />
          <input
            type="text"
            placeholder="Expires at (YYYY-MM-DD, optional)"
            value={newExpiresAt}
            onChange={(e) => setNewExpiresAt(e.target.value)}
            className="text-xs p-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800"
          />
          <input
            type="password"
            placeholder="Access token"
            value={newAccess}
            onChange={(e) => setNewAccess(e.target.value)}
            className="text-xs p-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 col-span-2"
          />
          <input
            type="password"
            placeholder="Refresh token (optional)"
            value={newRefresh}
            onChange={(e) => setNewRefresh(e.target.value)}
            className="text-xs p-1 border border-gray-300 dark:border-gray-600 rounded bg-white dark:bg-gray-800 col-span-2"
          />
        </div>
        <div className="mt-2">
          <Button
            onClick={handleAdd}
            disabled={adding || !newLabel.trim() || !newAccess.trim()}
            size="sm"
          >
            {adding ? 'Adding…' : 'Add account'}
          </Button>
        </div>
      </div>

      <p className="text-xs text-gray-500 dark:text-gray-400 mt-3">
        Dispatch picks one account per request via the strategy above and
        emits <code>X-OAuth-Account: &lt;id&gt;</code> on the response.
        Disabled or soft-deleted accounts are skipped. Legacy
        <code> api_key</code> is used when no accounts exist.
      </p>
    </div>
  )
}
