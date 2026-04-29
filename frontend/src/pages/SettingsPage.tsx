import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { useToast } from '@/components/ui/Toast'
import { settingsApi, type SettingSchemaItem } from '@/api'
import { ClusterDiffPanel } from '@/components/settings/ClusterDiffPanel'
import { DynamicSettingsPanel } from '@/components/settings/DynamicSettingsPanel'
import { UserPreferencesCard } from '@/components/settings/UserPreferencesCard'

type SettingsMap = Record<string, unknown>

export function SettingsPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const [form, setForm] = useState<SettingsMap | null>(null)

  const { data: serverSettings, isLoading } = useQuery<SettingsMap>({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
  })

  const { data: schema } = useQuery<SettingSchemaItem[]>({
    queryKey: ['settings', 'schema'],
    queryFn: settingsApi.schema,
  })

  // Populate form once on first load (don't overwrite in-progress edits)
  useEffect(() => {
    if (serverSettings && !form) {
      setForm(serverSettings)
    }
  }, [serverSettings, form])

  const saveMut = useMutation({
    mutationFn: (data: SettingsMap) => settingsApi.save(data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['settings'] })
      toast.success('Settings saved and applied live')
    },
    onError: (e: Error) => toast.error(`Save failed: ${e.message}`),
  })

  function numField(key: string): React.InputHTMLAttributes<HTMLInputElement> {
    return {
      value: String(form?.[key] ?? ''),
      onChange: (e) => setForm(f => ({ ...f!, [key]: Number(e.target.value) })),
    }
  }
  function boolField(key: string): { checked: boolean; onChange: (v: boolean) => void } {
    return {
      checked: Boolean(form?.[key] ?? true),
      onChange: (v: boolean) => setForm(f => ({ ...f!, [key]: v })),
    }
  }
  function strField(key: string): React.InputHTMLAttributes<HTMLInputElement> {
    return {
      value: String(form?.[key] ?? ''),
      onChange: (e) => setForm(f => ({ ...f!, [key]: e.target.value })),
    }
  }

  if (isLoading || !form) {
    return (
      <div className="p-6 flex items-center gap-2 text-gray-400">
        <RefreshCw className="h-4 w-4 animate-spin" /> Loading settings…
      </div>
    )
  }

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
        <p className="text-sm text-gray-500 mt-0.5">
          Changes apply live — no restart required. Environment variables remain the defaults.
        </p>
      </div>

      {/* Per-user display prefs (timezone, time format) — saved per user, not cluster-global */}
      <UserPreferencesCard />

      {/* CoT-E */}
      <Card>
        <CardHeader><CardTitle>Chain-of-Thought Emulation (CoT-E)</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">Enable CoT-E globally</p>
              <p className="text-xs text-gray-400">
                Multi-pass reasoning for non-native-thinking providers (claude-code keys &amp; reasoning hints)
              </p>
            </div>
            <Switch {...boolField('cot_enabled')} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Input
              label="Max refinement passes"
              type="number"
              tooltip="Upper bound on critique→refine iterations per request. Each pass costs one extra LLM call. The loop also stops early when the critique scores the draft at or above the quality threshold. 0 disables refinement entirely (draft is returned as-is)."
              {...numField('cot_max_iterations')}
              min={0}
              max={5}
            />
            <Input
              label="Quality threshold (1–10)"
              type="number"
              tooltip="Score the critique pass must give the draft (on a 1–10 rubric) for refinement to stop early. Higher = stricter, more passes consumed. Typical: 7–8. At 10 the loop will almost always run to max passes."
              {...numField('cot_quality_threshold')}
              min={1}
              max={10}
            />
            <Input
              label="Min draft tokens to skip refinement"
              type="number"
              tooltip="If the initial draft is at least this many output tokens, the critique/refine cycle is skipped — long answers are assumed thorough. Set to 0 to always refine regardless of draft length."
              {...numField('cot_min_tokens_skip')}
              min={0}
            />
            <Input
              label="Critique max tokens"
              type="number"
              tooltip="Token cap for each critique pass (the LLM grades the draft and proposes fixes). Lower = cheaper but may truncate detailed feedback. Range 50–500; 200 is a sensible default."
              {...numField('cot_critique_max_tokens')}
              min={50}
              max={500}
            />
          </div>

          <div className="border-t border-gray-200 dark:border-gray-700 pt-4 space-y-4">
            <p className="text-xs font-semibold text-gray-500 dark:text-gray-400 uppercase tracking-wide">Verification Pass</p>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-800 dark:text-gray-200">Enable verification pass</p>
                <p className="text-xs text-gray-400">
                  After refining, generates shell/API commands that confirm the answer is correct.
                  Adds one LLM call. Can also be forced per-request with <code className="font-mono text-indigo-400">X-Cot-Verify: true</code>.
                </p>
              </div>
              <Switch {...boolField('cot_verify_enabled')} />
            </div>
            <div className="flex items-center justify-between">
              <div>
                <p className="text-sm font-medium text-gray-800 dark:text-gray-200">Auto-detect shell commands</p>
                <p className="text-xs text-gray-400">
                  Only verify answers that contain shell code blocks or infrastructure CLI tools.
                  Turn off to verify every CoT response.
                </p>
              </div>
              <Switch {...boolField('cot_verify_auto_detect')} />
            </div>
            <Input
              label="Verification max tokens"
              type="number"
              tooltip="Token cap for the verification pass that emits shell/API checks confirming the answer is correct. Higher = room for more checks; lower = cheaper. Range 100–800; 400 is a balanced default."
              {...numField('cot_verify_max_tokens')}
              min={100}
              max={800}
            />
          </div>
        </CardContent>
      </Card>

      {/* Native Reasoning */}
      <Card>
        <CardHeader><CardTitle>Native Reasoning</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <p className="text-xs text-gray-400">
            Applied automatically when routing to providers with native thinking capability
            (Gemini 2.5, OpenAI o-series). Anthropic extended-thinking passes the client's
            own <code className="font-mono text-indigo-400">thinking</code> block through unchanged.
          </p>
          <div className="grid grid-cols-2 gap-4">
            <Input
              label="Thinking budget tokens (Gemini 2.5)"
              type="number"
              tooltip="Maximum tokens Gemini 2.5 may spend on internal thinking before producing the answer. Higher = better quality on hard reasoning, more cost/latency. Range 1024–32768; 8192 is a typical default."
              {...numField('native_thinking_budget_tokens')}
              min={1024}
              max={32768}
            />
            <Input
              label="Reasoning effort (o-series: low / medium / high)"
              tooltip="Controls how much hidden reasoning OpenAI o-series models perform. 'low' is fastest/cheapest, 'high' is most thorough. Ignored for non-o-series providers."
              {...strField('native_reasoning_effort')}
              placeholder="medium"
            />
          </div>
        </CardContent>
      </Card>

      {/* Circuit breaker */}
      <Card>
        <CardHeader><CardTitle>Circuit Breaker</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          <Input
            label="Failure threshold (opens CB)"
            type="number"
            tooltip="Consecutive failures on a provider that flip the circuit breaker to OPEN. While OPEN the provider is skipped by routing. Lower = more aggressive isolation; higher = more retries before giving up."
            {...numField('circuit_breaker_threshold')}
            min={1}
          />
          <Input
            label="Successes to close CB"
            type="number"
            tooltip="Consecutive successes during the HALF-OPEN trial window required to close the breaker again. Higher = more conservative recovery."
            {...numField('circuit_breaker_success_needed')}
            min={1}
          />
          <Input
            label="Open timeout (seconds)"
            type="number"
            tooltip="How long the breaker stays fully OPEN after tripping before transitioning to HALF-OPEN to test recovery. The provider is hard-skipped during this window."
            {...numField('circuit_breaker_timeout_sec')}
            min={10}
          />
          <Input
            label="Half-open window (seconds)"
            type="number"
            tooltip="Length of the HALF-OPEN trial window during which a small number of probe requests are allowed to test if the provider has recovered."
            {...numField('circuit_breaker_halfopen_sec')}
            min={5}
          />
          <Input
            label="Provider hold-down (seconds)"
            type="number"
            tooltip="Default cool-off (per provider) after a single non-fatal failure before it's eligible for routing again. Distinct from the breaker — this is a soft penalty on each failure. 0 disables hold-down."
            {...numField('hold_down_sec')}
            min={0}
          />
        </CardContent>
      </Card>

      {/* Email alerts */}
      <Card>
        <CardHeader><CardTitle>Email Alerts</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <p className="text-sm font-medium text-gray-800 dark:text-gray-200">Enable email alerts</p>
            <Switch {...boolField('smtp_enabled')} />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Input
              label="SMTP host"
              placeholder="smtp.example.com"
              tooltip="Hostname of your outbound SMTP relay (e.g. smtp.gmail.com, smtp.sendgrid.net). The proxy connects here when alert conditions fire."
              {...strField('smtp_host')}
            />
            <Input
              label="SMTP port"
              type="number"
              tooltip="TCP port of the SMTP relay. Common values: 587 (STARTTLS), 465 (SMTPS), 25 (legacy/internal)."
              {...numField('smtp_port')}
            />
            <Input
              label="From address"
              type="email"
              tooltip="Sender address used in the email's From: header. Some relays require this to match an authenticated identity."
              {...strField('smtp_from')}
            />
            <Input
              label="Alert recipient"
              type="email"
              tooltip="Where alert emails are delivered (provider down, budget exceeded, etc.). One address; for multiple, use a distribution list."
              {...strField('smtp_to')}
            />
          </div>
        </CardContent>
      </Card>

      {/* Auto-rendered extra groups (Privacy, OAuth capture, Audit export, SSO, …) */}
      {schema && <DynamicSettingsPanel schema={schema} form={form} setForm={setForm} />}

      <div className="flex justify-end">
        <Button onClick={() => saveMut.mutate(form)} loading={saveMut.isPending}>
          <Save className="h-4 w-4 mr-1.5" />Save Settings
        </Button>
      </div>

      <ClusterDiffPanel />
    </div>
  )
}
