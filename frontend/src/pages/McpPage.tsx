/**
 * v5.7.5 — MCP dashboard panel.
 *
 * Single-page view of the proxy's MCP aggregation surface:
 *   - live tool inventory (read from the FastMCP root via list_tools)
 *   - 24h call counts by tool + per-key activity
 *   - latency p50/p95 per tool
 *   - link to the per-key policy editor (route /admin/mcp/policy/:keyId)
 *
 * Read-only on this page; the admin endpoints for editing policy live
 * under /api/admin/mcp/keys/{key_id}/policy and are surfaced from the
 * existing API Keys page (v5.7.5 follow-up — keeps this page minimal).
 */
import { useQuery } from '@tanstack/react-query'
import { api } from '@/api/client'

type ToolLive = { name: string; description: string }
type CallsByTool = { tool_name: string; count: number; errors: number }
type CallsByKey = { api_key_id: string; count: number }
type LatencyByTool = { tool_name: string; p50_ms: number; p95_ms: number; n: number }

interface McpSummary {
  tools_live: ToolLive[]
  calls_by_tool_24h: CallsByTool[]
  calls_by_key_24h: CallsByKey[]
  latency_by_tool_24h: LatencyByTool[]
  total_calls_24h: number
  total_errors_24h: number
  tools_live_error?: string
  agg_error?: string
}

export function McpPage() {
  const { data, isLoading, error } = useQuery<McpSummary>({
    queryKey: ['admin', 'mcp', 'summary'],
    queryFn: () => api.get<McpSummary>('/api/admin/mcp/summary'),
    refetchInterval: 30_000,
  })

  if (isLoading)
    return <div className="p-6 text-gray-500 dark:text-gray-400">Loading…</div>
  if (error)
    return (
      <div className="p-6 text-red-600 dark:text-red-300">
        Failed to load MCP summary: {(error as Error).message}
      </div>
    )
  if (!data) return null

  const errPct =
    data.total_calls_24h > 0
      ? Math.round((data.total_errors_24h / data.total_calls_24h) * 100)
      : 0

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-gray-100">
          MCP — Model Context Protocol
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Aggregation endpoint: <code className="bg-gray-100 dark:bg-gray-800 px-1 py-0.5 rounded">/llm-proxy2/mcp/</code>
        </p>
      </div>

      {(data.tools_live_error || data.agg_error) && (
        <div className="rounded-md border border-amber-300 bg-amber-50 dark:bg-amber-950 dark:border-amber-800 p-3 text-sm text-amber-900 dark:text-amber-200">
          {data.tools_live_error && (
            <div>
              <strong>tools_live error:</strong> {data.tools_live_error}
            </div>
          )}
          {data.agg_error && (
            <div>
              <strong>aggregation error:</strong> {data.agg_error}
            </div>
          )}
        </div>
      )}

      {/* ── 24h headline numbers ── */}
      <section className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Stat label="Tool calls (24h)" value={data.total_calls_24h.toLocaleString()} />
        <Stat
          label="Errors (24h)"
          value={`${data.total_errors_24h.toLocaleString()} (${errPct}%)`}
          tone={errPct > 10 ? 'red' : errPct > 2 ? 'amber' : 'green'}
        />
        <Stat label="Tools registered" value={String(data.tools_live.length)} />
      </section>

      {/* ── Live tool inventory ── */}
      <section>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Tools available (live from FastMCP root)
        </h2>
        {data.tools_live.length === 0 ? (
          <p className="text-gray-500 dark:text-gray-400 text-sm">
            No tools registered. The MCP endpoint may be in scaffold mode.
          </p>
        ) : (
          <ul className="divide-y divide-gray-200 dark:divide-gray-800 rounded-md border border-gray-200 dark:border-gray-800 overflow-hidden">
            {data.tools_live.map((t) => (
              <li key={t.name} className="px-4 py-3 bg-white dark:bg-gray-900">
                <div className="font-mono text-sm text-gray-900 dark:text-gray-100">
                  {t.name}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-0.5">
                  {t.description || <em>(no description)</em>}
                </div>
              </li>
            ))}
          </ul>
        )}
      </section>

      {/* ── Calls by tool ── */}
      <section>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Calls by tool — last 24h
        </h2>
        <Table
          rows={data.calls_by_tool_24h}
          empty="No tool calls yet."
          cols={[
            { key: 'tool_name', label: 'Tool' },
            { key: 'count', label: 'Calls', align: 'right' },
            { key: 'errors', label: 'Errors', align: 'right' },
          ]}
        />
      </section>

      {/* ── Latency by tool ── */}
      <section>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Latency by tool — last 24h (successful calls only)
        </h2>
        <Table
          rows={data.latency_by_tool_24h}
          empty="No successful tool calls yet."
          cols={[
            { key: 'tool_name', label: 'Tool' },
            { key: 'p50_ms', label: 'p50 ms', align: 'right' },
            { key: 'p95_ms', label: 'p95 ms', align: 'right' },
            { key: 'n', label: 'N', align: 'right' },
          ]}
        />
      </section>

      {/* ── Top callers ── */}
      <section>
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-2">
          Top callers by API key — last 24h
        </h2>
        <Table
          rows={data.calls_by_key_24h}
          empty="No keys have hit the MCP endpoint yet."
          cols={[
            { key: 'api_key_id', label: 'API key id', mono: true },
            { key: 'count', label: 'Calls', align: 'right' },
          ]}
        />
      </section>
    </div>
  )
}

function Stat({
  label,
  value,
  tone = 'gray',
}: {
  label: string
  value: string
  tone?: 'gray' | 'green' | 'amber' | 'red'
}) {
  const toneClass =
    tone === 'red'
      ? 'text-red-600 dark:text-red-300'
      : tone === 'amber'
      ? 'text-amber-600 dark:text-amber-300'
      : tone === 'green'
      ? 'text-emerald-600 dark:text-emerald-300'
      : 'text-gray-900 dark:text-gray-100'
  return (
    <div className="rounded-md border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 px-4 py-3">
      <div className="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400">
        {label}
      </div>
      <div className={`mt-1 text-2xl font-semibold ${toneClass}`}>{value}</div>
    </div>
  )
}

type Col = { key: string; label: string; align?: 'left' | 'right'; mono?: boolean }

function Table({
  rows,
  cols,
  empty,
}: {
  rows: any[]
  cols: Col[]
  empty: string
}) {
  if (!rows.length)
    return (
      <p className="text-gray-500 dark:text-gray-400 text-sm">{empty}</p>
    )
  return (
    <div className="rounded-md border border-gray-200 dark:border-gray-800 overflow-x-auto bg-white dark:bg-gray-900">
      <table className="min-w-full text-sm">
        <thead className="bg-gray-50 dark:bg-gray-800">
          <tr>
            {cols.map((c) => (
              <th
                key={c.key}
                className={`px-4 py-2 ${
                  c.align === 'right' ? 'text-right' : 'text-left'
                } text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400`}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-200 dark:divide-gray-800">
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c) => (
                <td
                  key={c.key}
                  className={`px-4 py-2 ${
                    c.align === 'right' ? 'text-right' : 'text-left'
                  } ${c.mono ? 'font-mono text-xs' : ''} text-gray-900 dark:text-gray-100`}
                >
                  {r[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
