import { clsx } from 'clsx'

export function Spinner({ className }: { className?: string }) {
  return (
    <div className={clsx('h-5 w-5 border-2 border-current border-t-transparent rounded-full animate-spin', className)} />
  )
}
