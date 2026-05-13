/**
 * v3.7.27 (#245) — ChatGPT Plus / Codex Cloud usage scrape panel.
 *
 * Mirrors AnthropicBillingPanel for codex-oauth providers. Unlike the
 * Anthropic case (where the API endpoint is hardcoded), the operator
 * supplies BOTH the analytics endpoint URL (captured from DevTools)
 * AND the chatgpt.com session cookies. The 4h worker fires a GET
 * against that URL and stores the response in external_usage_snapshot.
 *
 * Only rendered when provider_type === "ChatGPT-oauth-plan".
 *
 * Phase 1: capture + raw_response storage. Field extraction lands in
 * Phase 2 once a sample response confirms the shape.
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

function daysSince(unixTs: number | null | undefined): number | null {
  if (!unixTs) return null
  const ms = Date.now() - unixTs * 1000
  return Math.floor(ms / 86400_000)
}

export function CodexBillingPanel({ provider, onUpdated }: Props) {
  const toast = useToast()
  const [endpointUrl, setEndpointUrl] = useState(provider.codex_usage_endpoint_url ?? '')
  const [cookies, setCookies] = useState('')
  const [showPaste, setShowPaste] = useState(false)
  const [saving, setSaving] = useState(false)
  const [refreshing, setRefreshing] = useState(false)

  const has_cookies = !!provider.has_codex_session_cookies
  const captured_at = provider.codex_session_captured_at ?? null
  const captured_days = daysSince(captured_at)

  async function handleSaveCredentials() {
    if (!endpointUrl.trim()) {
      toast.error('analytics endpoint URL is required')
      return
    }
    if (!cookies.trim()) {
      toast.error('cookies blob is required')
      return
    }
    setSaving(true)
    try {
      const r = await providersApi.setCodexBillingCredentials(provider.id, {
        endpoint_url: endpointUrl.trim(),
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
      const r = await providersApi.refreshCodexBillingNow(provider.id)
      if (r.ok) {
        toast.success(`Scrape OK — ${r.http_status ?? '?'} (snapshot #${r.snapshot_id ?? '?'})`)
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

  return (
    <div className="md:col-span-2 mt-6 border-t border-gray-200 dark:border-gray-700 pt-4">
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <h4 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
            External Usage (ChatGPT / Codex Cloud)
          </h4>
          <p className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
            Authoritative subscription utilization scraped from a
            chatgpt.com analytics endpoint you capture from DevTools.
            Same purpose as the Anthropic Console scrape: closes the
            blind spot where ChatGPT Plus accounts are also used
            outside this proxy (mobile, chat UI), so local counters
            undercount real usage.
          </p>
        </div>
      </div>

      <div className="rounded border border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-900/40 p-3 mb-3 text-sm">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div className="text-gray-700 dark:text-gray-300">
            {has_cookies ? (
              <>
                <span className="font-medium">Status:</span> credentials stored.
                {captured_days !== null && (
                  <span className={`ml-2 text-xs ${captured_days >= 14 ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500'}`}>
                    cookies pasted {captured_days}d ago
                    {captured_days >= 14 && ' — may need refresh soon'}
                  </span>
                )}
              </>
            ) : (
              <span className="text-amber-600 dark:text-amber-400">
                No credentials stored — paste the analytics URL + cookies below.
              </span>
            )}
          </div>
          {has_cookies && (
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
        {provider.codex_usage_endpoint_url && (
          <div className="mt-2 text-xs text-gray-500 dark:text-gray-400 font-mono break-all">
            endpoint: {provider.codex_usage_endpoint_url}
          </div>
        )}
      </div>

      {!showPaste ? (
        <Button variant="outline" size="sm" onClick={() => setShowPaste(true)}>
          {has_cookies ? 'Replace credentials' : 'Paste credentials'}
        </Button>
      ) : (
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
              Analytics endpoint URL
            </label>
            <input
              type="text"
              value={endpointUrl}
              onChange={e => setEndpointUrl(e.target.value)}
              placeholder="https://chatgpt.com/backend-api/.../usage"
              className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm px-3 py-1.5 font-mono"
            />
            <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1 leading-snug">
              Open <code>https://chatgpt.com/codex/cloud/settings/analytics</code> in a logged-in tab.
              DevTools → Network → reload → find the XHR call that returns the usage JSON →
              copy the full request URL here.
            </p>
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-gray-300 mb-1">
              chatgpt.com cookies (JSON dict or header style)
            </label>
            <textarea
              value={cookies}
              onChange={e => setCookies(e.target.value)}
              rows={5}
              placeholder='{"__Secure-next-auth.session-token": "...", "cf_clearance": "...", ...}'
              className="w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-xs px-3 py-2 font-mono"
            />
            <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1 leading-snug">
              DevTools → Application → Cookies → <code>chatgpt.com</code> → copy all cookies as a
              JSON object. The session token cookie is required; Cloudflare cookies are recommended.
            </p>
          </div>
          <div className="flex items-center gap-2">
            <Button onClick={handleSaveCredentials} loading={saving} disabled={saving}>
              Save credentials
            </Button>
            <Button variant="outline" onClick={() => { setShowPaste(false); setCookies('') }}>
              Cancel
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
