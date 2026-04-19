import { clsx } from 'clsx'
import type { ReactNode } from 'react'

interface CardProps { children: ReactNode; className?: string; onClick?: () => void }

export function Card({ children, className, onClick }: CardProps) {
  return (
    <div
      onClick={onClick}
      className={clsx(
        'bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm',
        onClick && 'cursor-pointer hover:border-indigo-500 dark:hover:border-indigo-500 transition-colors',
        className
      )}
    >
      {children}
    </div>
  )
}

export function CardHeader({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('px-6 py-4 border-b border-gray-200 dark:border-gray-700', className)}>{children}</div>
}

export function CardTitle({ children, className }: { children: ReactNode; className?: string }) {
  return <h3 className={clsx('text-sm font-semibold text-gray-900 dark:text-gray-100', className)}>{children}</h3>
}

export function CardContent({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('px-6 py-4', className)}>{children}</div>
}

export function CardFooter({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={clsx('px-6 py-3 border-t border-gray-200 dark:border-gray-700 bg-gray-50 dark:bg-gray-800/50 rounded-b-xl', className)}>{children}</div>
}
