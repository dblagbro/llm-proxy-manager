import type { ReactNode } from 'react'
import { clsx } from 'clsx'

interface StatCardProps {
  label: string
  value: ReactNode
  sub?: ReactNode
  icon?: ReactNode
  variant?: 'default' | 'success' | 'warning' | 'danger'
  className?: string
}

export function StatCard({ label, value, sub, icon, variant = 'default', className }: StatCardProps) {
  const variants = {
    default: 'bg-white dark:bg-gray-800 border-gray-200 dark:border-gray-700',
    success: 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800',
    warning: 'bg-amber-50 dark:bg-amber-900/20 border-amber-200 dark:border-amber-800',
    danger:  'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800',
  }
  return (
    <div className={clsx('rounded-xl border p-4 shadow-sm', variants[variant], className)}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex flex-col gap-1 min-w-0">
          <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase tracking-wide truncate">{label}</p>
          <p className="text-2xl font-bold text-gray-900 dark:text-gray-100 leading-tight">{value}</p>
          {sub && <p className="text-xs text-gray-500 dark:text-gray-400">{sub}</p>}
        </div>
        {icon && <div className="text-gray-400 dark:text-gray-500 shrink-0">{icon}</div>}
      </div>
    </div>
  )
}
