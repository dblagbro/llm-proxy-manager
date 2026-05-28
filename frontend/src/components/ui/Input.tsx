import { forwardRef, useId, type InputHTMLAttributes } from 'react'
import { clsx } from 'clsx'
import { HelpHint } from './HelpHint'

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: string
  error?: string
  tooltip?: string
}

export const Input = forwardRef<HTMLInputElement, InputProps>(
  ({ label, error, tooltip, className, id, ...props }, ref) => {
  // v4.4.26 (F-OBS-005) — associate the visible label with the input via
  // htmlFor/id so the control has an accessible name for screen readers.
  // Pre-fix the <label> wrapped only a <span> with no `for`, leaving the
  // <input> programmatically anonymous (flagged by the a11y audit).
  const autoId = useId()
  const inputId = id ?? autoId
  return (
    <div className="flex flex-col gap-1">
      {label && (
        <label htmlFor={inputId} className="text-sm font-medium text-gray-700 dark:text-gray-300 flex items-center gap-1">
          <span>{label}</span>
          {tooltip && <HelpHint text={tooltip} />}
        </label>
      )}
      <input
        ref={ref}
        id={inputId}
        className={clsx(
          'w-full px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
          'placeholder:text-gray-400 dark:placeholder:text-gray-500',
          error
            ? 'border-red-500 focus:ring-red-500'
            : 'border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-indigo-500',
          'focus:outline-none focus:ring-2 focus:ring-offset-0',
          className
        )}
        {...props}
      />
      {error && <p className="text-xs text-red-500">{error}</p>}
    </div>
  )
  }
)
Input.displayName = 'Input'
