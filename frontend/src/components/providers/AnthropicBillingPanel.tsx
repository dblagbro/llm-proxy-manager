/**
 * v3.7.5 — Anthropic Console billing scrape panel for claude-oauth providers.
 *
 * Surfaces:
 *   - The provider's currently-stored org_uuid + a "cookies are N days old" badge.
 *   - A cookie-paste textarea + org_uuid input for refreshing credentials when
 *     a sessionKey expires (~30d cadence). Posts to
 *     POST /api/providers/{id}/anthropic-billing-credentials.
 *   - A "Refresh now" button that fires a one-shot scrape immediately and
 *     prints the real five_hour/seven_day percentages. Posts to
 *     POST /api/providers/{id}/anthropic-billing-refresh.
 *   - A snapshots table (latest 10) so the operator can see what's been
 *     collected and when, including failure rows (auth_state != "ok").
 *   - The auto-rotation banner if auto_skip_until is set: shows reason +
 *     countdown to reset.
 *
 * Only rendered when provider_type === "claude-oauth". For all other
 * provider types the modal is unchanged.
 */
import { useEffect, useState } from 'react'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import { providersApi } from '@/api'
import { useToast } from '@/components/ui/Toast'
import type { Provider } from '@/types'

interface Snapshot {
  id: number
  captured_at: string | null
  source: string | null
  http_status: number | null
  auth_state: string | null
  error: string | null
  five_hour_utilization: number | null
  seven_day_utilization: number | null
  seven_day_sonnet_utilization: number | null
  seven_day_opus_utilization: number | null
  extra_usage_is_enabled: boolean | null
  extra_usage_monthly_limit: number | null
  extra_usage_used_credits: number | null
  extra_usage_utilization: number | null
  extra_usage_currency: string | null
}

interface Props {
  provider: Provider
  onUpdated?: () => void
}

function daysSince(unixTs: number | null | undefined): number | null {
  if (!unixTs) return null
  const ms = Date.now() - unixTs * 1000
  return Math.floor(ms / 86400_000)
}

function fmtCountdown(iso: string | null | undefined): string {
  if (!iso) return ''
  try {
    const target = new Date(iso).getTime()
    const ms = target - Date.now()
    if (ms <= 0) return 'expired'
    const min = Math.floor(ms / 60_000)
    if (min < 60) return `${min}m`
    const hr = Math.floor(min / 60)
    const rem = min % 60
    return `${hr}h ${rem}m`
  } catch { return '' }
}

function pctColor(pct: number | null | undefined): string {
  if (pct === null || pct === undefined) return 'text-gray-500 dark:text-gray-400'
  if (pct >= 95) return 'text-red-600 dark:text-red-400 font-semibold'
  if (pct >= 80) return 'text-amber-600 dark:text-amber-400'
  if (pct >= 50) return 'text-yellow-600 dark:text-yellow-400'
  return 'text-emerald-600 dark:text-emerald-400'
}

