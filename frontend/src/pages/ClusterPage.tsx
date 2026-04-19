import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, ShieldAlert, ShieldCheck } from 'lucide-react'
import { clusterApi } from '@/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { CircuitBreakerBadge } from '@/components/providers/CircuitBreakerBadge'
import { useToast } from '@/components/ui/Toast'

export function ClusterPage() {
  const qc = useQueryClient()
  const toast = useToast()

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
        <CardHeader><CardTitle>Cluster Nodes</CardTitle></CardHeader>
        <CardContent>
          {clusterLoading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : !cluster || cluster.peers.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-6">Single-node mode — no peer nodes configured</p>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {cluster.peers.map(node => {
                const online = node.status === 'healthy'
                return (
                <div key={node.id} className="flex items-center gap-3 py-3">
                  <div className={`h-2 w-2 rounded-full shrink-0 ${online ? 'bg-green-500' : 'bg-red-500'}`} />
                  <div className="flex-1 min-w-0">
                    <p className="font-medium text-gray-900 dark:text-gray-100">{node.name}</p>
                    <p className="text-xs text-gray-500">{node.url}</p>
                  </div>
                  <Badge variant={online ? 'success' : 'danger'}>{online ? 'Online' : node.status}</Badge>
                  {node.last_heartbeat && (
                    <span className="text-xs text-gray-400">
                      {new Date(node.last_heartbeat * 1000).toLocaleTimeString()}
                    </span>
                  )}
                </div>
                )
              })}
            </div>
          )}
        </CardContent>
      </Card>

      {/* Circuit Breakers */}
      <Card>
        <CardHeader><CardTitle>Circuit Breakers</CardTitle></CardHeader>
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
                    <p className="font-medium text-gray-900 dark:text-gray-100">{providerId}</p>
                    {cb.hold_down_remaining > 0 && (
                      <p className="text-xs text-amber-500">Hold-down: {cb.hold_down_remaining}s remaining</p>
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
                      <ShieldCheck className="h-3.5 w-3.5 mr-1" />Force Close
                    </Button>
                    <Button
                      size="sm"
                      variant="danger"
                      onClick={() => cbMutation.mutate({ id: providerId, action: 'open' })}
                      disabled={cb.state === 'open'}
                    >
                      <ShieldAlert className="h-3.5 w-3.5 mr-1" />Force Open
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
