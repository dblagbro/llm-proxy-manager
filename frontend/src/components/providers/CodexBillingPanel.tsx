/**
 * v3.8.1 (#245 Phase 2) — ChatGPT Plus / Codex Cloud usage scrape panel.
 *
 * Phase 2 dropped the operator-paste flow. The scraper now uses the
 * provider's existing OAuth access_token (managed by the codex-oauth
 * refresh flow) to call https://chatgpt.com/backend-api/wham/usage.
 * No separate credentials needed.
 *
 * Panel renders a status summary + a "Refresh now" button. Only
 * rendered when provider_type === "ChatGPT-oauth-plan".
 */
import { useState } from 'react'
import { Button } from '@/components/ui/Button'
import { providersApi } from '@/api'
import { useToast } from '@/components/ui/Toast'
import type { Provider } from '@/types'

interface Props {
  provider: Provider
  onUpdated?: () => void
}

export function CodexBillingPanel({ provider, onUpdated }: Props) {
  const toast = useToast()
  const [refreshing, setRefreshing] = useState(false)

  async function handleRefreshNow() {
    setRefreshing(true)
    try {
      const r = await providersApi.refreshCodexBillingNow(provider.id)
      if (r.ok) {
        const fh = (r as { five_hour_utilization?: number | null }).five_hour_utilization
        const sd = (r as { seven_day_utilization?: number | null }).seven_day_utilization
        const fhStr = (fh ?? null) !== null ? `${fh!.toFixed(0)}%` : '?'
        const sdStr = (sd ?? null) !== null ? `${sd!.toFixed(0)}%` : '?'
        toast.success(`Scrape OK — 5h: ${fhStr} · 7d: ${sdStr}`)
      } else {
        toast.error(`Scrape failed: ${r.auth_state}${r.http_status ? ` (HTTP ${r.http_status})` : ''}`)
      }
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  // Bearer is the existing OAuth access_token — populated on the
  // provider's api_key field. Surface via the provider object's
  // auth state (we can't see the raw token from the API, but if the
  // provider exists at all it's been through the OAuth flow).
  const has_oauth = !!provider.id

  return (
    <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            External Usage (ChatGPT / Codex Cloud)
          </h4>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Authoritative subscription utilization scraped from{' '}
            <code className="mx-1">chatgpt.com/backend-api/wham/usage</code>{' '}
            via a 4-hourly call using this provider's OAuth access token
            (same bearer the inference path uses). Closes the blind spot
            where the same ChatGPT Plus account is consumed by other
            channels (mobile, chat UI, Codex CLI on other workstations).
          </p>
        </div>
      </div>

      <div className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3 mb-3 text-sm">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="text-gray-700 dark:text-gray-300">
            {has_oauth ? (
              <>
                <span className="font-medium">Auth:</span> using OAuth access_token (auto-refreshed).
                No manual credential paste required.
              </>
            ) : (
              <span className="text-amber-600 dark:text-amber-400">
                No OAuth token — complete the ChatGPT subscription sign-in flow first.
              </span>
            )}
          </div>
          {has_oauth && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleRefreshNow}
              loading={refreshing}
              disabled={refreshing}
            >
              Refresh now
            </Button>
          )}
        </div>
        <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-2 leading-snug">
          Worker scrapes every 4 hours by default. If the access token expires (chatgpt.com
          bearer is JWT-style and rotates via refresh_token), the scraper will lazy-refresh
          using the stored refresh_token — same flow as the inference path.
        </p>
      </div>
    </div>
  )
}
