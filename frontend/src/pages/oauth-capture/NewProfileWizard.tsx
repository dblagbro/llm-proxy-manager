import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Plus } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { useToast } from '@/components/ui/Toast'
import { oauthCaptureApi, type OAuthCapturePreset } from '@/api'

export function NewProfileWizard({
  presets, existingNames, onCreated,
}: {
  presets: OAuthCapturePreset[]
  existingNames: Set<string>
  onCreated: (name: string) => void
}) {
  const toast = useToast()
  const [preset, setPreset] = useState<string>('claude-code')
  const [name, setName] = useState('')

  const createMut = useMutation({
    mutationFn: (body: { name: string; preset: string }) =>
      oauthCaptureApi.createProfile({ ...body, enabled: true }),
    onSuccess: (p) => {
      toast.success(`Profile ${p.name} created — secret is in the detail panel`)
      onCreated(p.name)
      setName('')
    },
    onError: (e: Error) => toast.error(`Create failed: ${e.message}`),
  })

  const selectedPreset = presets.find(p => p.key === preset)
  const suggestedName = selectedPreset
    ? selectedPreset.key + '-' + new Date().toISOString().slice(0, 10).replace(/-/g, '')
    : ''

  function handleCreate() {
    const finalName = (name || suggestedName).trim()
    if (!finalName) return toast.error('Profile name required')
    if (existingNames.has(finalName)) return toast.error('A profile with that name already exists')
    createMut.mutate({ name: finalName, preset })
  }

  return (
    <Card>
      <CardHeader><CardTitle>New capture profile</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        <div>
          <label className="text-xs font-medium text-gray-600 dark:text-gray-400">CLI / vendor</label>
          <select
            value={preset}
            onChange={(e) => setPreset(e.target.value)}
            className="mt-1 w-full rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 px-2 py-1.5 text-sm"
          >
            {presets.map(p => (
              <option key={p.key} value={p.key}>{p.label}</option>
            ))}
          </select>
          {selectedPreset?.cli_hint && (
            <p className="mt-1 text-xs text-gray-400">CLI: {selectedPreset.cli_hint}</p>
          )}
          {selectedPreset?.setup_hint && (
            <p className="mt-1 text-xs text-gray-400">{selectedPreset.setup_hint}</p>
          )}
        </div>
        <Input
          label="Profile name"
          placeholder={suggestedName}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button onClick={handleCreate} loading={createMut.isPending} className="w-full">
          <Plus className="h-4 w-4 mr-1.5" /> Create + enable
        </Button>
        <p className="text-xs text-gray-400">
          A unique capture secret is generated automatically. You'll see it exactly once
          after creation.
        </p>
      </CardContent>
    </Card>
  )
}
