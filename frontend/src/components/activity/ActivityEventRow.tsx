import { clsx } from 'clsx'
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

interface Props {
  event: ActivityEvent
  compact?: boolean
}

export function ActivityEventRow({ event, compact }: Props) {
  const dot = SEVERITY_DOT[event.severity] ?? 'bg-gray-400'
  return (
    <div className={clsx('flex items-start gap-3 px-4', compact ? 'py-2' : 'py-3')}>
      <span className={clsx('mt-1.5 h-2 w-2 rounded-full shrink-0', dot)} />
      <div className="flex-1 min-w-0">
        <p className="text-sm text-gray-800 dark:text-gray-200 truncate">{event.message}</p>
        {!compact && event.provider_id && (
          <p className="text-xs text-gray-400 mt-0.5">Provider: {event.provider_id}</p>
        )}
      </div>
      <span className="text-xs text-gray-400 shrink-0 tabular-nums">{fmt(event.timestamp)}</span>
    </div>
  )
}
