import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, RefreshCw } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { useToast } from '@/components/ui/Toast'
import { settingsApi } from '@/api'

type SettingsMap = Record<string, unknown>

export function SettingsPage() {
  const toast = useToast()
  const qc = useQueryClient()
  const [form, setForm] = useState<SettingsMap | null>(null)

  const { data: serverSettings, isLoading } = useQuery<SettingsMap>({
    queryKey: ['settings'],
    queryFn: settingsApi.get,
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
            <Input label="Max refinement passes" type="number" {...numField('cot_max_iterations')} min={0} max={5} />
            <Input label="Quality threshold (1–10)" type="number" {...numField('cot_quality_threshold')} min={1} max={10} />
            <Input label="Min draft tokens to skip refinement" type="number" {...numField('cot_min_tokens_skip')} min={0} />
            <Input label="Critique max tokens" type="number" {...numField('cot_critique_max_tokens')} min={50} max={500} />
          </div>
          <p className="text-xs text-gray-400">
            <strong>Min draft tokens:</strong> When the initial draft exceeds this count, critique/refinement is
            skipped — the answer is already thorough. Set to 0 to always refine.
          </p>
        </CardContent>
      </Card>

      {/* Circuit breaker */}
      <Card>
        <CardHeader><CardTitle>Circuit Breaker</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          <Input label="Failure threshold (opens CB)" type="number" {...numField('circuit_breaker_threshold')} min={1} />
          <Input label="Successes to close CB" type="number" {...numField('circuit_breaker_success_needed')} min={1} />
          <Input label="Open timeout (seconds)" type="number" {...numField('circuit_breaker_timeout_sec')} min={10} />
          <Input label="Half-open window (seconds)" type="number" {...numField('circuit_breaker_halfopen_sec')} min={5} />
          <Input label="Provider hold-down (seconds)" type="number" {...numField('hold_down_sec')} min={0} />
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
            <Input label="SMTP host" placeholder="smtp.example.com" {...strField('smtp_host')} />
            <Input label="SMTP port" type="number" {...numField('smtp_port')} />
            <Input label="From address" type="email" {...strField('smtp_from')} />
            <Input label="Alert recipient" type="email" {...strField('smtp_to')} />
          </div>
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={() => saveMut.mutate(form)} loading={saveMut.isPending}>
          <Save className="h-4 w-4 mr-1.5" />Save Settings
        </Button>
      </div>
    </div>
  )
}
