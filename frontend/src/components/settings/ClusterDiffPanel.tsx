import { useMutation } from '@tanstack/react-query'
import { RefreshCw, CheckCircle, AlertTriangle, WifiOff } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { settingsApi } from '@/api'

export function ClusterDiffPanel() {
  const diffMut = useMutation({ mutationFn: () => settingsApi.clusterDiff() })

  const result = diffMut.data
  if (result && !result.cluster_enabled) return null

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>Cluster Settings Sync</CardTitle>
          <Button
            variant="outline"
            size="sm"
            onClick={() => diffMut.mutate()}
            loading={diffMut.isPending}
          >
            <RefreshCw className="h-3.5 w-3.5 mr-1.5" />Check Nodes
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {!result && !diffMut.isPending && (
          <p className="text-xs text-gray-400">
            Click "Check Nodes" to compare settings across cluster nodes.
          </p>
        )}
        {result && result.cluster_enabled && (
          <div className="space-y-3">
            {result.all_synced ? (
              <div className="flex items-center gap-2 text-sm text-green-600 dark:text-green-400">
                <CheckCircle className="h-4 w-4" />
                All nodes are in sync
              </div>
            ) : (
              <div className="flex items-center gap-2 text-sm text-amber-600 dark:text-amber-400">
                <AlertTriangle className="h-4 w-4" />
                Settings differ across nodes
              </div>
            )}
            <div className="space-y-2">
              {result.peers?.map(peer => (
                <div key={peer.id} className="rounded border border-gray-200 dark:border-gray-700 p-3">
                  <div className="flex items-center gap-2 mb-1">
                    {peer.status === 'unreachable' || peer.status === 'error'
                      ? <WifiOff className="h-3.5 w-3.5 text-red-500" />
                      : peer.diffs.length === 0
                        ? <CheckCircle className="h-3.5 w-3.5 text-green-500" />
                        : <AlertTriangle className="h-3.5 w-3.5 text-amber-500" />
                    }
                    <span className="text-sm font-medium text-gray-800 dark:text-gray-200">
                      {peer.name || peer.id}
                    </span>
                    <span className={`text-xs ml-auto ${
                      peer.status === 'healthy' ? 'text-green-500'
                      : peer.status === 'unreachable' || peer.status === 'error' ? 'text-red-500'
                      : 'text-gray-400'
                    }`}>{peer.status}</span>
                  </div>
                  {peer.diffs.length > 0 && (
                    <div className="mt-2 space-y-1">
                      {peer.diffs.map(key => (
                        <div key={key} className="flex items-center gap-3 text-xs font-mono">
                          <span className="text-gray-500 w-48 truncate">{key}</span>
                          <span className="text-green-600 dark:text-green-400 truncate">
                            local: {String(result.local?.settings[key] ?? '—')}
                          </span>
                          <span className="text-amber-600 dark:text-amber-400 truncate">
                            peer: {String(peer.settings?.[key] ?? '—')}
                          </span>
                        </div>
                      ))}
                    </div>
                  )}
                  {(peer.status === 'unreachable' || peer.status === 'error') && (
                    <p className="text-xs text-red-400 mt-1">{peer.error ?? 'Node unreachable'}</p>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  )
}
