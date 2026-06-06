import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldAlert, ShieldCheck, Server } from 'lucide-react'
import { clusterApi, providersApi, type ClusterPeerRow } from '@/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { CircuitBreakerBadge } from '@/components/providers/CircuitBreakerBadge'
import { useToast } from '@/components/ui/Toast'
import { useAuth } from '@/context/AuthContext'
import { formatTimeForUser } from '@/utils/time'

export function ClusterPage() {
  const qc = useQueryClient()
  const toast = useToast()
  const { user } = useAuth()

  const { data: health, isLoading: healthLoading } = useQuery({
    queryKey: ['health'],
    queryFn: clusterApi.health,
    refetchInterval: 10_000,
  })

  const { data: cluster, isLoading: clusterLoading } = useQuery({
    queryKey: ['cluster'],
    queryFn: clusterApi.status,
    refetchInterval: 15_000,
  })

  const { data: providers } = useQuery({
    queryKey: ['providers'],
    queryFn: providersApi.list,
  })

  const syncMutation = useMutation({
    mutationFn: clusterApi.sync,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['cluster'] }); toast.success('Sync initiated') },
    onError: (e: Error) => toast.error(e.message),
  })

  const cbMutation = useMutation({
    mutationFn: ({ id, action }: { id: string; action: 'open' | 'close' }) =>
      clusterApi.forceCircuitBreaker(id, action),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['health'] }); toast.success('Circuit breaker updated') },
    onError: (e: Error) => toast.error(e.message),
  })

  const cbs = Object.entries(health?.circuitBreakers ?? {})
  const providerMap = Object.fromEntries((providers ?? []).map(p => [p.id, p.name]))

  const allNodes = cluster ? [
    { ...cluster.local_node, isLocal: true },
    ...cluster.peers.map(p => ({ ...p, isLocal: false })),
  ] : []

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Cluster</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Multi-node status and circuit breakers</p>
        </div>
        <Button size="sm" variant="outline" onClick={() => syncMutation.mutate()} loading={syncMutation.isPending}>
          <RefreshCw className="h-4 w-4 mr-1.5" />Sync Now
        </Button>
      </div>

      {/* Cluster nodes */}
      <Card>
        <CardHeader>
          <CardTitle>
            Cluster Nodes
            {cluster && (
              <span className="ml-2 text-sm font-normal text-gray-500 dark:text-gray-400">
                {cluster.healthy_nodes}/{cluster.total_nodes} healthy
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {clusterLoading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : !cluster?.cluster_enabled ? (
            <div className="text-center py-6">
              <Server className="h-8 w-8 text-gray-400 mx-auto mb-2" />
              <p className="text-sm text-gray-500 dark:text-gray-400">Cluster mode not enabled</p>
              <p className="text-xs text-gray-400 mt-1">Set CLUSTER_ENABLED=true and CLUSTER_PEERS in your environment</p>
            </div>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {allNodes.map(node => {
                const online = node.status === 'healthy'
                return (
                  <div key={node.id} className="flex items-center gap-3 py-3">
                    <div className={`h-2 w-2 rounded-full shrink-0 ${online ? 'bg-green-500' : 'bg-red-500'}`} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <p className="font-medium text-gray-900 dark:text-gray-100">{node.name || node.id}</p>
                        {'isLocal' in node && node.isLocal && (
                          <span className="text-xs bg-indigo-100 dark:bg-indigo-900 text-indigo-700 dark:text-indigo-300 px-1.5 py-0.5 rounded">this node</span>
                        )}
                      </div>
                      <p className="text-xs text-gray-500 dark:text-gray-400">{node.url}</p>
                      {'healthy_providers' in node && node.healthy_providers != null && (
                        <p className="text-xs text-gray-400">{node.healthy_providers}/{node.total_providers} providers healthy</p>
                      )}
                    </div>
                    <Badge variant={online ? 'success' : 'danger'}>{online ? 'Online' : node.status}</Badge>
                    {'last_heartbeat' in node && node.last_heartbeat ? (
                      <span className="text-xs text-gray-400">
                        {formatTimeForUser(node.last_heartbeat * 1000, user, 'time')}
                      </span>
                    ) : null}
                    {'latency_ms' in node && node.latency_ms ? (
                      <span className="text-xs text-gray-400">{Math.round(node.latency_ms)}ms</span>
                    ) : null}
                  </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* v5.0.18 — UI-configurable cluster peers */}
      <ClusterPeersPanel />

      {/* Circuit Breakers */}
      <Card>
        <CardHeader><CardTitle>Provider Circuit Breakers</CardTitle></CardHeader>
        <CardContent>
          {healthLoading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : cbs.length === 0 ? (
            <p className="text-sm text-gray-500 dark:text-gray-400 text-center py-6">No providers configured</p>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {cbs.map(([providerId, cb]) => (
                <div key={providerId} className="flex items-center gap-3 py-3">
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-gray-900 dark:text-gray-100">
                      {providerMap[providerId] || providerId}
                    </p>
                    <p className="text-xs text-gray-400">{providerId}</p>
                    {cb.hold_down_remaining > 0 && (
                      <p className="text-xs text-amber-500">Hold-down: {Math.ceil(cb.hold_down_remaining)}s remaining</p>
                    )}
                    {cb.failures > 0 && (
                      <p className="text-xs text-red-400">{cb.failures} failure{cb.failures !== 1 ? 's' : ''} recorded</p>
                    )}
                  </div>
                  <CircuitBreakerBadge state={cb.state as 'closed' | 'open' | 'half-open'} />
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => cbMutation.mutate({ id: providerId, action: 'close' })}
                      disabled={cb.state === 'closed'}
                    >
                      <ShieldCheck className="h-3.5 w-3.5 mr-1" />Force Online
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => cbMutation.mutate({ id: providerId, action: 'open' })}
                      disabled={cb.state === 'open'}
                    >
                      <ShieldAlert className="h-3.5 w-3.5 mr-1" />Force Trip
                    </Button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}


// ── v5.0.18: UI-configurable cluster peers panel ───────────────────────
function ClusterPeersPanel() {
  const qc = useQueryClient()
  const toast = useToast()
  const { data: peers, isLoading } = useQuery({
    queryKey: ['cluster-peers'],
    queryFn: clusterApi.listPeers,
    refetchInterval: 30_000,
  })
  const addMut = useMutation({
    mutationFn: clusterApi.addPeer,
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['cluster-peers'] }); toast.success('Peer added') },
    onError:   (e: Error) => toast.error(e.message),
  })
  const removeMut = useMutation({
    mutationFn: (id: string) => clusterApi.removePeer(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['cluster-peers'] }); toast.success('Peer removed') },
    onError:   (e: Error) => toast.error(e.message),
  })

  const [id, setId] = useState('')
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  // v5.0.25 / Batch 4 (BUG-063) — replace browser confirm() with the
  // app's ConfirmDialog component (autofocuses the action button +
  // listens for Enter/Space — UX consistent with the other delete
  // confirmations across the app, and Playwright-testable).
  const [removeTarget, setRemoveTarget] = useState<ClusterPeerRow | null>(null)

  function submit() {
    const trim = (s: string) => s.trim()
    if (!trim(id) || !trim(url)) {
      toast.error('id + url are required')
      return
    }
    if (!/^https?:\/\//.test(trim(url))) {
      toast.error('url must include scheme (http:// or https://)')
      return
    }
    addMut.mutate({ id: trim(id), url: trim(url), name: trim(name) || undefined })
    setId(''); setUrl(''); setName('')
  }

  const active = (peers ?? []).filter(p => p.active)
  const removed = (peers ?? []).filter(p => !p.active)

  return (
    <Card>
      <CardHeader>
        <CardTitle>
          Cluster Peers
          <span className="ml-2 text-sm font-normal text-gray-500 dark:text-gray-400">
            {active.length} active{removed.length > 0 ? ` · ${removed.length} removed` : ''}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="flex justify-center py-6"><Spinner /></div>
        ) : (
          <div className="space-y-4">
            {/* Add row */}
            <div className="border border-gray-200 dark:border-gray-700 rounded-lg p-3 bg-gray-50 dark:bg-gray-800/50">
              <p className="text-xs text-gray-500 dark:text-gray-400 mb-2">
                Add peer: enter the remote node's CLUSTER_NODE_ID and its public URL (no trailing slash).
                Replicates to existing peers within ~60s.
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-12 gap-2">
                <input
                  className="sm:col-span-3 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
                  placeholder="id (e.g. llm-proxy2-www3)"
                  value={id}
                  onChange={e => setId(e.target.value)}
                />
                <input
                  className="sm:col-span-6 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600 font-mono"
                  placeholder="https://node3.example.com/llm-proxy2"
                  value={url}
                  onChange={e => setUrl(e.target.value)}
                />
                <input
                  className="sm:col-span-2 px-2.5 py-1.5 text-sm rounded border bg-white dark:bg-gray-800 border-gray-300 dark:border-gray-600"
                  placeholder="name (optional)"
                  value={name}
                  onChange={e => setName(e.target.value)}
                />
                <Button
                  size="sm"
                  className="sm:col-span-1"
                  onClick={submit}
                  loading={addMut.isPending}
                  disabled={!id.trim() || !url.trim()}
                >
                  Add
                </Button>
              </div>
            </div>

            {/* Active peers */}
            {active.length === 0 ? (
              <p className="text-sm text-gray-500 dark:text-gray-400 px-1">No active peers configured.</p>
            ) : (
              <div className="space-y-1.5">
                {active.map(p => (
                  <div key={p.id} className="flex items-center justify-between gap-3 px-3 py-2 rounded border border-gray-200 dark:border-gray-700">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                        {p.name || p.id}
                        {p.name && p.name !== p.id && (
                          <span className="ml-1.5 text-xs text-gray-500 dark:text-gray-400">({p.id})</span>
                        )}
                      </div>
                      <div className="text-xs text-gray-500 dark:text-gray-400 font-mono truncate">{p.url}</div>
                    </div>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => setRemoveTarget(p)}
                      loading={removeMut.isPending && removeTarget?.id === p.id}
                    >
                      Remove
                    </Button>
                  </div>
                ))}
              </div>
            )}

            {/* Removed (tombstones) */}
            {removed.length > 0 && (
              <details className="text-xs text-gray-500 dark:text-gray-400">
                <summary className="cursor-pointer mb-1.5 hover:text-gray-700 dark:hover:text-gray-200">
                  Recently removed ({removed.length})
                </summary>
                <div className="space-y-1 pl-3">
                  {removed.map(p => (
                    <div key={p.id} className="flex items-center justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <span className="font-mono">{p.id}</span>
                        <span className="ml-2 text-[0.7rem]">removed {p.removed_at}</span>
                      </div>
                      <button
                        className="text-indigo-600 dark:text-indigo-400 hover:underline"
                        onClick={() => addMut.mutate({ id: p.id, url: p.url, name: p.name || undefined })}
                      >
                        Restore
                      </button>
                    </div>
                  ))}
                </div>
              </details>
            )}
          </div>
        )}
      </CardContent>

      {/* v5.0.25 / Batch 4 (BUG-063) — ConfirmDialog replaces browser confirm() */}
      <ConfirmDialog
        open={removeTarget !== null}
        title={`Remove peer ${removeTarget?.id ?? ''}?`}
        message={
          removeTarget
            ? `The local node will stop syncing to ${removeTarget.url} within 30s. The tombstone replicates to all peers, so they will also drop ${removeTarget.id}.`
            : ''
        }
        confirmLabel="Remove"
        variant="danger"
        loading={removeMut.isPending}
        onConfirm={() => {
          if (removeTarget) removeMut.mutate(removeTarget.id)
          setRemoveTarget(null)
        }}
        onCancel={() => setRemoveTarget(null)}
      />
    </Card>
  )
}
