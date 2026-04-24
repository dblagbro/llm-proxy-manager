import { useState, useEffect } from 'react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { RefreshCw, Trash2, Play, Pause, Download } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import {
  oauthCaptureApi,
  type OAuthCaptureProfile,
  type OAuthCaptureLogEntry,
} from '@/api'

export function LiveCaptureTail({ profile }: { profile: OAuthCaptureProfile }) {
  const toast = useToast()
  const [entries, setEntries] = useState<OAuthCaptureLogEntry[]>([])
  const [streaming, setStreaming] = useState(false)

  const { data: initial, refetch } = useQuery<OAuthCaptureLogEntry[]>({
    queryKey: ['oauth-capture', 'log', profile.name],
    queryFn: () => oauthCaptureApi.listLog(profile.name, 50),
    enabled: !!profile.name,
  })

  useEffect(() => {
    if (initial) setEntries(initial)
  }, [initial])

  // SSE stream
  useEffect(() => {
    if (!streaming || !profile.enabled) return
    const url = `${import.meta.env.BASE_URL.replace(/\/$/, '')}${oauthCaptureApi.streamUrl(profile.name)}`
    const es = new EventSource(url, { withCredentials: true })
    es.onmessage = (msg) => {
      try {
        const entry: OAuthCaptureLogEntry = JSON.parse(msg.data)
        setEntries(prev => [entry, ...prev].slice(0, 200))
      } catch { /* ignore non-JSON pings */ }
    }
    es.onerror = () => {
      setStreaming(false)
      es.close()
    }
    return () => es.close()
  }, [streaming, profile.name, profile.enabled])

  const clearMut = useMutation({
    mutationFn: () => oauthCaptureApi.clearLog(profile.name),
    onSuccess: () => {
      setEntries([])
      toast.success('Log cleared')
    },
  })

  const exportUrl = `${import.meta.env.BASE_URL.replace(/\/$/, '')}${oauthCaptureApi.exportUrl(profile.name)}`

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-gray-800 dark:text-gray-200">
          Live captures ({entries.length})
        </div>
        <div className="flex items-center gap-1.5">
          <Button onClick={() => setStreaming(!streaming)} variant="secondary" size="sm">
            {streaming ? <><Pause className="h-3.5 w-3.5 mr-1" />Stop tail</> : <><Play className="h-3.5 w-3.5 mr-1" />Start tail</>}
          </Button>
          <Button onClick={() => refetch()} variant="secondary" size="sm">
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <a href={exportUrl} download={`captures-${profile.name}.ndjson`}>
            <Button variant="secondary" size="sm">
              <Download className="h-3.5 w-3.5 mr-1" /> NDJSON
            </Button>
          </a>
          <Button
            onClick={() => {
              if (confirm(`Clear all captures for ${profile.name}?`)) clearMut.mutate()
            }}
            variant="danger"
            size="sm"
            loading={clearMut.isPending}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className="border border-gray-200 dark:border-gray-700 rounded max-h-[400px] overflow-y-auto">
        {entries.length === 0 ? (
          <div className="p-4 text-center text-sm text-gray-400">
            No captures yet. Start the tail, then run your CLI.
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="bg-gray-50 dark:bg-gray-800 text-left">
              <tr>
                <th className="px-2 py-1 font-medium">Time</th>
                <th className="px-2 py-1 font-medium">Method</th>
                <th className="px-2 py-1 font-medium">Path</th>
                <th className="px-2 py-1 font-medium">Status</th>
                <th className="px-2 py-1 font-medium">Latency</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-t border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="px-2 py-1 font-mono">{e.created_at?.slice(11, 19)}</td>
                  <td className="px-2 py-1 font-mono">{e.method}</td>
                  <td className="px-2 py-1 font-mono truncate max-w-[280px]">{e.path}</td>
                  <td className={
                    'px-2 py-1 font-mono ' +
                    (e.resp_status && e.resp_status < 400 ? 'text-green-600' :
                     e.resp_status && e.resp_status >= 400 ? 'text-red-600' : 'text-gray-400')
                  }>
                    {e.resp_status ?? (e.error ? 'err' : '—')}
                  </td>
                  <td className="px-2 py-1 font-mono text-gray-500">{Math.round(e.latency_ms)}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
