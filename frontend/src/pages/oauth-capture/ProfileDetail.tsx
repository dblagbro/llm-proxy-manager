import { useState, useMemo } from 'react'
import { useMutation } from '@tanstack/react-query'
import { Copy, Trash2, Play, Pause, Eye, EyeOff, KeyRound } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import {
  oauthCaptureApi,
  type OAuthCaptureProfile,
  type OAuthCapturePreset,
} from '@/api'
import { LiveCaptureTail } from './LiveCaptureTail'
import { TerminalPane } from './TerminalPane'

export function ProfileDetail({
  profile, presets, onChanged,
}: {
  profile: OAuthCaptureProfile
  presets: OAuthCapturePreset[]
  onChanged: () => void
}) {
  const toast = useToast()
  const [secret, setSecret] = useState<string | null>(null)
  const [showSecret, setShowSecret] = useState(false)

  const preset = presets.find(p => p.key === profile.preset) ?? null
  const captureUrlBase = `${window.location.origin}${import.meta.env.BASE_URL.replace(/\/$/, '')}/api/oauth-capture/${profile.name}`

  const toggleMut = useMutation({
    mutationFn: () => oauthCaptureApi.updateProfile(profile.name, { enabled: !profile.enabled }),
    onSuccess: () => {
      toast.success(profile.enabled ? 'Capture paused' : 'Capture started')
      onChanged()
    },
  })

  const rotateMut = useMutation({
    mutationFn: () => oauthCaptureApi.updateProfile(profile.name, { rotate_secret: true }),
    onSuccess: (p) => {
      setSecret(p.secret ?? null)
      setShowSecret(true)
      toast.success('Secret rotated — old one is invalidated')
      onChanged()
    },
  })

  const revealMut = useMutation({
    mutationFn: () => oauthCaptureApi.revealSecret(profile.name),
    onSuccess: (r) => {
      setSecret(r.secret)
      setShowSecret(true)
    },
  })

  const deleteMut = useMutation({
    mutationFn: () => oauthCaptureApi.deleteProfile(profile.name),
    onSuccess: () => {
      toast.success('Profile deleted')
      onChanged()
    },
  })

  function copy(text: string, label = 'Copied') {
    navigator.clipboard.writeText(text).then(() => toast.success(label))
  }

  const envBlock = useMemo(() => {
    if (!secret) return '# Click "Reveal secret" first'
    const envs = (preset?.env_var_names ?? ['ANTHROPIC_BASE_URL']).map(
      (v) => `export ${v}="${captureUrlBase}?cap=${secret}"`
    )
    return envs.join('\n')
  }, [secret, preset, captureUrlBase])

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between">
          <CardTitle>{profile.name}</CardTitle>
          <div className="flex items-center gap-2">
            <Button
              onClick={() => toggleMut.mutate()}
              variant={profile.enabled ? 'secondary' : 'primary'}
              loading={toggleMut.isPending}
            >
              {profile.enabled
                ? <><Pause className="h-4 w-4 mr-1.5" />Pause</>
                : <><Play className="h-4 w-4 mr-1.5" />Start capture</>}
            </Button>
            <Button
              onClick={() => {
                if (confirm(`Delete profile ${profile.name}? This also wipes its capture logs.`)) {
                  deleteMut.mutate()
                }
              }}
              variant="danger"
              loading={deleteMut.isPending}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* Secret management */}
        <section>
          <div className="flex items-center justify-between mb-1">
            <div className="text-sm font-medium text-gray-800 dark:text-gray-200">Capture secret</div>
            <div className="flex items-center gap-1.5">
              <Button onClick={() => revealMut.mutate()} variant="secondary" size="sm" loading={revealMut.isPending}>
                <Eye className="h-3.5 w-3.5 mr-1" /> Reveal
              </Button>
              <Button onClick={() => rotateMut.mutate()} variant="secondary" size="sm" loading={rotateMut.isPending}>
                <KeyRound className="h-3.5 w-3.5 mr-1" /> Rotate
              </Button>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <code className="flex-1 block text-xs font-mono bg-gray-100 dark:bg-gray-800 px-2 py-2 rounded overflow-hidden text-ellipsis">
              {showSecret && secret ? secret : '••••••••••••••••••••••••••••••••'}
            </code>
            {secret && (
              <Button onClick={() => copy(secret, 'Secret copied')} variant="secondary" size="sm">
                <Copy className="h-3.5 w-3.5" />
              </Button>
            )}
            {secret && (
              <Button onClick={() => setShowSecret(!showSecret)} variant="secondary" size="sm">
                {showSecret ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
              </Button>
            )}
          </div>
        </section>

        {/* Setup block */}
        <section>
          <div className="text-sm font-medium text-gray-800 dark:text-gray-200 mb-1">
            Workstation setup
          </div>
          <p className="text-xs text-gray-400 mb-2">
            Paste this into a shell, then run {preset?.cli_hint ?? 'your CLI'}. Every request is recorded below.
          </p>
          <div className="relative">
            <pre className="text-xs font-mono bg-gray-900 text-gray-100 p-3 rounded overflow-x-auto">
{envBlock}
            </pre>
            {secret && (
              <Button
                onClick={() => copy(envBlock, 'Env block copied')}
                variant="secondary"
                size="sm"
                className="absolute top-2 right-2"
              >
                <Copy className="h-3.5 w-3.5" />
              </Button>
            )}
          </div>
        </section>

        {/* v2.6.0 — In-browser terminal (hidden when preset has no login_cmd) */}
        <TerminalPane profile={profile} preset={preset} />

        {/* Live tail */}
        <LiveCaptureTail profile={profile} />
      </CardContent>
    </Card>
  )
}
