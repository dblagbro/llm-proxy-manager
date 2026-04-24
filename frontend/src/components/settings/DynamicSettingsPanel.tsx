import { useMemo } from 'react'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Switch } from '@/components/ui/Switch'
import type { SettingSchemaItem } from '@/api'

// Keys that are already rendered by the hand-written cards above this panel.
// New schema entries that aren't in this list get auto-rendered, grouped by
// schema's "group" field.
const HAND_CODED_KEYS = new Set<string>([
  // CoT-E (Chain-of-Thought) card
  'cot_enabled',
  'cot_max_iterations',
  'cot_quality_threshold',
  'cot_critique_max_tokens',
  'cot_plan_max_tokens',
  'cot_min_tokens_skip',
  'cot_verify_enabled',
  'cot_verify_max_tokens',
  'cot_verify_auto_detect',
  'cot_cross_provider_critique',
  'cot_verify_execute',
  'cot_verify_step_timeout_sec',
  'cot_plan_compact',
  'fallback_enabled',
  'fallback_max_providers',
  'task_auto_detect_enabled',
  'shadow_traffic_rate',
  'shadow_candidate_provider_id',
  'structured_output_enabled',
  'structured_output_max_repairs',
  'vision_route_enabled',
  'semantic_cache_enabled',
  'semantic_cache_threshold',
  'semantic_cache_ttl_sec',
  'semantic_cache_min_response_chars',
  'hedge_enabled',
  'hedge_max_per_sec',
  // Native reasoning card
  'native_thinking_budget_tokens',
  'native_reasoning_effort',
  // Circuit breaker card
  'circuit_breaker_threshold',
  'circuit_breaker_timeout_sec',
  'circuit_breaker_halfopen_sec',
  'circuit_breaker_success_needed',
  'hold_down_sec',
  // Email card
  'smtp_enabled',
  'smtp_host',
  'smtp_port',
  'smtp_from',
  'smtp_to',
])

type SettingsMap = Record<string, unknown>
type Props = {
  schema: SettingSchemaItem[]
  form: SettingsMap
  setForm: React.Dispatch<React.SetStateAction<SettingsMap | null>>
}

export function DynamicSettingsPanel({ schema, form, setForm }: Props) {
  const groups = useMemo(() => {
    const extras = schema.filter(s => !HAND_CODED_KEYS.has(s.key))
    const grouped = new Map<string, SettingSchemaItem[]>()
    for (const s of extras) {
      const g = s.group || 'General'
      const list = grouped.get(g) ?? []
      list.push(s)
      grouped.set(g, list)
    }
    // Sort groups alphabetically, with OAuth capture / Privacy / Audit
    // grouped above SSO purely for a sensible default reading order.
    const order = ['Privacy', 'OAuth capture', 'Audit export', 'SSO']
    return [...grouped.entries()].sort(([a], [b]) => {
      const ai = order.indexOf(a); const bi = order.indexOf(b)
      if (ai >= 0 && bi >= 0) return ai - bi
      if (ai >= 0) return -1
      if (bi >= 0) return 1
      return a.localeCompare(b)
    })
  }, [schema])

  if (groups.length === 0) return null

  const coerce = (val: unknown, type: SettingSchemaItem['type']) => {
    if (type === 'int') return Number(val)
    if (type === 'float') return Number(val)
    if (type === 'bool') return Boolean(val)
    return String(val)
  }

  return (
    <>
      {groups.map(([groupName, items]) => (
        <Card key={groupName}>
          <CardHeader><CardTitle>{groupName}</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {items.map(item => (
              <SettingField
                key={item.key}
                item={item}
                value={form[item.key]}
                onChange={(v) =>
                  setForm(prev => ({ ...(prev ?? {}), [item.key]: coerce(v, item.type) }))
                }
              />
            ))}
          </CardContent>
        </Card>
      ))}
    </>
  )
}

function SettingField({
  item, value, onChange,
}: {
  item: SettingSchemaItem
  value: unknown
  onChange: (v: unknown) => void
}) {
  if (item.type === 'bool') {
    return (
      <div className="flex items-start justify-between gap-4">
        <div className="flex-1">
          <p className="text-sm font-medium text-gray-800 dark:text-gray-200">{item.label}</p>
          {item.help && <p className="text-xs text-gray-400 mt-0.5">{item.help}</p>}
        </div>
        <Switch checked={Boolean(value)} onChange={onChange} />
      </div>
    )
  }

  const inputType = item.secret ? 'password' : (item.type === 'int' || item.type === 'float') ? 'number' : 'text'

  return (
    <div>
      <Input
        label={item.label}
        type={inputType}
        value={value == null ? '' : String(value)}
        onChange={(e) => onChange((e.target as HTMLInputElement).value)}
        // Secrets are pre-filled with a mask from the backend; clearing the
        // field is how the user says "don't change it". Leaving the mask in
        // is fine too — the backend drops it from PUT bodies.
        autoComplete={item.secret ? 'new-password' : 'off'}
      />
      {item.help && <p className="text-xs text-gray-400 mt-1">{item.help}</p>}
    </div>
  )
}
