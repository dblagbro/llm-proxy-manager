import { useState, useEffect } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Save } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import { authApi } from '@/api'
import { useAuth } from '@/context/AuthContext'
import { COMMON_TIMEZONES, formatTimeForUser } from '@/utils/time'

/**
 * Display preferences for the logged-in user (v3.0 R1).
 *
 * Two prefs, both optional (NULL = follow browser):
 *  - timezone: IANA name (dropdown of common zones + free-text fallback)
 *  - time_format: '12h' | '24h' | '' (locale default)
 *
 * Server is always UTC; this only changes presentation.
 */
export function UserPreferencesCard() {
  const { user, refresh } = useAuth()
  const qc = useQueryClient()
  const toast = useToast()
  const [timezone, setTimezone] = useState<string>('')
  const [timeFormat, setTimeFormat] = useState<'12h' | '24h' | ''>('')

  useEffect(() => {
    setTimezone(user?.timezone || '')
    setTimeFormat((user?.time_format as '12h' | '24h' | null) || '')
  }, [user?.timezone, user?.time_format])

  const save = useMutation({
    mutationFn: () => authApi.setPreferences({
      timezone: timezone,
      time_format: timeFormat,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['auth', 'me'] })
      refresh()
      toast.success('Preferences saved')
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  })

  const sampleTs = new Date()
  const samplePrefs = {
    timezone: timezone || null,
    time_format: (timeFormat || null) as '12h' | '24h' | null,
  }
  const previewUser = { username: user?.username || '', role: 'user' as const, ...samplePrefs }

  return (
    <Card>
      <CardHeader><CardTitle>Display Preferences</CardTitle></CardHeader>
      <CardContent className="space-y-4">
        <p className="text-xs text-gray-400">
          Affects how timestamps are shown in the UI. Server logs and stored data are always in UTC.
        </p>
        <div className="grid grid-cols-2 gap-4">
          <div className="flex flex-col gap-1">
            <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Timezone</label>
            <select
              value={timezone}
              onChange={e => setTimezone(e.target.value)}
              className="w-full px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              {COMMON_TIMEZONES.map(tz => (
                <option key={tz.value} value={tz.value}>{tz.label}</option>
              ))}
              {timezone && !COMMON_TIMEZONES.find(t => t.value === timezone) && (
                <option value={timezone}>{timezone} (custom)</option>
              )}
            </select>
          </div>
          <div className="flex flex-col gap-1">
            <label className="text-sm font-medium text-gray-700 dark:text-gray-300">Time format</label>
            <select
              value={timeFormat}
              onChange={e => setTimeFormat(e.target.value as '12h' | '24h' | '')}
              className="w-full px-3 py-2 text-sm rounded-lg border bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border-gray-300 dark:border-gray-600 focus:outline-none focus:ring-2 focus:ring-indigo-500"
            >
              <option value="">Locale default</option>
              <option value="12h">12-hour (3:45 PM)</option>
              <option value="24h">24-hour (15:45)</option>
            </select>
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400 border-t border-gray-100 dark:border-gray-700 pt-3">
          <span className="font-medium">Preview:</span>
          <span className="font-mono">{formatTimeForUser(sampleTs, previewUser, 'datetime')}</span>
        </div>
        <div className="flex justify-end">
          <Button size="sm" onClick={() => save.mutate()} loading={save.isPending}>
            <Save className="h-4 w-4 mr-1.5" />Save Preferences
          </Button>
        </div>
      </CardContent>
    </Card>
  )
}
