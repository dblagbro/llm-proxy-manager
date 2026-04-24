/**
 * Admin → OAuth Capture (v2.5.0)
 * Multi-vendor capture wizard for reverse-engineering CLI OAuth flows.
 *
 * Flow:
 *   1. Admin picks a preset (Claude Code / OpenAI codex / GitHub Copilot / …)
 *      and gives the profile a name.
 *   2. Server creates the profile + secret. UI shows copy-paste env block.
 *   3. Admin enables the profile and runs the CLI on their workstation.
 *   4. Requests stream into the Live Capture panel via SSE.
 *   5. When done, admin exports NDJSON for offline reverse-eng or stops capture.
 */
import { useState, useEffect, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Plus, RefreshCw, Copy, Trash2, Play, Pause, Download, Eye, EyeOff, KeyRound,
} from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { useToast } from '@/components/ui/Toast'
import {
  oauthCaptureApi,
  type OAuthCaptureProfile,
  type OAuthCapturePreset,
  type OAuthCaptureLogEntry,
} from '@/api'

export function OAuthCapturePage() {
  const qc = useQueryClient()
  const [selectedProfile, setSelectedProfile] = useState<string | null>(null)

  const { data: presets } = useQuery<OAuthCapturePreset[]>({
    queryKey: ['oauth-capture', 'presets'],
    queryFn: oauthCaptureApi.listPresets,
  })

  const { data: profiles, refetch: refetchProfiles } = useQuery<OAuthCaptureProfile[]>({
    queryKey: ['oauth-capture', 'profiles'],
    queryFn: oauthCaptureApi.listProfiles,
  })

  const currentProfile = useMemo(
    () => profiles?.find(p => p.name === selectedProfile) ?? null,
    [profiles, selectedProfile]
  )

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">
          OAuth Capture
        </h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Reverse-engineer CLI OAuth flows (claude-code, codex, gh copilot, gcloud …) by
          recording their traffic as they log in, then use the captured transcripts to
          implement a direct <code className="text-xs font-mono">*-oauth</code> provider type.
        </p>
      </div>

      <div className="grid grid-cols-12 gap-6">
        {/* Left: profile list + wizard */}
        <div className="col-span-5 space-y-4">
          <NewProfileWizard
            presets={presets ?? []}
            existingNames={new Set((profiles ?? []).map(p => p.name))}
            onCreated={(name) => {
              qc.invalidateQueries({ queryKey: ['oauth-capture', 'profiles'] })
              setSelectedProfile(name)
            }}
          />
          <ProfileList
            profiles={profiles ?? []}
            selected={selectedProfile}
            onSelect={setSelectedProfile}
            onChanged={() => refetchProfiles()}
          />
        </div>

        {/* Right: detail panel */}
        <div className="col-span-7">
          {currentProfile ? (
            <ProfileDetail profile={currentProfile} presets={presets ?? []} onChanged={() => refetchProfiles()} />
          ) : (
            <Card>
              <CardContent className="py-12 text-center text-gray-400 text-sm">
                Select or create a capture profile to begin.
              </CardContent>
            </Card>
          )}
        </div>
      </div>
    </div>
  )
}


// ── New profile wizard ───────────────────────────────────────────────────────

