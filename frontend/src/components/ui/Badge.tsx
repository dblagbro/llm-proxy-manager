import { clsx } from 'clsx'
import type { ReactNode, MouseEventHandler } from 'react'

interface BadgeProps {
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info' | 'muted'
  size?: 'sm' | 'md'
  children: ReactNode
  className?: string
  // v2.8.0: optional click handler — used by the "Needs re-auth" badge to
  // jump the user into the expanded provider panel.
  onClick?: MouseEventHandler<HTMLSpanElement>
}

export function Badge({ variant = 'default', size = 'sm', children, className, onClick }: BadgeProps) {
  const variants = {
    default: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
    success: 'bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-400',
    warning: 'bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-400',
    danger:  'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-400',
    info:    'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-400',
    muted:   'bg-gray-50 text-gray-500 dark:bg-gray-800 dark:text-gray-500',
  }
  const sizes = { sm: 'px-2 py-0.5 text-xs', md: 'px-2.5 py-1 text-sm' }
  return (
    <span
      onClick={onClick}
      className={clsx('inline-flex items-center rounded-full font-medium', variants[variant], sizes[size], className)}
    >
      {children}
    </span>
  )
}
