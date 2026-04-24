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
 *
 * The four sub-components live under ./oauth-capture/ and are imported here.
 * Split on 2026-04-24 so no single page file carries multiple responsibilities.
 */
import { useState, useMemo } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  oauthCaptureApi,
  type OAuthCaptureProfile,
  type OAuthCapturePreset,
} from '@/api'
import { Card, CardContent } from '@/components/ui/Card'
import { NewProfileWizard } from './oauth-capture/NewProfileWizard'
import { ProfileList } from './oauth-capture/ProfileList'
import { ProfileDetail } from './oauth-capture/ProfileDetail'

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
          />
        </div>

        {/* Right: detail panel */}
        <div className="col-span-7">
          {currentProfile ? (
            <ProfileDetail
              profile={currentProfile}
              presets={presets ?? []}
              onChanged={() => refetchProfiles()}
            />
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
