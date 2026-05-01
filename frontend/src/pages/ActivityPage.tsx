import { useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { RefreshCw, Search, X } from 'lucide-react'
import { monitoringApi } from '@/api'
import { getBasePath } from '@/lib/basePath'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { Button } from '@/components/ui/Button'
import { ActivityEventRow } from '@/components/activity/ActivityEventRow'
import type { ActivityEvent } from '@/types'

const PAGE_SIZE = 200

const SEVERITY_OPTS: { value: string; label: string }[] = [
  { value: '',                    label: 'All severities' },
  { value: 'info',                label: 'Info' },
  { value: 'warning',             label: 'Warning' },
  { value: 'error',               label: 'Error' },
  { value: 'critical',            label: 'Critical' },
  { value: 'warning,error,critical', label: 'Non-info only' },
]

export function ActivityPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const providerFilter = searchParams.get('provider') ?? ''
  const [searchInput, setSearchInput] = useState('')
  const [search, setSearch] = useState('')
  const [severity, setSeverity] = useState<string>('')
  const [livePaused, setLivePaused] = useState(false)

  // History — paged. We keep ALL pages we've fetched in `events`.
  const [events, setEvents] = useState<ActivityEvent[]>([])
  const [oldestId, setOldestId] = useState<number | null>(null)
  const [hasMore, setHasMore] = useState(true)
  const [loadingPage, setLoadingPage] = useState(false)
  const [refreshKey, setRefreshKey] = useState(0)

  // Live tail (only active when no search/filter and not paused)
  const esRef = useRef<EventSource | null>(null)
  const [connected, setConnected] = useState(false)
  const liveActive = !search && !severity && !livePaused

  // Total count for the current filter
  const { data: countData } = useQuery({
    queryKey: ['activity-count', providerFilter, severity, search],
    queryFn: () => monitoringApi.activityCount({
      provider_id: providerFilter || null,
      severity: severity || null,
      search: search || null,
    }),
    refetchInterval: 30_000,
    staleTime: 10_000,
  })

  // Initial load + reset whenever filters change
  useEffect(() => {
    let cancel = false
    setLoadingPage(true)
    setEvents([])
    setOldestId(null)
    setHasMore(true)
    monitoringApi.activity({
      limit: PAGE_SIZE,
      provider_id: providerFilter || null,
      severity: severity || null,
      search: search || null,
    }).then(rows => {
      if (cancel) return
      setEvents(rows)
      setOldestId(rows.length ? rows[rows.length - 1].id : null)
      setHasMore(rows.length === PAGE_SIZE)
    }).finally(() => { if (!cancel) setLoadingPage(false) })
    return () => { cancel = true }
  }, [providerFilter, severity, search, refreshKey])

  // Live SSE — only when no filter and not paused
  useEffect(() => {
    if (!liveActive) {
      if (esRef.current) { esRef.current.close(); esRef.current = null }
      setConnected(false)
      return
    }
    // v3.0.16: runtime base-path detection (mount-point agnostic).
    const base = getBasePath()
    const es = new EventSource(`${base}/api/monitoring/activity/stream`, { withCredentials: true })
    esRef.current = es
    es.addEventListener('open', () => setConnected(true))
    es.addEventListener('error', () => setConnected(false))
    es.addEventListener('activity', (e) => {
      try {
        const event: ActivityEvent = JSON.parse((e as MessageEvent).data)
        // De-dupe by id; prepend
        setEvents(prev => prev.some(p => p.id === event.id) ? prev : [event, ...prev])
      } catch { /* ignore */ }
    })
    return () => { es.close(); esRef.current = null }
  }, [liveActive])

  async function loadOlder() {
    if (!oldestId || !hasMore || loadingPage) return
    setLoadingPage(true)
    try {
      const rows = await monitoringApi.activity({
        limit: PAGE_SIZE,
        before_id: oldestId,
        provider_id: providerFilter || null,
        severity: severity || null,
        search: search || null,
      })
      if (rows.length) {
        setEvents(prev => [...prev, ...rows])
        setOldestId(rows[rows.length - 1].id)
      }
      setHasMore(rows.length === PAGE_SIZE)
    } finally {
      setLoadingPage(false)
    }
  }

  function applySearch() {
    setSearch(searchInput.trim())
  }

  function clearAllFilters() {
    setSearchInput('')
    setSearch('')
    setSeverity('')
    setSearchParams({})
  }

  const total = countData?.total
  const showing = events.length

  // Memoized de-duped + sorted view (newest first by id desc)
  const orderedEvents = useMemo(() => {
    const seen = new Set<number>()
    const out: ActivityEvent[] = []
    for (const e of events) {
      if (!seen.has(e.id)) { seen.add(e.id); out.push(e) }
    }
    out.sort((a, b) => b.id - a.id)
    return out
  }, [events])

  return (
    <div className="p-6 space-y-4 max-w-5xl">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Activity Log</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            {providerFilter && <span>Provider <span className="font-mono">{providerFilter}</span> · </span>}
            {showing > 0 ? (
              <>
                Showing <span className="font-mono">{showing.toLocaleString()}</span>
                {total != null && total > showing && (
                  <> of <span className="font-mono">{total.toLocaleString()}</span></>
                )}
                {liveActive && <span> · live tail</span>}
              </>
            ) : 'Loading…'}
          </p>
        </div>
        <div className="flex items-center gap-2">
          {liveActive ? (
            <Badge variant={connected ? 'success' : 'muted'}>
              <span className={`inline-block h-1.5 w-1.5 rounded-full mr-1.5 ${connected ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`} />
              {connected ? 'Live' : 'Connecting…'}
            </Badge>
          ) : (
            <Badge variant="muted">Live paused</Badge>
          )}
          <Button size="sm" variant="outline" onClick={() => setRefreshKey(k => k + 1)}
                  loading={loadingPage} title="Re-fetch from server">
            <RefreshCw className="h-3.5 w-3.5 mr-1" />Refresh
          </Button>
        </div>
      </div>

      {/* Filter bar */}
      <Card>
        <CardContent className="p-3">
          <div className="flex items-center gap-2 flex-wrap">
            <div className="relative flex-1 min-w-[260px]">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-gray-400" />
              <input
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') applySearch() }}
                placeholder="Search messages, providers, request bodies, errors…"
                className="w-full pl-9 pr-9 py-2 text-sm bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 placeholder:text-gray-400 dark:placeholder:text-gray-500 border border-gray-200 dark:border-gray-700 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
              />
              {searchInput && (
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600"
                  onClick={() => { setSearchInput(''); setSearch('') }}
                  title="Clear search"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
            <Button size="sm" variant="primary" onClick={applySearch}>Search</Button>
            <select
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
              className="px-3 py-2 bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              {SEVERITY_OPTS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
            </select>
            {liveActive ? (
              <Button size="sm" variant="ghost" onClick={() => setLivePaused(true)}>Pause live</Button>
            ) : !search && !severity && (
              <Button size="sm" variant="ghost" onClick={() => setLivePaused(false)}>Resume live</Button>
            )}
            {(providerFilter || search || severity) && (
              <Button size="sm" variant="ghost" onClick={clearAllFilters}>Clear all</Button>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardContent className="p-0">
          {loadingPage && events.length === 0 ? (
            <div className="flex justify-center py-16"><Spinner /></div>
          ) : orderedEvents.length === 0 ? (
            <p className="text-center text-gray-500 py-12">
              No activity{search ? ' matching your search' : providerFilter ? ' for this provider' : ' yet'}
            </p>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700 max-h-[75vh] overflow-y-auto">
              {orderedEvents.map(e => <ActivityEventRow key={e.id} event={e} />)}
              <div className="px-4 py-3 text-center">
                {hasMore ? (
                  <Button size="sm" variant="outline" onClick={loadOlder} loading={loadingPage}>
                    Load older ({PAGE_SIZE} more)
                  </Button>
                ) : (
                  <p className="text-xs text-gray-400">— end of log —</p>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
