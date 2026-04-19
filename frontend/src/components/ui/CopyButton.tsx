import { useState } from 'react'
import { Copy, Check } from 'lucide-react'
import { clsx } from 'clsx'

export function CopyButton({ text, className }: { text: string; className?: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }
  return (
    <button onClick={copy} className={clsx('p-1 rounded text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 transition-colors', className)}>
      {copied ? <Check className="h-4 w-4 text-green-500" /> : <Copy className="h-4 w-4" />}
    </button>
  )
}
