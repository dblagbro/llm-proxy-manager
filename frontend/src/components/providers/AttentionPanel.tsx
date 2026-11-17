import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink } from 'lucide-react'
import { providersApi, type AttentionItem } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { useToast } from '@/components/ui/Toast'
import { providerToForm } from '@/components/providers/ProviderForm'
import type { Provider } from '@/types'

const REASON_LABEL: Record<AttentionItem['reason'], string> = {
  billing: 'Billing / spend cap',
  auth: 'Invalid key / auth',
  rate_limit: 'Rate-limited',
  logged_out: 'Logged out',
  unreachable: 'Unreachable',
}

function fmtRetry(sec: number | null): string {
  if (!sec || sec <= 0) return 'on next probe'
  if (sec < 90) return `in ~${Math.round(sec)}s`
  if (sec < 5400) return `in ~${Math.round(sec / 60)} min`
  return `in ~${(sec / 3600).toFixed(1)} h`
}

function Row({ item, provider }: { item: AttentionItem; provider?: Provider }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [newKey, setNewKey] = useState('')
  const [msg, setMsg] = useState<string | null>(null)

  function refresh() {
    qc.invalidateQueries({ queryKey: ['provider-attention'] })
    qc.invalidateQueries({ queryKey: ['providers'] })
    qc.invalidateQueries({ queryKey: ['health'] })
  }

  const retest = useMutation({
    mutationFn: () => providersApi.attentionRetest(item.provider_id),
    onSuccess: res => {
      if (res.success) {
        setMsg(null)
        toast.success(`${item.name}: test passed, breaker reset on this node`)
      } else {
        setMsg(res.error || 'Test failed')
      }
      refresh()
    },
    onError: (e: Error) => setMsg(e.message),
  })

  // Save via the existing provider update endpoint, then run Test; the
  // backend resets the breaker only if Test passes. The key is never echoed.
  const replaceKey = useMutation({
    mutationFn: async () => {
      if (!provider) throw new Error('Provider data not loaded')
      await providersApi.update(provider.id, { ...providerToForm(provider), api_key: newKey })
      setNewKey('')
      return providersApi.attentionRetest(provider.id)
    },
    onSuccess: res => {
      if (res.success) {
        setMsg(null)
        toast.success(`${item.name}: key saved, test passed, breaker reset on this node`)
      } else {
        setMsg(`Key saved but test failed: ${res.error || 'unknown error'}`)
      }
      refresh()
    },
    onError: (e: Error) => setMsg(e.message),
  })

  const busy = retest.isPending || replaceKey.isPending

  return (
    <div className="rounded-lg border border-amber-300 dark:border-amber-700 p-3 space-y-2">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-medium text-sm text-gray-900 dark:text-gray-100">{item.name}</span>
        <Badge variant="muted">{item.provider_type}</Badge>
        <Badge variant={item.reason === 'rate_limit' ? 'warning' : 'danger'}>{REASON_LABEL[item.reason]}</Badge>
        <span className="text-xs text-gray-500 dark:text-gray-400">breaker {item.breaker_state}</span>
      </div>

      {item.reason === 'billing' && (
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <span className="text-gray-600 dark:text-gray-300">Spend cap or credits exhausted (held up to 6h).</span>
          {item.billing_url && (
            <a href={item.billing_url} target="_blank" rel="noopener noreferrer"
               className="inline-flex items-center text-indigo-500 hover:underline">
              Open vendor billing <ExternalLink className="h-3.5 w-3.5 ml-1" />
            </a>
          )}
          <Button size="sm" variant="outline" loading={busy} onClick={() => retest.mutate()}>
            I raised it — re-test now
          </Button>
        </div>
      )}

      {item.reason === 'auth' && (
        <div className="space-y-2">
          {item.last_error && <p className="text-xs text-red-400">{item.last_error}</p>}
          {item.can_replace_key && (
            <form
              className="flex items-end gap-2 flex-wrap"
              onSubmit={e => { e.preventDefault(); if (newKey) replaceKey.mutate() }}
            >
              <div className="min-w-[16rem] flex-1">
                <Input
                  label="Replace key"
                  type="password"
                  autoComplete="off"
                  value={newKey}
                  onChange={e => setNewKey(e.target.value)}
                  placeholder="Paste the new API key"
                />
              </div>
              <Button size="sm" type="submit" loading={busy} disabled={!newKey || !provider}>
                Save &amp; test
              </Button>
            </form>
          )}
        </div>
      )}

      {item.reason === 'rate_limit' && (
        <p className="text-sm text-gray-600 dark:text-gray-300">
          Upstream is rate-limiting; probing backs off automatically. Next retry {fmtRetry(item.retry_in_sec)}.
        </p>
      )}

      {item.reason === 'unreachable' && (
        <div className="flex items-center gap-2 flex-wrap text-sm">
          <span className="text-gray-600 dark:text-gray-300">
            Upstream not responding. Next automatic retry {fmtRetry(item.retry_in_sec)}.
          </span>
          <Button size="sm" variant="outline" loading={busy} onClick={() => retest.mutate()}>Re-test now</Button>
        </div>
      )}

      {item.reason === 'logged_out' && (
        <p className="text-sm text-gray-600 dark:text-gray-300">
          The grok.com session behind the bridge is logged out.{' '}
          {item.bridge_login_url ? (
            <a href={item.bridge_login_url} target="_blank" rel="noopener noreferrer"
               className="inline-flex items-center text-indigo-500 hover:underline">
              Open bridge login <ExternalLink className="h-3.5 w-3.5 ml-1" />
            </a>
          ) : 'Open the bridge /login page and sign in again.'}
        </p>
      )}

      {msg && <p className="text-xs text-red-400 bg-red-900/10 rounded p-2">{msg}</p>}
    </div>
  )
}

export function AttentionPanel({ providers }: { providers?: Provider[] }) {
  const { data } = useQuery({
    queryKey: ['provider-attention'],
    queryFn: providersApi.attention,
    refetchInterval: 20_000,
  })
  const items = data?.items ?? []
  if (items.length === 0) return null
  return (
    <Card>
      <CardContent>
        <div className="space-y-3">
          <div className="flex items-center gap-2">
            <AlertTriangle className="h-4 w-4 text-amber-500" />
            <h2 className="text-sm font-semibold text-gray-900 dark:text-gray-100">
              Needs attention ({items.length})
            </h2>
          </div>
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Breaker state is per node: a reset here applies to this node only; the peer re-probes on its own.
          </p>
          {items.map(it => (
            <Row key={it.provider_id} item={it} provider={providers?.find(p => p.id === it.provider_id)} />
          ))}
        </div>
      </CardContent>
    </Card>
  )
}
