/**
 * v5.3.5 — Cursor dashboard usage scrape panel for cursor-oauth providers.
 *
 * Parity ship for the v4.4.41 Cursor billing scraper. The worker
 * shipped in v4.4.41 (4h cadence) but the manual-trigger UI was never
 * wired — Cursor stayed behind Anthropic + Codex on this surface.
 *
 * Cursor uses the same no-cookie design as Codex: the scraper hits
 * cursor.com endpoints via the existing OAuth access_token already
 * managed by the cursor-oauth login flow. No separate credentials
 * needed — same pattern as ``CodexBillingPanel``.
 *
 * Only rendered when provider_type === "cursor-oauth".
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

export function CursorBillingPanel({ provider, onUpdated }: Props) {
  const toast = useToast()
  const [refreshing, setRefreshing] = useState(false)

  async function handleRefreshNow() {
    setRefreshing(true)
    try {
      const r = await providersApi.refreshCursorBillingNow(provider.id)
      if (r.ok) {
        const sd = r.seven_day_utilization
        const sdStr = (sd ?? null) !== null ? `${sd!.toFixed(0)}%` : '?'
        toast.success(`Scrape OK — 7d: ${sdStr}`)
      } else {
        toast.error(
          `Scrape failed: ${r.auth_state}${r.http_status ? ` (HTTP ${r.http_status})` : ''}`,
        )
      }
      onUpdated?.()
    } catch (e: unknown) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Refresh failed')
    } finally {
      setRefreshing(false)
    }
  }

  // The cursor-oauth login flow populates provider.api_key with the
  // OAuth access_token. If the provider exists at all it's been
  // through that flow — we can't see the raw token, so use presence as
  // the only viable client-side signal.
  const has_oauth = !!provider.id

  return (
    <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            External Usage (Cursor dashboard)
          </h4>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Authoritative subscription utilization scraped from{' '}
            <code className="mx-1">cursor.com</code>{' '}
            via a 4-hourly call using this provider's OAuth access token
            (same bearer the inference path uses). Mirrors the Anthropic +
            Codex panels — closes the blind spot where the same Cursor
            account is consumed by the Cursor IDE itself on the operator's
            workstation.
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
                No OAuth token — complete the cursor-oauth sign-in flow first.
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
          Worker scrapes every 4 hours by default. Use{' '}
          <strong>Refresh now</strong> right after a Pro upgrade to confirm
          the new tier propagated, or after re-auth to verify the new token
          works against cursor.com.
        </p>
      </div>
    </div>
  )
}
