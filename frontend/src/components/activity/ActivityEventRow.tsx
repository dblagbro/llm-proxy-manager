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

/** Pull a short text preview from a request_body JSON string.
 * Picks the last user message's content and clips it. Returns empty
 * string if the body isn't parseable JSON or has no user message. */
function previewRequest(body: string | undefined, max = 240): string {
  if (!body) return ''
  try {
    const obj = JSON.parse(body)
    const msgs = obj?.messages
    if (Array.isArray(msgs) && msgs.length) {
      // Walk backwards to find the last user message
      for (let i = msgs.length - 1; i >= 0; i--) {
        const m = msgs[i]
        if (m?.role !== 'user') continue
        const c = m.content
        const txt = typeof c === 'string'
          ? c
          : Array.isArray(c)
            ? c.map((b: { type?: string; text?: string }) => b?.type === 'text' ? (b.text ?? '') : '').filter(Boolean).join(' ')
            : ''
        if (txt) return txt.length > max ? txt.slice(0, max).trimEnd() + '…' : txt
      }
    }
    if (typeof obj?.prompt === 'string') {
      const t = obj.prompt
      return t.length > max ? t.slice(0, max).trimEnd() + '…' : t
    }
  } catch { /* not JSON */ }
  return body.length > max ? body.slice(0, max).trimEnd() + '…' : body
}

/** Pull a short text preview from a response_body JSON string.
 * Anthropic shape (content[].text) and OpenAI shape (choices[0].message.content). */
function previewResponse(body: string | undefined, max = 240): string {
  if (!body) return ''
  try {
    const obj = JSON.parse(body)
    if (Array.isArray(obj?.content)) {
      const txt = obj.content.map((b: { type?: string; text?: string }) => b?.type === 'text' ? (b.text ?? '') : '').filter(Boolean).join(' ')
      if (txt) return txt.length > max ? txt.slice(0, max).trimEnd() + '…' : txt
    }
    const ch = obj?.choices?.[0]?.message?.content
    if (typeof ch === 'string' && ch) return ch.length > max ? ch.slice(0, max).trimEnd() + '…' : ch
  } catch { /* not JSON */ }
  return ''
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
  const reqPreview = previewRequest(reqBody, compact ? 120 : 240)
  const respPreview = previewResponse(respBody, compact ? 120 : 240)

  return (
    <div className={clsx('px-4', compact ? 'py-1.5' : 'py-2')}>
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
          <div className="flex items-baseline gap-2">
            <p className="text-sm text-gray-800 dark:text-gray-200 truncate flex-1 min-w-0">{event.message}</p>
            {summary && <span className="text-xs text-gray-400 font-mono shrink-0">{summary}</span>}
            <span className="text-xs text-gray-400 shrink-0 tabular-nums">{fmt(event.timestamp)}</span>
          </div>
          {(reqPreview || respPreview || errorMsg) && (
            <div className="mt-1 space-y-0.5 text-xs">
              {reqPreview && (
                <p className="truncate text-gray-600 dark:text-gray-400">
                  <span className="text-indigo-500 dark:text-indigo-400 font-medium mr-1.5">→</span>
                  <span className="text-gray-500 dark:text-gray-500">{reqPreview}</span>
                </p>
              )}
              {respPreview && (
                <p className="truncate text-gray-600 dark:text-gray-400">
                  <span className="text-emerald-500 dark:text-emerald-400 font-medium mr-1.5">←</span>
                  <span className="text-gray-500 dark:text-gray-500">{respPreview}</span>
                </p>
              )}
              {errorMsg && !respPreview && (
                <p className="truncate text-red-500 dark:text-red-400">
                  <span className="font-medium mr-1.5">!</span>
                  <span>{errorMsg.length > 240 ? errorMsg.slice(0, 240) + '…' : errorMsg}</span>
                </p>
              )}
            </div>
          )}
        </div>
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
