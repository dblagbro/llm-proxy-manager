import { useState, useCallback, useEffect } from 'react'
import { CheckCircle, AlertTriangle, XCircle, Info, X } from 'lucide-react'
import { clsx } from 'clsx'

export type ToastVariant = 'success' | 'warning' | 'error' | 'info'

interface ToastItem {
  id: number
  message: string
  variant: ToastVariant
}

let _addToast: ((msg: string, variant: ToastVariant) => void) | null = null

export function useToast() {
  return {
    toast: (message: string, variant: ToastVariant = 'info') => _addToast?.(message, variant),
    success: (msg: string) => _addToast?.(msg, 'success'),
    error:   (msg: string) => _addToast?.(msg, 'error'),
    warning: (msg: string) => _addToast?.(msg, 'warning'),
    info:    (msg: string) => _addToast?.(msg, 'info'),
  }
}

export function Toaster() {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  const add = useCallback((message: string, variant: ToastVariant) => {
    const id = Date.now()
    setToasts(prev => [...prev, { id, message, variant }])
    setTimeout(() => setToasts(prev => prev.filter(t => t.id !== id)), 4000)
  }, [])

  useEffect(() => { _addToast = add; return () => { _addToast = null } }, [add])

  if (toasts.length === 0) return null

  return (
    <div className="fixed bottom-4 right-4 z-[100] flex flex-col gap-2 max-w-sm">
      {toasts.map(t => <Toast key={t.id} {...t} onClose={() => setToasts(p => p.filter(x => x.id !== t.id))} />)}
    </div>
  )
}

function Toast({ message, variant, onClose }: ToastItem & { onClose: () => void }) {
  const icons = { success: CheckCircle, warning: AlertTriangle, error: XCircle, info: Info }
  const colors = {
    success: 'bg-green-50 dark:bg-green-900/30 border-green-200 dark:border-green-800 text-green-800 dark:text-green-200',
    warning: 'bg-amber-50 dark:bg-amber-900/30 border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-200',
    error:   'bg-red-50 dark:bg-red-900/30 border-red-200 dark:border-red-800 text-red-800 dark:text-red-200',
    info:    'bg-blue-50 dark:bg-blue-900/30 border-blue-200 dark:border-blue-800 text-blue-800 dark:text-blue-200',
  }
  const Icon = icons[variant]
  return (
    <div className={clsx('flex items-start gap-3 px-4 py-3 rounded-xl border shadow-lg text-sm animate-in slide-in-from-right', colors[variant])}>
      <Icon className="h-4 w-4 mt-0.5 shrink-0" />
      <span className="flex-1">{message}</span>
      <button onClick={onClose} className="shrink-0 opacity-60 hover:opacity-100"><X className="h-4 w-4" /></button>
    </div>
  )
}
