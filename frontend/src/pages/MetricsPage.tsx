import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid,
} from 'recharts'
import { monitoringApi } from '@/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Spinner } from '@/components/ui/Spinner'

const WINDOWS = [6, 24, 72] as const
type Window = typeof WINDOWS[number]

function fmtCost(v: number) {
  if (v === 0) return '$0.00'
  if (v < 0.01) return `$${v.toFixed(5)}`
  return `$${v.toFixed(3)}`
}

export function MetricsPage() {
  const [window, setWindow] = useState<Window>(24)

  const { data: metrics, isLoading } = useQuery({
    queryKey: ['metrics', window],
    queryFn: () => monitoringApi.metrics(window),
    refetchInterval: 60_000,
  })

  const providers = metrics?.providers ?? []

  // Aggregate summary
  const totalRequests = providers.reduce((s, p) => s + p.requests, 0)
  const totalCost = providers.reduce((s, p) => s + p.total_cost_usd, 0)
  const avgSuccess = providers.length
    ? providers.reduce((s, p) => s + p.success_rate, 0) / providers.length
    : 0

  // Bar chart data — one bar per provider
  const barData = providers.map(p => ({
    name: p.provider_id.slice(0, 12),
    requests: p.requests,
    cost: +p.total_cost_usd.toFixed(4),
    success: +p.success_rate.toFixed(1),
  }))

  return (
    <div className="p-6 space-y-6 max-w-6xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Metrics</h1>
          <p className="text-sm text-gray-500 mt-0.5">Provider performance statistics</p>
        </div>
        <div className="flex gap-1.5">
          {WINDOWS.map(w => (
            <button
              key={w}
              onClick={() => setWindow(w)}
              className={`px-3 py-1.5 text-sm rounded-lg transition-colors ${
                window === w
                  ? 'bg-indigo-600 text-white'
                  : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 border border-gray-200 dark:border-gray-700 hover:border-indigo-400'
              }`}
            >
              {w}h
            </button>
          ))}
        </div>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-3 gap-4">
        {[
          { label: 'Total Requests', value: totalRequests.toLocaleString() },
          { label: 'Total Cost', value: fmtCost(totalCost) },
          { label: 'Avg Success Rate', value: `${avgSuccess.toFixed(1)}%` },
        ].map(c => (
          <Card key={c.label}>
            <CardContent className="py-5">
              <p className="text-xs text-gray-400 mb-1">{c.label}</p>
              <p className="text-2xl font-bold text-gray-900 dark:text-gray-100">{c.value}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      {isLoading ? (
        <div className="flex justify-center py-16"><Spinner /></div>
      ) : providers.length === 0 ? (
        <Card><CardContent><p className="text-center text-gray-500 py-12">No metrics data yet</p></CardContent></Card>
      ) : (
        <>
          {/* Requests by provider */}
          <Card>
            <CardHeader><CardTitle>Requests by Provider — last {window}h</CardTitle></CardHeader>
            <CardContent>
              <ResponsiveContainer width="100%" height={220}>
                <BarChart data={barData}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#374151" opacity={0.4} />
                  <XAxis dataKey="name" tick={{ fontSize: 11, fill: '#9ca3af' }} />
                  <YAxis tick={{ fontSize: 11, fill: '#9ca3af' }} />
                  <Tooltip
                    contentStyle={{ background: '#1f2937', border: '1px solid #374151', borderRadius: 8, fontSize: 12 }}
                    labelStyle={{ color: '#e5e7eb' }}
                  />
                  <Bar dataKey="requests" fill="#6366f1" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            </CardContent>
          </Card>

          {/* Per-provider table */}
          <Card>
            <CardHeader><CardTitle>Provider Summary</CardTitle></CardHeader>
            <CardContent className="p-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-gray-100 dark:border-gray-700">
                      {['Provider', 'Requests', 'Success %', 'Avg Latency', 'Tokens', 'Cost'].map(h => (
                        <th key={h} className="text-left px-5 py-3 text-xs text-gray-400 font-medium">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
                    {providers.map(p => (
                      <tr key={p.provider_id} className="hover:bg-gray-50 dark:hover:bg-gray-800/50">
                        <td className="px-5 py-3 font-medium text-gray-900 dark:text-gray-100">{p.provider_id}</td>
                        <td className="px-5 py-3 text-gray-600 dark:text-gray-400">{p.requests.toLocaleString()}</td>
                        <td className="px-5 py-3">
                          <span className={p.success_rate >= 95 ? 'text-green-600' : p.success_rate >= 80 ? 'text-amber-500' : 'text-red-500'}>
                            {p.success_rate.toFixed(1)}%
                          </span>
                        </td>
                        <td className="px-5 py-3 text-gray-600 dark:text-gray-400">
                          {p.avg_latency_ms > 1000 ? `${(p.avg_latency_ms / 1000).toFixed(1)}s` : `${Math.round(p.avg_latency_ms)}ms`}
                        </td>
                        <td className="px-5 py-3 text-gray-600 dark:text-gray-400">{p.total_tokens.toLocaleString()}</td>
                        <td className="px-5 py-3 text-gray-600 dark:text-gray-400">{fmtCost(p.total_cost_usd)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  )
}
