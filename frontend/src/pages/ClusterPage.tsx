import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldAlert, ShieldCheck, Server } from 'lucide-react'
import { clusterApi, providersApi } from '@/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
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
          <p className="text-sm text-gray-500 mt-0.5">Multi-node status and circuit breakers</p>
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
              <span className="ml-2 text-sm font-normal text-gray-500">
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
              <p className="text-sm text-gray-500">Cluster mode not enabled</p>
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
                      <p className="text-xs text-gray-500">{node.url}</p>
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

      {/* Circuit Breakers */}
      <Card>
        <CardHeader><CardTitle>Provider Circuit Breakers</CardTitle></CardHeader>
        <CardContent>
          {healthLoading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : cbs.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-6">No providers configured</p>
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
