import { useState } from 'react'
import { clsx } from 'clsx'
import { ChevronRight, ChevronDown } from 'lucide-react'
import type { ActivityEvent } from '@/types'

const SEVERITY_DOT: Record<string, string> = {
  info: 'bg-blue-400',
  warning: 'bg-amber-400',
  error: 'bg-red-500',
  critical: 'bg-red-700',
}

function fmt(ts: string) {
  const d = new Date(ts)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function tryPretty(s: unknown): string {
  if (typeof s !== 'string' || !s) return ''
  try {
    return JSON.stringify(JSON.parse(s), null, 2)
  } catch {
    return s
  }
}

function summarize(meta: Record<string, unknown> | undefined): string {
  if (!meta) return ''
  const parts: string[] = []
  if (meta.in_tok != null) parts.push(`in:${meta.in_tok}`)
  if (meta.out_tok != null) parts.push(`out:${meta.out_tok}`)
  if (meta.cost_usd != null) parts.push(`$${Number(meta.cost_usd).toFixed(5)}`)
  if (meta.latency_ms != null) parts.push(`${Math.round(Number(meta.latency_ms))}ms`)
  return parts.join(' · ')
}

interface Props {
  event: ActivityEvent
  compact?: boolean
}

export function ActivityEventRow({ event, compact }: Props) {
  const dot = SEVERITY_DOT[event.severity] ?? 'bg-gray-400'
  const meta = (event.metadata || {}) as Record<string, unknown>
  const reqBody = meta.request_body as string | undefined
  const respBody = meta.response_body as string | undefined
  const errorMsg = meta.error as string | undefined
  const expandable = Boolean(reqBody || respBody || errorMsg)
  const [open, setOpen] = useState(false)
  const summary = summarize(meta)

  return (
    <div className={clsx('px-4', compact ? 'py-1.5' : 'py-2.5')}>
      <div
        className={clsx(
          'flex items-start gap-2',
          expandable && 'cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-800/40 -mx-2 px-2 rounded'
        )}
        onClick={() => expandable && setOpen(!open)}
      >
        {expandable ? (
          open
            ? <ChevronDown className="mt-1 h-3.5 w-3.5 shrink-0 text-gray-400" />
            : <ChevronRight className="mt-1 h-3.5 w-3.5 shrink-0 text-gray-400" />
        ) : (
          <span className="mt-1 h-3.5 w-3.5 shrink-0" />
        )}
        <span className={clsx('mt-1.5 h-2 w-2 rounded-full shrink-0', dot)} />
        <div className="flex-1 min-w-0">
          <p className="text-sm text-gray-800 dark:text-gray-200 truncate">{event.message}</p>
          {!compact && (event.provider_id || summary) && (
            <p className="text-xs text-gray-400 mt-0.5">
              {event.provider_id && <span>Provider: <span className="font-mono">{event.provider_id}</span></span>}
              {event.provider_id && summary && <span>  ·  </span>}
              {summary && <span className="font-mono">{summary}</span>}
            </p>
          )}
        </div>
        <span className="text-xs text-gray-400 shrink-0 tabular-nums">{fmt(event.timestamp)}</span>
      </div>

      {open && expandable && (
        <div className="mt-2 ml-7 space-y-2 text-xs">
          {errorMsg && (
            <div>
              <p className="text-[11px] font-medium text-red-500 mb-1">Error</p>
              <pre className="font-mono whitespace-pre-wrap break-all bg-red-50 dark:bg-red-950/30 text-red-700 dark:text-red-300 rounded p-2 max-h-60 overflow-auto">{errorMsg}</pre>
            </div>
          )}
          {reqBody && (
            <div>
              <p className="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-1">Request</p>
              <pre className="font-mono whitespace-pre-wrap break-all bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-200 rounded p-2 max-h-96 overflow-auto">{tryPretty(reqBody)}</pre>
            </div>
          )}
          {respBody && (
            <div>
              <p className="text-[11px] font-medium text-gray-500 dark:text-gray-400 mb-1">Response</p>
              <pre className="font-mono whitespace-pre-wrap break-all bg-gray-50 dark:bg-gray-900 text-gray-700 dark:text-gray-200 rounded p-2 max-h-96 overflow-auto">{tryPretty(respBody)}</pre>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
