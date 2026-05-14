/**
 * v3.7.28 (#252 phase 2) — top-of-page banner that surfaces providers
 * currently under manual override.
 *
 * When the operator clicks "Disable" on a provider, the backend sets
 * manual_override_until on that row. The AI supervisor (Phase 4) will
 * refuse to mutate enabled/auto_skip_until/priority on those providers.
 * This banner is the visible reminder so the operator doesn't forget
 * they have manual overrides in place, and provides a one-click way
 * to release all locks back to AI control.
 *
 * Rendered globally in Layout.tsx — appears on every admin page.
 * Auto-hides when no provider is locked.
 */
import { useState } from 'react'
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query'
import { Lock, ChevronDown, ChevronUp } from 'lucide-react'
import { providersApi } from '@/api'
import { useToast } from '@/components/ui/Toast'

export function ManualOverrideBanner() {
  const queryClient = useQueryClient()
  const toast = useToast()
  const [expanded, setExpanded] = useState(false)
  const [confirming, setConfirming] = useState(false)

  const { data: providers } = useQuery({
    queryKey: ['providers'],
    queryFn: providersApi.list,
    refetchInterval: 30_000,
  })

  const locked = (providers ?? []).filter(p => p.manual_override_active)

  const releaseAll = useMutation({
    mutationFn: () => providersApi.releaseManualOverrides(),
    onSuccess: r => {
      // v3.8.6 — backend now ALSO re-enables by default; surface that
      // in the toast so the operator knows their click did both things.
      const enabledNote = (r as { re_enabled?: boolean }).re_enabled === false
        ? ' (locks only — providers stay disabled)'
        : ' (releases + re-enables)'
      toast.success(`Released ${r.released} provider lock${r.released === 1 ? '' : 's'}${enabledNote}`)
      queryClient.invalidateQueries({ queryKey: ['providers'] })
      setConfirming(false)
      setExpanded(false)
    },
    onError: (e: unknown) => {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || 'Failed to release manual overrides')
      setConfirming(false)
    },
  })

  if (locked.length === 0) return null

  return (
    <div className="bg-amber-50 dark:bg-amber-950/40 border-b border-amber-200 dark:border-amber-800 shrink-0">
      <div className="px-4 py-2 flex items-center gap-3 text-sm">
        <Lock className="h-4 w-4 shrink-0 text-amber-700 dark:text-amber-300" />
        <span className="flex-1 min-w-0 text-amber-900 dark:text-amber-200">
          <span className="font-medium">
            {locked.length} provider{locked.length === 1 ? '' : 's'} under manual override
          </span>
          <span className="text-amber-700 dark:text-amber-300 ml-2 hidden sm:inline">
            — AI supervisor won't manage {locked.length === 1 ? 'it' : 'them'} until released.
          </span>
        </span>
        <button
          onClick={() => setExpanded(s => !s)}
          className="text-xs text-amber-800 dark:text-amber-300 hover:underline flex items-center gap-1 shrink-0"
          aria-label="Toggle details"
        >
          {expanded ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
          {expanded ? 'Hide details' : 'View details'}
        </button>
        {!confirming ? (
          <button
            onClick={() => setConfirming(true)}
            className="bg-white dark:bg-amber-900 text-amber-800 dark:text-amber-100 hover:bg-amber-100 dark:hover:bg-amber-800 px-3 py-1 rounded text-xs font-medium shrink-0 border border-amber-300 dark:border-amber-700"
            title="Releases the manual override AND re-enables the locked providers — the inverse of the Disable click that locked them."
          >
            Release &amp; re-enable all
          </button>
        ) : (
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-xs text-amber-800 dark:text-amber-200">
              Release locks &amp; re-enable {locked.length} provider{locked.length === 1 ? '' : 's'}?
            </span>
            <button
              onClick={() => releaseAll.mutate()}
              disabled={releaseAll.isPending}
              className="bg-amber-600 text-white hover:bg-amber-700 px-2.5 py-1 rounded text-xs font-medium"
            >
              {releaseAll.isPending ? 'Releasing…' : 'Yes, release & enable'}
            </button>
            <button
              onClick={() => setConfirming(false)}
              className="text-amber-700 dark:text-amber-300 hover:underline text-xs"
            >
              Cancel
            </button>
          </div>
        )}
      </div>
      {expanded && (
        <div className="px-4 pb-3 pt-1 border-t border-amber-200 dark:border-amber-800/50 bg-amber-50/70 dark:bg-amber-950/30">
          <ul className="space-y-1 text-xs text-amber-900 dark:text-amber-200">
            {locked.map(p => (
              <li key={p.id} className="flex items-center gap-2">
                <Lock className="h-3 w-3 shrink-0 text-amber-700 dark:text-amber-400" />
                <span className="font-medium">{p.name}</span>
                <span className="text-amber-700 dark:text-amber-400 font-mono text-[11px]">
                  {p.provider_type}
                </span>
                {p.manual_override_set_at && (
                  <span className="text-amber-700 dark:text-amber-400 ml-auto text-[11px]">
                    locked {new Date(p.manual_override_set_at).toLocaleString()}
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
