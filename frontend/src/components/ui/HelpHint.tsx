import { useState, useRef, useEffect } from 'react'
import { HelpCircle } from 'lucide-react'

interface HelpHintProps {
  text: string
}

export function HelpHint({ text }: HelpHintProps) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLSpanElement>(null)

  useEffect(() => {
    if (!open) return
    function onDoc(e: MouseEvent) {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('click', onDoc)
    return () => document.removeEventListener('click', onDoc)
  }, [open])

  return (
    <span
      ref={wrapRef}
      className="relative inline-flex"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        aria-label="Show help"
        onClick={(e) => { e.preventDefault(); setOpen(o => !o) }}
        className="text-gray-400 hover:text-indigo-500 focus:outline-none focus:text-indigo-500"
      >
        <HelpCircle className="h-3.5 w-3.5" />
      </button>
      {open && (
        <span
          role="tooltip"
          className="absolute left-1/2 -translate-x-1/2 top-full mt-1.5 z-50 w-64 rounded-md border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-900 px-3 py-2 text-xs leading-relaxed text-gray-700 dark:text-gray-200 shadow-lg whitespace-normal pointer-events-none"
        >
          {text}
        </span>
      )}
    </span>
  )
}