function NewProfileWizard({
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
            className="mt-1 w-full rounded border border-gray-300 dark:border-gray-700 bg-white dark:bg-gray-800 px-2 py-1.5 text-sm"
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


// ── Profile list ─────────────────────────────────────────────────────────────

function ProfileList({
  profiles, selected, onSelect,
}: {
  profiles: OAuthCaptureProfile[]
  selected: string | null
  onSelect: (name: string) => void
  onChanged: () => void   // still in the prop signature so callers don't break; unused here
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


// ── Profile detail (env block + live capture tail) ───────────────────────────

function ProfileDetail({
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

        {/* Live tail */}
        <LiveCaptureTail profile={profile} />
      </CardContent>
    </Card>
  )
}


// ── Live capture tail (SSE) ─────────────────────────────────────────────────

function LiveCaptureTail({ profile }: { profile: OAuthCaptureProfile }) {
  const toast = useToast()
  const [entries, setEntries] = useState<OAuthCaptureLogEntry[]>([])
  const [streaming, setStreaming] = useState(false)

  const { data: initial, refetch } = useQuery<OAuthCaptureLogEntry[]>({
    queryKey: ['oauth-capture', 'log', profile.name],
    queryFn: () => oauthCaptureApi.listLog(profile.name, 50),
    enabled: !!profile.name,
  })

  useEffect(() => {
    if (initial) setEntries(initial)
  }, [initial])

  // SSE stream
  useEffect(() => {
    if (!streaming || !profile.enabled) return
    const url = `${import.meta.env.BASE_URL.replace(/\/$/, '')}${oauthCaptureApi.streamUrl(profile.name)}`
    const es = new EventSource(url, { withCredentials: true })
    es.onmessage = (msg) => {
      try {
        const entry: OAuthCaptureLogEntry = JSON.parse(msg.data)
        setEntries(prev => [entry, ...prev].slice(0, 200))
      } catch { /* ignore non-JSON pings */ }
    }
    es.onerror = () => {
      setStreaming(false)
      es.close()
    }
    return () => es.close()
  }, [streaming, profile.name, profile.enabled])

  const clearMut = useMutation({
    mutationFn: () => oauthCaptureApi.clearLog(profile.name),
    onSuccess: () => {
      setEntries([])
      toast.success('Log cleared')
    },
  })

  const exportUrl = `${import.meta.env.BASE_URL.replace(/\/$/, '')}${oauthCaptureApi.exportUrl(profile.name)}`

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-gray-800 dark:text-gray-200">
          Live captures ({entries.length})
        </div>
        <div className="flex items-center gap-1.5">
          <Button onClick={() => setStreaming(!streaming)} variant="secondary" size="sm">
            {streaming ? <><Pause className="h-3.5 w-3.5 mr-1" />Stop tail</> : <><Play className="h-3.5 w-3.5 mr-1" />Start tail</>}
          </Button>
          <Button onClick={() => refetch()} variant="secondary" size="sm">
            <RefreshCw className="h-3.5 w-3.5" />
          </Button>
          <a href={exportUrl} download={`captures-${profile.name}.ndjson`}>
            <Button variant="secondary" size="sm">
              <Download className="h-3.5 w-3.5 mr-1" /> NDJSON
            </Button>
          </a>
          <Button
            onClick={() => {
              if (confirm(`Clear all captures for ${profile.name}?`)) clearMut.mutate()
            }}
            variant="danger"
            size="sm"
            loading={clearMut.isPending}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </Button>
        </div>
      </div>

      <div className="border border-gray-200 dark:border-gray-700 rounded max-h-[400px] overflow-y-auto">
        {entries.length === 0 ? (
          <div className="p-4 text-center text-sm text-gray-400">
            No captures yet. Start the tail, then run your CLI.
          </div>
        ) : (
          <table className="w-full text-xs">
            <thead className="bg-gray-50 dark:bg-gray-800 text-left">
              <tr>
                <th className="px-2 py-1 font-medium">Time</th>
                <th className="px-2 py-1 font-medium">Method</th>
                <th className="px-2 py-1 font-medium">Path</th>
                <th className="px-2 py-1 font-medium">Status</th>
                <th className="px-2 py-1 font-medium">Latency</th>
              </tr>
            </thead>
            <tbody>
              {entries.map(e => (
                <tr key={e.id} className="border-t border-gray-100 dark:border-gray-700 hover:bg-gray-50 dark:hover:bg-gray-800/50">
                  <td className="px-2 py-1 font-mono">{e.created_at?.slice(11, 19)}</td>
                  <td className="px-2 py-1 font-mono">{e.method}</td>
                  <td className="px-2 py-1 font-mono truncate max-w-[280px]">{e.path}</td>
                  <td className={
                    'px-2 py-1 font-mono ' +
                    (e.resp_status && e.resp_status < 400 ? 'text-green-600' :
                     e.resp_status && e.resp_status >= 400 ? 'text-red-600' : 'text-gray-400')
                  }>
                    {e.resp_status ?? (e.error ? 'err' : '—')}
                  </td>
                  <td className="px-2 py-1 font-mono text-gray-500">{Math.round(e.latency_ms)}ms</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  )
}
