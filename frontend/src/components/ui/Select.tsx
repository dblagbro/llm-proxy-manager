import { forwardRef, type SelectHTMLAttributes } from 'react'
import { clsx } from 'clsx'

interface SelectProps extends SelectHTMLAttributes<HTMLSelectElement> {
  label?: string
  error?: string
}

// Lightweight native select — swap for Radix Select if needed
export const Select = forwardRef<HTMLSelectElement, SelectProps>(
  ({ label, error, className, children, ...props }, ref) => (
    <div className="flex flex-col gap-1">
      {label && <label className="text-sm font-medium text-gray-700 dark:text-gray-300">{label}</label>}
      <select
        ref={ref}
        className={clsx(
          'w-full px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100',
          'border-gray-300 dark:border-gray-600 focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500 focus:outline-none',
          error && 'border-red-500',
          className
        )}
        {...props}
      >
        {children}
      </select>
      {error && <p className="text-xs text-red-500">{error}</p>}
    </div>
  )
)
Select.displayName = 'Select'

// Placeholder exports matching the index.ts re-exports
export const SelectTrigger = Select
export const SelectContent = ({ children }: { children: React.ReactNode }) => <>{children}</>
export const SelectItem = ({ value, children }: { value: string; children: React.ReactNode }) => <option value={value}>{children}</option>
export const SelectValue = ({ placeholder }: { placeholder?: string }) => <option value="">{placeholder}</option>
