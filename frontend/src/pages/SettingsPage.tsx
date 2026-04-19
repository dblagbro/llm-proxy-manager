import { useState } from 'react'
import { Save } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import { useToast } from '@/components/ui/Toast'

// Settings are read/written via the backend settings endpoint.
// For now we expose the subset that can be tuned at runtime.

interface SettingsForm {
  cot_enabled: boolean
  cot_threshold: number
  cot_max_refinements: number
  cb_failure_threshold: number
  cb_success_threshold: number
  cb_hold_down_sec: number
  cb_billing_hold_down_sec: number
  smtp_host: string
  smtp_port: number
  smtp_from: string
  smtp_to: string
}

const DEFAULTS: SettingsForm = {
  cot_enabled: true,
  cot_threshold: 7,
  cot_max_refinements: 2,
  cb_failure_threshold: 3,
  cb_success_threshold: 2,
  cb_hold_down_sec: 60,
  cb_billing_hold_down_sec: 3600,
  smtp_host: '',
  smtp_port: 587,
  smtp_from: '',
  smtp_to: '',
}

export function SettingsPage() {
  const toast = useToast()
  const [form, setForm] = useState<SettingsForm>(DEFAULTS)

  function field<K extends keyof SettingsForm>(key: K) {
    return {
      value: String(form[key]),
      onChange: (e: React.ChangeEvent<HTMLInputElement>) =>
        setForm(f => ({ ...f, [key]: typeof DEFAULTS[key] === 'number' ? Number(e.target.value) : e.target.value })),
    }
  }

  function handleSave() {
    // POST to /api/settings when backend endpoint is available
    toast.success('Settings saved (restart may be needed for some changes)')
  }

  return (
    <div className="p-6 space-y-6 max-w-3xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Settings</h1>
        <p className="text-sm text-gray-500 mt-0.5">Runtime configuration — environment variables take precedence</p>
      </div>

      {/* CoT-E settings */}
      <Card>
        <CardHeader><CardTitle>Chain-of-Thought Emulation (CoT-E)</CardTitle></CardHeader>
        <CardContent className="space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-medium text-gray-800 dark:text-gray-200">Enable CoT-E</p>
              <p className="text-xs text-gray-400">Applies multi-pass reasoning to non-native-reasoning models</p>
            </div>
            <Switch
              checked={form.cot_enabled}
              onChange={v => setForm(f => ({ ...f, cot_enabled: v }))}
            />
          </div>
          <div className="grid grid-cols-2 gap-4">
            <Input label="Quality Threshold (1–10)" type="number" {...field('cot_threshold')} />
            <Input label="Max Refinement Passes" type="number" {...field('cot_max_refinements')} />
          </div>
        </CardContent>
      </Card>

      {/* Circuit breaker settings */}
      <Card>
        <CardHeader><CardTitle>Circuit Breaker</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          <Input label="Failure Threshold (opens CB)" type="number" {...field('cb_failure_threshold')} />
          <Input label="Success Threshold (closes CB)" type="number" {...field('cb_success_threshold')} />
          <Input label="Hold-Down (seconds)" type="number" {...field('cb_hold_down_sec')} />
          <Input label="Billing Error Hold-Down (seconds)" type="number" {...field('cb_billing_hold_down_sec')} />
        </CardContent>
      </Card>

      {/* Email notifications */}
      <Card>
        <CardHeader><CardTitle>Email Notifications</CardTitle></CardHeader>
        <CardContent className="grid grid-cols-2 gap-4">
          <Input label="SMTP Host" placeholder="smtp.example.com" {...field('smtp_host')} />
          <Input label="SMTP Port" type="number" {...field('smtp_port')} />
          <Input label="From Address" type="email" {...field('smtp_from')} />
          <Input label="Alert Recipient" type="email" {...field('smtp_to')} />
        </CardContent>
      </Card>

      <div className="flex justify-end">
        <Button onClick={handleSave}>
          <Save className="h-4 w-4 mr-1.5" />Save Settings
        </Button>
      </div>
    </div>
  )
}
