import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import type { OAuthCaptureProfile } from '@/api'

export function ProfileList({
  profiles, selected, onSelect,
}: {
  profiles: OAuthCaptureProfile[]
  selected: string | null
  onSelect: (name: string) => void
}) {
  if (profiles.length === 0) return null

  return (
    <Card>
      <CardHeader><CardTitle>Profiles ({profiles.length})</CardTitle></CardHeader>
      <CardContent className="space-y-1">
        {profiles.map(p => (
          <button
            key={p.name}
            onClick={() => onSelect(p.name)}
            className={
              'w-full text-left rounded px-2 py-1.5 text-sm ' +
              (selected === p.name
                ? 'bg-indigo-50 dark:bg-indigo-900/30 text-indigo-900 dark:text-indigo-100'
                : 'hover:bg-gray-50 dark:hover:bg-gray-800')
            }
          >
            <div className="flex items-center justify-between">
              <span className="font-mono">{p.name}</span>
              <span className={
                'rounded px-1.5 py-0.5 text-xs ' +
                (p.enabled ? 'bg-green-100 text-green-900' : 'bg-gray-200 text-gray-700')
              }>
                {p.enabled ? 'capturing' : 'paused'}
              </span>
            </div>
            {p.preset && <div className="text-xs text-gray-500">{p.preset}</div>}
          </button>
        ))}
      </CardContent>
    </Card>
  )
}
