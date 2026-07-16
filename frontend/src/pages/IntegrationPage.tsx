import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Copy, RefreshCw, Eye, EyeOff, Check } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import { integrationApi } from '@/api'

/**
 * v5.8.1 — AI Integration admin page.
 *
 * Operator workflow:
 *   1. Toggle integration_enabled in Settings (or rotate to auto-generate
 *      a passphrase the first time).
 *   2. Click "Copy dev handoff" to get a markdown package the operator
 *      can paste into Slack / email for a developer team integrating
 *      a new project.
 *   3. The passphrase is editable in Settings → AI Integration. Rotate
 *      here when a leak is suspected — the new value renders ONCE.
 */
export function IntegrationPage() {
  const toastApi = useToast()
  const qc = useQueryClient()
  const [revealed, setRevealed] = useState(false)
  const [copiedKind, setCopiedKind] = useState<string | null>(null)
  const [newlyRotated, setNewlyRotated] = useState<string | null>(null)

  const { data, isLoading, refetch } = useQuery({
    queryKey: ['integration', 'dev-handoff'],
    queryFn: integrationApi.devHandoff,
  })

  const rotateMut = useMutation({
    mutationFn: integrationApi.rotatePassphrase,
    onSuccess: (resp) => {
      toastApi.success('Passphrase rotated. Copy it now — re-rotation is the only recovery.')
      setNewlyRotated(resp.passphrase)
      setRevealed(true)
      qc.invalidateQueries({ queryKey: ['integration', 'dev-handoff'] })
    },
    onError: (err: any) => toastApi.error(String(err?.message ?? err)),
  })

  function copy(text: string, kind: string) {
    navigator.clipboard.writeText(text).then(
      () => {
        setCopiedKind(kind)
        setTimeout(() => setCopiedKind(null), 2000)
        toastApi.success(`${kind} copied to clipboard`)
      },
      () => toastApi.error('Clipboard write failed'),
    )
  }

  if (isLoading) {
    return <div className="p-6">Loading…</div>
  }

  if (!data) {
    return <div className="p-6 text-red-600">Failed to load integration config.</div>
  }

  const displayPassphrase = newlyRotated ?? data.passphrase
  const isPassphraseSet = displayPassphrase && !displayPassphrase.startsWith('(NOT SET')

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div>
        <h1 className="text-2xl font-semibold mb-2">AI Integration</h1>
        <p className="text-sm text-gray-700 dark:text-gray-200 max-w-3xl">
          Lets other AI-driven projects discover this proxy via{' '}
          <code>/announce</code> and negotiate API keys via{' '}
          <code>/api/integration/chat</code>. The chat is passphrase-gated —
          the integrating AI must supply the passphrase below on every
          request. Use the "Copy dev handoff" button to hand a developer
          team everything they need.
        </p>
      </div>

      {/* Enabled status */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Status</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3">
            <span
              className={`inline-block w-3 h-3 rounded-full ${
                data.enabled ? 'bg-emerald-500' : 'bg-gray-400'
              }`}
            />
            <span className="text-sm">
              {data.enabled ? 'ENABLED — accepting integration requests' : 'DISABLED — toggle in Settings → AI Integration'}
            </span>
          </div>
        </CardContent>
      </Card>

      {/* URLs */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">URLs</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <UrlRow
            label="Announce (public, no auth)"
            url={data.announce_url}
            copiedKind={copiedKind === 'announce' ? 'announce' : null}
            onCopy={() => copy(data.announce_url, 'announce')}
          />
          <UrlRow
            label="Integration chat (passphrase-gated)"
            url={data.chat_url}
            copiedKind={copiedKind === 'chat' ? 'chat' : null}
            onCopy={() => copy(data.chat_url, 'chat')}
          />
        </CardContent>
      </Card>

      {/* Passphrase */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Shared passphrase</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex items-center gap-2">
            <code
              className={`flex-1 px-3 py-2 rounded bg-gray-100 dark:bg-gray-800 font-mono text-sm break-all ${
                !isPassphraseSet ? 'text-red-600' : ''
              }`}
            >
              {revealed
                ? displayPassphrase
                : isPassphraseSet
                  ? '••••••••••••••••••••••••••••••••'
                  : displayPassphrase}
            </code>
            <Button
              variant="secondary"
              onClick={() => setRevealed((r) => !r)}
              title={revealed ? 'Hide' : 'Reveal'}
            >
              {revealed ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
            </Button>
            <Button
              variant="secondary"
              disabled={!isPassphraseSet}
              onClick={() => copy(displayPassphrase, 'passphrase')}
              title="Copy"
            >
              {copiedKind === 'passphrase' ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                if (confirm('Rotate the passphrase? Any integrating clients with the old value will be locked out until you redistribute the new one.')) {
                  rotateMut.mutate()
                }
              }}
              disabled={rotateMut.isPending}
            >
              <RefreshCw className={`h-4 w-4 ${rotateMut.isPending ? 'animate-spin' : ''}`} />
              <span className="ml-1">Rotate</span>
            </Button>
          </div>
          {newlyRotated && (
            <div className="text-xs px-3 py-2 rounded bg-amber-50 dark:bg-amber-950 text-amber-800 dark:text-amber-200 border border-amber-200 dark:border-amber-900">
              ⚠ Just rotated. Copy the new value now — closing this page or re-fetching forgets the plaintext (the masked stored value remains in the DB and a future re-rotation is the only way to recover a lost one).
            </div>
          )}
          {!isPassphraseSet && (
            <div className="text-xs px-3 py-2 rounded bg-red-50 dark:bg-red-950 text-red-700 dark:text-red-200 border border-red-200 dark:border-red-900">
              No passphrase set. Click <b>Rotate</b> to generate one (or set it manually in Settings → AI Integration). Until then, the chat endpoint refuses every request.
            </div>
          )}
        </CardContent>
      </Card>

      {/* Limits summary */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Limits</CardTitle>
        </CardHeader>
        <CardContent>
          <ul className="text-sm space-y-1">
            <li>Default daily budget: <b>${data.limits.default_daily_budget_usd.toFixed(2)}</b></li>
            <li>Max daily budget (hard cap on minted keys): <b>${data.limits.max_daily_budget_usd.toFixed(2)}</b></li>
            <li>Max messages per chat session: <b>{data.limits.max_messages_per_session}</b></li>
          </ul>
          <p className="text-xs text-gray-800 dark:text-gray-100 mt-2">
            Edit these in Settings → AI Integration. Changes sync cluster-wide
            via the existing system_settings replication.
          </p>
        </CardContent>
      </Card>

      {/* Dev handoff */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Copy dev handoff</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-gray-700 dark:text-gray-200">
            One markdown package containing all three URLs, the current
            passphrase, a Python sample, a curl sample, and the limits.
            Paste into Slack / email for the integrating team. Throw it
            away after — it includes the passphrase in plaintext.
          </p>
          <div className="flex gap-2">
            <Button
              onClick={() => copy(data.markdown, 'handoff')}
              disabled={!isPassphraseSet}
            >
              {copiedKind === 'handoff' ? <Check className="h-4 w-4 mr-1" /> : <Copy className="h-4 w-4 mr-1" />}
              Copy markdown
            </Button>
            <Button variant="secondary" onClick={() => refetch()}>
              <RefreshCw className="h-4 w-4 mr-1" />
              Refresh
            </Button>
          </div>
          <details className="text-xs text-gray-800 dark:text-gray-100">
            <summary className="cursor-pointer">Preview ({data.markdown.length} chars)</summary>
            <pre className="mt-2 p-3 rounded bg-gray-50 dark:bg-gray-900 overflow-x-auto whitespace-pre-wrap max-h-64">
              {data.markdown.slice(0, 800)}
              {data.markdown.length > 800 ? '\n…\n(truncated; copy to see full)' : ''}
            </pre>
          </details>
        </CardContent>
      </Card>
    </div>
  )
}

function UrlRow({
  label,
  url,
  onCopy,
  copiedKind,
}: {
  label: string
  url: string
  onCopy: () => void
  copiedKind: string | null
}) {
  return (
    <div>
      <div className="text-xs text-gray-800 dark:text-gray-100 mb-1">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 px-3 py-2 rounded bg-gray-100 dark:bg-gray-800 font-mono text-xs break-all">
          {url}
        </code>
        <Button variant="secondary" onClick={onCopy} title="Copy">
          {copiedKind ? <Check className="h-4 w-4" /> : <Copy className="h-4 w-4" />}
        </Button>
      </div>
    </div>
  )
}