export function AnthropicBillingPanel({ provider, onUpdated }: Props) {
  const toast = useToast()
  const [orgUuid, setOrgUuid] = useState(provider.anthropic_org_uuid ?? '')
  const [cookies, setCookies] = useState('')
  const [showPaste, setShowPaste] = useState(false)
  const [snapshots, setSnapshots] = useState<Snapshot[]>([])
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [saving, setSaving] = useState(false)

  const has_cookies = !!provider.has_anthropic_session_cookies
  const captured_at = provider.anthropic_session_captured_at ?? null
  const captured_days = daysSince(captured_at)
  const skip_until = provider.auto_skip_until ?? null
  const skip_reason = provider.auto_skip_reason ?? null

  async function loadSnapshots() {
    setLoading(true)
    try {
      const rows = await providersApi.listSnapshots(provider.id, 10)
      setSnapshots(rows)
    } catch (e) {
      // Quiet — first load may happen before any snapshot exists
      setSnapshots([])
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (provider.id && has_cookies) {
      loadSnapshots()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider.id, has_cookies])

  async function handleSaveCredentials() {
    if (!orgUuid.trim()) {
      toast.error('org_uuid is required')
      return
    }
    if (!cookies.trim()) {
      toast.error('cookies blob is required')
      return
    }
    setSaving(true)
    try {
      const r = await providersApi.setBillingCredentials(provider.id, {
        org_uuid: orgUuid.trim(),
        cookies: cookies.trim(),
      })
      toast.success(`Credentials stored (${r.cookie_count} cookies)`)
      setCookies('')
      setShowPaste(false)
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Failed to save credentials')
    } finally {
      setSaving(false)
    }
  }

  async function handleRefreshNow() {
    setRefreshing(true)
    try {
      const r = await providersApi.refreshBillingNow(provider.id)
      if (r.ok) {
        const msg = `Scrape OK — 7d: ${r.seven_day_utilization?.toFixed(1) ?? '?'}% · 5h: ${r.five_hour_utilization?.toFixed(1) ?? '?'}%`
        toast.success(msg)
      } else {
        toast.error(`Scrape failed: ${r.auth_state}${r.http_status ? ` (HTTP ${r.http_status})` : ''}`)
      }
      await loadSnapshots()
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  return (
    <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex items-center justify-between mb-3">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            External Usage (Anthropic Console)
          </h4>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Authoritative weekly + per-model utilization from
            <code className="mx-1">claude.ai/api/organizations/&#123;uuid&#125;/usage</code>
            via a 4-hourly browser-cookie scrape. Routing decisions
            (v3.7.1+) use this as the rotation signal for claude-oauth.
          </p>
        </div>
      </div>

      {/* Auto-rotation status banner */}
      {skip_until && (
        <div className="mb-3 rounded border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/40 p-3 text-sm">
          <div className="flex items-center justify-between">
            <span className="font-semibold text-red-900 dark:text-red-200">
              🚦 Auto-skipped — router is bypassing this provider
            </span>
            <span className="text-xs text-red-700 dark:text-red-300">
              resets in {fmtCountdown(skip_until)}
            </span>
          </div>
          {skip_reason && (
            <div className="mt-1 text-xs text-red-800 dark:text-red-300">{skip_reason}</div>
          )}
        </div>
      )}

      {/* Credentials state */}
      <div className="mb-3 grid grid-cols-1 md:grid-cols-2 gap-3">
        <div>
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
            Organization UUID
          </label>
          <div className="font-mono text-xs text-gray-700 dark:text-gray-300">
            {provider.anthropic_org_uuid || <span className="text-gray-400">— not configured —</span>}
          </div>
        </div>
        <div>
          <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
            Session cookies
          </label>
          <div className="text-xs text-gray-700 dark:text-gray-300">
            {has_cookies ? (
              <>
                <span className="text-emerald-600 dark:text-emerald-400">●</span> stored
                {captured_days !== null && (
                  <span className="text-gray-500 dark:text-gray-400 ml-2">
                    captured {captured_days === 0 ? 'today' : `${captured_days}d ago`}
                    {captured_days >= 25 && (
                      <span className="ml-1 text-amber-600">— refresh soon (~30d lifetime)</span>
                    )}
                  </span>
                )}
              </>
            ) : (
              <span className="text-gray-400">— not configured —</span>
            )}
          </div>
        </div>
      </div>

      <div className="flex gap-2 mb-3">
        <Button
          variant="secondary"
          onClick={() => setShowPaste(!showPaste)}
        >
          {has_cookies ? 'Rotate cookies' : 'Paste cookies'}
        </Button>
        {has_cookies && (
          <Button
            variant="secondary"
            onClick={handleRefreshNow}
            disabled={refreshing}
          >
            {refreshing ? 'Refreshing…' : 'Refresh now'}
          </Button>
        )}
      </div>

      {/* Paste workflow */}
      {showPaste && (
        <div className="mb-3 rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900 p-3 space-y-2">
          <p className="text-xs text-gray-600 dark:text-gray-400">
            Capture from a real browser: sign into <code>claude.ai</code> → DevTools (F12) →
            Network → reload → click any <code>claude.ai</code> request → Headers →
            right-click the <code>Cookie:</code> request header → Copy value. Paste below
            (cookie-header style <code>name=val; name=val</code>, or JSON dict).
          </p>
          <Input
            label="Organization UUID"
            value={orgUuid}
            onChange={e => setOrgUuid(e.target.value)}
            placeholder="69bf83f2-a466-4bd2-97b6-6d39fb3927c3"
          />
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
              Cookie blob
            </label>
            <textarea
              value={cookies}
              onChange={e => setCookies(e.target.value)}
              rows={4}
              className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-3 py-1.5 text-xs font-mono"
              placeholder="sessionKey=...; sessionKeyLC=...; routingHint=...; lastActiveOrg=...; cf_clearance=..."
            />
          </div>
          <div className="flex gap-2">
            <Button onClick={handleSaveCredentials} disabled={saving}>
              {saving ? 'Saving…' : 'Save credentials'}
            </Button>
            <Button variant="secondary" onClick={() => { setShowPaste(false); setCookies('') }}>
              Cancel
            </Button>
          </div>
        </div>
      )}

      {/* Snapshots table */}
      {has_cookies && (
        <div>
          <div className="flex items-center justify-between mb-1">
            <h5 className="text-xs font-medium text-gray-700 dark:text-gray-300">
              Recent snapshots
            </h5>
            <Button variant="ghost" onClick={loadSnapshots} disabled={loading}>
              {loading ? '…' : 'Reload'}
            </Button>
          </div>
          {snapshots.length === 0 ? (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              No snapshots yet. Click <strong>Refresh now</strong> to fire one immediately.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-gray-500 dark:text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th className="py-1 pr-3">Captured</th>
                    <th className="py-1 pr-3">State</th>
                    <th className="py-1 pr-3">7d</th>
                    <th className="py-1 pr-3">5h</th>
                    <th className="py-1 pr-3">Sonnet 7d</th>
                    <th className="py-1 pr-3">Extra</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshots.map(s => (
                    <tr key={s.id} className="border-b border-gray-100 dark:border-gray-800">
                      <td className="py-1 pr-3 font-mono text-gray-600 dark:text-gray-400">
                        {s.captured_at ? new Date(s.captured_at).toLocaleString() : '-'}
                      </td>
                      <td className="py-1 pr-3">
                        {s.auth_state === 'ok' ? (
                          <span className="text-emerald-600 dark:text-emerald-400">ok</span>
                        ) : (
                          <span className="text-red-600 dark:text-red-400" title={s.error ?? ''}>
                            {s.auth_state ?? 'unknown'}
                          </span>
                        )}
                      </td>
                      <td className={`py-1 pr-3 ${pctColor(s.seven_day_utilization)}`}>
                        {s.seven_day_utilization != null ? `${s.seven_day_utilization.toFixed(1)}%` : '-'}
                      </td>
                      <td className={`py-1 pr-3 ${pctColor(s.five_hour_utilization)}`}>
                        {s.five_hour_utilization != null ? `${s.five_hour_utilization.toFixed(1)}%` : '-'}
                      </td>
                      <td className={`py-1 pr-3 ${pctColor(s.seven_day_sonnet_utilization)}`}>
                        {s.seven_day_sonnet_utilization != null ? `${s.seven_day_sonnet_utilization.toFixed(1)}%` : '-'}
                      </td>
                      <td className="py-1 pr-3 text-gray-600 dark:text-gray-400">
                        {s.extra_usage_is_enabled
                          ? `${s.extra_usage_used_credits?.toFixed(0) ?? '-'}/${s.extra_usage_monthly_limit?.toFixed(0) ?? '-'} ${s.extra_usage_currency ?? ''}`
                          : '-'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
