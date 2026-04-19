import { useQuery } from '@tanstack/react-query'
import { Server, Activity, DollarSign, Zap, AlertTriangle, Clock } from 'lucide-react'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from 'recharts'
import { clusterApi, monitoringApi, providersApi } from '@/api'
import { StatCard } from '@/components/ui/StatCard'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { ActivityEventRow } from '@/components/activity/ActivityEventRow'
import { CircuitBreakerBadge } from '@/components/providers/CircuitBreakerBadge'
import type { Provider } from '@/types'

function fmtCost(v: number) {
  if (v === 0) return '$0.00'
  if (v < 0.01) return `$${v.toFixed(5)}`
  return `$${v.toFixed(3)}`
}

function fmtLatency(ms: number) {
  if (!ms) return '—'
  return ms > 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

export function DashboardPage() {
  const { data: health } = useQuery({ queryKey: ['health'], queryFn: clusterApi.health, refetchInterval: 15_000 })
  const { data: metrics } = useQuery({ queryKey: ['metrics', 24], queryFn: () => monitoringApi.metrics(24), refetchInterval: 60_000 })
  const { data: providers } = useQuery({ queryKey: ['providers'], queryFn: providersApi.list, refetchInterval: 30_000 })
  const { data: activity } = useQuery({ queryKey: ['activity'], queryFn: () => monitoringApi.activity(20), refetchInterval: 10_000 })
  const { data: extStatus } = useQuery({ queryKey: ['status-pages'], queryFn: monitoringApi.statusPages, refetchInterval: 300_000 })

  const totalRequests = metrics?.providers.reduce((s, p) => s + p.requests, 0) ?? 0
  const totalCost = metrics?.providers.reduce((s, p) => s + p.total_cost_usd, 0) ?? 0
  const avgLatency = metrics?.providers.length
    ? metrics.providers.reduce((s, p) => s + p.avg_latency_ms, 0) / metrics.providers.length
    : 0
  const openCBs = Object.values(health?.circuitBreakers ?? {}).filter(cb => cb.state === 'open').length

  // Build chart data from metrics buckets (last 6h from any provider)
  const chartData = buildChartData(metrics?.providers ?? [])


  return (
    <div className="p-6 space-y-6 max-w-7xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Dashboard</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Last 24 hours</p>
      </div>

      {/* Stat cards */}
      <div className="grid grid-cols-2 lg:grid-cols-3 xl:grid-cols-6 gap-4">
        <StatCard
          label="Providers"
          value={`${health?.healthyProviders ?? '—'}/${health?.totalProviders ?? '—'}`}
          sub="healthy"
          icon={<Server className="h-5 w-5" />}
          variant={openCBs > 0 ? 'warning' : 'success'}
        />
        <StatCard
          label="Requests"
          value={totalRequests.toLocaleString()}
          sub="24h"
          icon={<Zap className="h-5 w-5" />}
        />
        <StatCard
          label="Cost Today"
          value={fmtCost(totalCost)}
          sub="USD"
          icon={<DollarSign className="h-5 w-5" />}
        />
        <StatCard
          label="Avg Latency"
          value={fmtLatency(avgLatency)}
          sub="per request"
          icon={<Clock className="h-5 w-5" />}
        />
        <StatCard
          label="Circuit Breakers"
          value={openCBs}
          sub={openCBs === 0 ? 'all closed' : 'open'}
          icon={<AlertTriangle className="h-5 w-5" />}
          variant={openCBs > 0 ? 'danger' : 'default'}
        />
        <StatCard
          label="Activity"
          value={activity?.length ?? 0}
          sub="recent events"
          icon={<Activity className="h-5 w-5" />}
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        {/* Provider status strip */}
        <Card className="xl:col-span-2">
          <CardHeader>
            <CardTitle>Provider Status</CardTitle>
          </CardHeader>
          <CardContent className="p-0">
            {!providers ? (
              <div className="flex items-center justify-center py-12"><Spinner /></div>
            ) : providers.length === 0 ? (
              <p className="text-sm text-gray-500 text-center py-8">No providers configured</p>
            ) : (
              <div className="divide-y divide-gray-100 dark:divide-gray-700">
                {providers.map(p => (
                  <ProviderStatusRow
                    key={p.id}
                    provider={p}
                    cb={health?.circuitBreakers?.[p.id]}
                    summary={metrics?.providers.find(m => m.provider_id === p.id)}
                  />
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* External status */}
        <Card>
          <CardHeader><CardTitle>External Status</CardTitle></CardHeader>
          <CardContent className="space-y-3">
            {(['anthropic', 'openai', 'google'] as const).map(key => {
              const s = extStatus?.[key]
              return (
                <div key={key} className="flex items-center justify-between">
                  <span className="text-sm font-medium text-gray-700 dark:text-gray-300 capitalize">{key}</span>
                  <Badge variant={!s ? 'muted' : s.degraded ? 'danger' : 'success'}>
                    {!s ? 'Checking…' : s.degraded ? 'Degraded' : 'Operational'}
                  </Badge>
                </div>
              )
            })}
          </CardContent>
        </Card>
      </div>

      {/* Chart */}
      {chartData.length > 0 && (
        <Card>
          <CardHeader><CardTitle>Request Volume — 24h</CardTitle></CardHeader>
          <CardContent>
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={chartData}>
                <defs>
                  <linearGradient id="reqGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="#6366f1" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="#6366f1" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" opacity={0.4} />
                <XAxis dataKey="time" tick={{ fontSize: 11, fill: '#9ca3af' }} />
                <YAxis tick={{ fontSize: 11, fill: '#9ca3af' }} />
                <Tooltip
                  contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
                  labelStyle={{ color: '#e5e7eb' }}
                />
                <Area type="monotone" dataKey="requests" stroke="#6366f1" fill="url(#reqGrad)" strokeWidth={2} dot={false} />
              </AreaChart>
            </ResponsiveContainer>
          </CardContent>
        </Card>
      )}

      {/* Recent activity */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Recent Activity</CardTitle>
          <a href="/activity" className="text-xs text-indigo-500 hover:underline">View all →</a>
        </CardHeader>
        <CardContent className="p-0">
          {!activity ? (
            <div className="flex items-center justify-center py-8"><Spinner /></div>
          ) : activity.length === 0 ? (
            <p className="text-sm text-gray-500 text-center py-8">No recent activity</p>
          ) : (
            <div className="divide-y divide-gray-100 dark:divide-gray-700">
              {activity.slice(0, 10).map(e => <ActivityEventRow key={e.id} event={e} compact />)}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function ProviderStatusRow({ provider, cb, summary }: {
  provider: Provider
  cb?: { state: string; hold_down_remaining: number }
  summary?: { requests: number; success_rate: number; avg_latency_ms: number; total_cost_usd: number }
}) {
  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <div className={`h-2 w-2 rounded-full shrink-0 ${provider.enabled ? 'bg-green-500' : 'bg-gray-400'}`} />
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">{provider.name}</p>
        <p className="text-xs text-gray-500 truncate">{provider.provider_type} · {provider.default_model ?? 'no model set'}</p>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {summary && (
          <>
            <span className="text-xs text-gray-500 hidden sm:block">{summary.requests.toLocaleString()} req</span>
            <span className="text-xs text-gray-500 hidden md:block">{summary.success_rate}%</span>
          </>
        )}
        <CircuitBreakerBadge state={(cb?.state as 'closed' | 'open' | 'half-open') ?? 'closed'} />
      </div>
    </div>
  )
}

function buildChartData(providerSummaries: { provider_id: string; requests: number }[]) {
  // Since we only have aggregated data from the summary endpoint, generate a placeholder
  // The actual per-bucket data comes from /api/monitoring/metrics/:id
  if (providerSummaries.length === 0) return []
  return [] // Will be populated when we implement per-bucket chart data
}
