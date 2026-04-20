import { useEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { monitoringApi } from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { ActivityEventRow } from '@/components/activity/ActivityEventRow'
import type { ActivityEvent } from '@/types'

export function ActivityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const providerFilter = searchParams.get('provider') ?? ''
  const [live, setLive] = useState<ActivityEvent[]>([])
  const [connected, setConnected] = useState(false)
  const esRef = useRef<EventSource | null>(null)

  const { data: history, isLoading } = useQuery({
    queryKey: ['activity-history', providerFilter],
    queryFn: () => monitoringApi.activity(200),
  })

  useEffect(() => {
    const base = import.meta.env.BASE_URL.replace(/\/$/, '')
    const es = new EventSource(`${base}/api/monitoring/activity/stream`, { withCredentials: true })
    esRef.current = es

    es.addEventListener('open', () => setConnected(true))
    es.addEventListener('error', () => setConnected(false))
    es.addEventListener('activity', (e) => {
      try {
        const event: ActivityEvent = JSON.parse((e as MessageEvent).data)
        setLive(prev => [event, ...prev].slice(0, 200))
      } catch { /* ignore */ }
    })

    return () => { es.close(); esRef.current = null }
  }, [])

  const allEvents = live.length > 0 ? live : history ?? []
  const events = providerFilter
    ? allEvents.filter(e => e.provider_id === providerFilter)
    : allEvents

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Activity Log</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {providerFilter ? `Filtered to provider ${providerFilter}` : 'Real-time event stream'}
          </p>
        </div>
        <div className="flex items-center gap-3">
          {providerFilter && (
            <button
              onClick={() => setSearchParams({})}
              className="text-xs text-indigo-500 hover:text-indigo-400 underline"
            >
              Clear filter
            </button>
          )}
          <Badge variant={connected ? 'success' : 'muted'}>
            <span className={`inline-block h-1.5 w-1.5 rounded-full mr-1.5 ${connected ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`} />
            {connected ? 'Live' : 'Connecting…'}
          </Badge>
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {isLoading && live.length === 0 ? (
            <div className="flex justify-center py-16"><Spinner /></div>
          ) : events.length === 0 ? (
            <p className="text-center text-gray-500 py-12">No activity{providerFilter ? ' for this provider' : ' yet'}</p>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700 max-h-[70vh] overflow-y-auto">
              {events.map(e => <ActivityEventRow key={e.id} event={e} />)}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
