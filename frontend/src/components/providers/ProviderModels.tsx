import { useState, useMemo } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Pencil, ChevronDown, ChevronRight, Search } from 'lucide-react'
import { providersApi } from '@/api'
import { Button } from '@/components/ui/Button'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { useToast } from '@/components/ui/Toast'
import { HelpHint } from '@/components/ui/HelpHint'
import type { ModelCapability } from '@/types'

// v3.1.5: collapse the model list by default when a provider has more than
// AUTO_COLLAPSE_THRESHOLD models. OpenRouter scans 367 models; rendering
// them all in a flat table makes the providers page scroll for thousands
// of pixels and disorients the operator. The search input lets you find a
// specific model id when the list is expanded.
const AUTO_COLLAPSE_THRESHOLD = 20

const TASKS = ['chat', 'reasoning', 'analysis', 'code', 'creative', 'vision', 'audio']
const MODALITIES = ['text', 'vision', 'audio', 'multimodal']

interface CapForm {
  tasks: string[]
  latency: string
  cost_tier: string
  safety: number
  context_length: number
  regions: string
  modalities: string[]
  native_reasoning: boolean
  native_tools: boolean
  native_vision: boolean
  // v3.5.1 — model-identity fields editable in the form so operators
  // can set canonical aliases / family / variant without a DB shell.
  // ``aliases`` is a comma-separated list in the form; converted back
  // to a string[] on save.
  aliases: string
  model_family: string
  model_variant: string
}

function capToForm(c: ModelCapability): CapForm {
  return {
    tasks: c.tasks,
    latency: c.latency,
    cost_tier: c.cost_tier,
    safety: c.safety,
    context_length: c.context_length,
    regions: (c.regions ?? []).join(', '),
    modalities: c.modalities,
    native_reasoning: c.native_reasoning,
    native_tools: c.native_tools,
    native_vision: c.native_vision,
    aliases: (c.aliases ?? []).join(', '),
    model_family: c.model_family ?? '',
    model_variant: c.model_variant ?? '',
  }
}

function Toggle({ label, checked, onChange, tooltip }: { label: string; checked: boolean; onChange: (v: boolean) => void; tooltip?: string }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
      />
      <span className="text-sm text-gray-700 dark:text-gray-300">{label}</span>
      {tooltip && <HelpHint text={tooltip} />}
    </label>
  )
}

function MultiCheck({ label, options, value, onChange, tooltip }: {
  label: string; options: string[]; value: string[]; onChange: (v: string[]) => void; tooltip?: string
}) {
  function toggle(opt: string) {
    onChange(value.includes(opt) ? value.filter(x => x !== opt) : [...value, opt])
  }
  return (
    <div>
      <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-1 flex items-center gap-1">
        <span>{label}</span>
        {tooltip && <HelpHint text={tooltip} />}
      </p>
      <div className="flex flex-wrap gap-2">
        {options.map(o => (
          <button
            key={o}
            onClick={() => toggle(o)}
            className={`px-2 py-0.5 rounded text-xs border transition-colors ${
              value.includes(o)
                ? 'bg-indigo-600 text-white border-indigo-600'
                : 'bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-400 border-gray-300 dark:border-gray-600 hover:border-indigo-400'
            }`}
          >
            {o}
          </button>
        ))}
      </div>
    </div>
  )
}

export function ProviderModels({ providerId }: { providerId: string }) {
  const qc = useQueryClient()
  const toast = useToast()
  const [editing, setEditing] = useState<ModelCapability | null>(null)
  const [form, setForm] = useState<CapForm | null>(null)
  const [expanded, setExpanded] = useState<boolean | null>(null)
  const [filter, setFilter] = useState('')

  const { data: caps, isLoading } = useQuery<ModelCapability[]>({
    queryKey: ['capabilities', providerId],
    queryFn: () => providersApi.capabilities(providerId),
  })

  // Default expanded state: open for short lists, collapsed for long ones.
  // null === user hasn't toggled, follow the threshold; once toggled, honor.
  const isExpanded = expanded ?? ((caps?.length ?? 0) <= AUTO_COLLAPSE_THRESHOLD)

  const filteredCaps = useMemo(() => {
    if (!caps || !filter.trim()) return caps ?? []
    const q = filter.trim().toLowerCase()
    return caps.filter(c => c.model_id.toLowerCase().includes(q))
  }, [caps, filter])

  const saveMutation = useMutation({
    mutationFn: (f: CapForm) => providersApi.updateCapability(providerId, editing!.model_id, {
      tasks: f.tasks,
      latency: f.latency as 'low' | 'medium' | 'high',
      cost_tier: f.cost_tier as 'economy' | 'standard' | 'premium',
      safety: Number(f.safety),
      context_length: Number(f.context_length),
      regions: f.regions.split(',').map(r => r.trim()).filter(Boolean),
      modalities: f.modalities,
      native_reasoning: f.native_reasoning,
      native_tools: f.native_tools,
      native_vision: f.native_vision,
      // v3.5.1 — model-identity fields. Empty strings → null for
      // family/variant; aliases empty → empty array (not the same
      // as null/missing — operator may explicitly clear aliases).
      aliases: f.aliases.split(',').map(a => a.trim()).filter(Boolean),
      model_family: f.model_family.trim() || null,
      model_variant: f.model_variant.trim() || null,
    }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['capabilities', providerId] })
      toast.success('Capability saved')
      setEditing(null)
    },
    onError: (e: Error) => toast.error(e.message),
  })

  function openEdit(c: ModelCapability) {
    setEditing(c)
    setForm(capToForm(c))
  }

  function set<K extends keyof CapForm>(key: K, value: CapForm[K]) {
    setForm(f => f ? { ...f, [key]: value } : f)
  }

  if (isLoading) return <div className="text-xs text-gray-400 py-2">Loading models…</div>

  if (!caps || caps.length === 0) {
    return (
      <p className="text-xs text-gray-400 py-2">
        No models indexed — click <strong>Scan Models</strong> to discover them.
      </p>
    )
  }

  return (
    <>
      <div className="mt-1">
        {/* v3.1.5: collapsible header. Click to expand/collapse the table —
            providers with hundreds of models (OpenRouter: 367) otherwise
            require thousands of pixels of scrolling. */}
        <button
          onClick={() => setExpanded(!isExpanded)}
          className="flex items-center gap-1 text-xs text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 mb-2 font-medium transition-colors"
        >
          {isExpanded ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
          <span>{caps.length} model{caps.length !== 1 ? 's' : ''} indexed</span>
          {!isExpanded && caps.length > AUTO_COLLAPSE_THRESHOLD && (
            <span className="text-gray-500 dark:text-gray-600 ml-1">— click to view</span>
          )}
        </button>

        {isExpanded && (
          <>
            {caps.length > AUTO_COLLAPSE_THRESHOLD && (
              <div className="relative mb-2">
                <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-gray-400 pointer-events-none" />
                <input
                  type="text"
                  value={filter}
                  onChange={e => setFilter(e.target.value)}
                  placeholder={`Filter ${caps.length} models by id…`}
                  className="w-full pl-7 pr-2 py-1 text-xs bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
                {filter && filteredCaps.length !== caps.length && (
                  <span className="absolute right-2 top-1/2 -translate-y-1/2 text-xs text-gray-400">
                    {filteredCaps.length} / {caps.length}
                  </span>
                )}
              </div>
            )}
            <div className="overflow-x-auto max-h-96 overflow-y-auto border border-gray-100 dark:border-gray-800 rounded">
              <table className="w-full text-xs border-collapse">
                <thead className="sticky top-0 bg-white dark:bg-gray-900">
                  <tr className="text-left text-gray-400 border-b border-gray-200 dark:border-gray-700">
                    <th className="pb-1 pt-1 pr-4 pl-2 font-medium">Model ID</th>
                    <th className="pb-1 pt-1 pr-4 font-medium">Tasks</th>
                    <th className="pb-1 pt-1 pr-4 font-medium">Cost</th>
                    <th className="pb-1 pt-1 pr-4 font-medium">Latency</th>
                    <th className="pb-1 pt-1 pr-4 font-medium">Context</th>
                    <th className="pb-1 pt-1 pr-4 font-medium">Features</th>
                    <th className="pb-1 pt-1 font-medium">Source</th>
                    <th className="pb-1 pt-1 pr-2" />
                  </tr>
                </thead>
                <tbody>
                  {filteredCaps.map(c => (
                    <tr key={c.id} className="border-b border-gray-100 dark:border-gray-800 last:border-0">
                      <td className="py-1 pr-4 pl-2 font-mono text-gray-700 dark:text-gray-300 whitespace-nowrap">{c.model_id}</td>
                      <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.tasks.join(', ') || '—'}</td>
                      <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.cost_tier}</td>
                      <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.latency}</td>
                      <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">
                        {c.context_length >= 1000 ? `${Math.round(c.context_length / 1000)}k` : c.context_length}
                      </td>
                      <td className="py-1 pr-4 text-gray-500 dark:text-gray-400 whitespace-nowrap">
                        {c.native_reasoning && <span title="Native reasoning" className="mr-1">🧠</span>}
                        {c.native_tools && <span title="Native tool use" className="mr-1">🔧</span>}
                        {c.native_vision && <span title="Native vision" className="mr-1">👁</span>}
                      </td>
                      <td className="py-1 pr-4">
                        <span className={`px-1.5 py-0.5 rounded text-xs ${c.source === 'manual' ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300' : 'bg-gray-100 text-gray-500 dark:text-gray-400 dark:bg-gray-800'}`}>
                          {c.source}
                        </span>
                      </td>
                      <td className="py-1 pr-2">
                        <button
                          onClick={() => openEdit(c)}
                          className="text-gray-400 hover:text-indigo-500 transition-colors"
                          title="Edit capabilities"
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </button>
                      </td>
                    </tr>
                  ))}
                  {filter && filteredCaps.length === 0 && (
                    <tr>
                      <td colSpan={8} className="py-2 px-2 text-center text-xs text-gray-400">
                        No models matching <span className="font-mono">{filter}</span>
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      {editing && form && (
        <Modal open onClose={() => setEditing(null)} size="lg">
          <ModalHeader onClose={() => setEditing(null)}>
            Edit Capabilities — <span className="font-mono text-sm">{editing.model_id}</span>
          </ModalHeader>
          <ModalBody>
            <div className="space-y-4">
              <MultiCheck
                label="Tasks"
                options={TASKS}
                value={form.tasks}
                onChange={v => set('tasks', v)}
                tooltip="Tags the LMRH router uses to match a request's task hint to candidate models. Pick all that apply. ‘chat’ is the safe default. ‘reasoning’ implies the model handles multi-step thinking; ‘code’ optimises for code generation; ‘vision’ requires native vision."
              />
              <MultiCheck
                label="Modalities"
                options={MODALITIES}
                value={form.modalities}
                onChange={v => set('modalities', v)}
                tooltip="What input/output types this model accepts. Most chat models are ‘text’. Add ‘vision’ for image-input models, ‘audio’ for STT/TTS-style endpoints. ‘multimodal’ is the catch-all for image+text+audio."
              />

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                    <span>Latency</span>
                    <HelpHint text="Coarse latency band used by LMRH scoring. ‘low’ for sub-2s p50 models (haiku, gpt-4o-mini, grok-fast). ‘medium’ for 2-5s (sonnet, gpt-4o). ‘high’ for 5s+ (opus, reasoning models). Doesn't affect routing alone — it's a tiebreaker when other dimensions match." />
                  </label>
                  <select
                    value={form.latency}
                    onChange={e => set('latency', e.target.value)}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  >
                    {['low', 'medium', 'high'].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                    <span>Cost tier</span>
                    <HelpHint text="Coarse cost band the router uses when LMRH ‘cost’ hint is set. ‘economy’ for cheapest options (haiku, mini, flash). ‘standard’ for everyday workhorses (sonnet, gpt-4o). ‘premium’ for expensive flagships (opus, gpt-4-turbo, gemini-pro)." />
                  </label>
                  <select
                    value={form.cost_tier}
                    onChange={e => set('cost_tier', e.target.value)}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  >
                    {['economy', 'standard', 'premium'].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                    <span>Safety level (1–5)</span>
                    <HelpHint text="How strict this model's safety filtering is. 1 = permissive (raw research models, ollama). 5 = highly filtered (Claude on platform.claude.com Pro). Used by LMRH ‘safety-min’ hint to require at least N — callers handling sensitive content set safety-min=4 to keep risky content off less-filtered routes." />
                  </label>
                  <input
                    type="number" min={1} max={5} value={form.safety}
                    onChange={e => set('safety', Number(e.target.value))}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                    <span>Context length (tokens)</span>
                    <HelpHint text="Maximum input + output tokens this model accepts. Used to filter candidates that physically can't serve a long prompt. Common: 128000 (gpt-4o, claude-sonnet-4-5), 200000 (claude opus), 1000000 (gemini 2.5)." />
                  </label>
                  <input
                    type="number" min={1000} value={form.context_length}
                    onChange={e => set('context_length', Number(e.target.value))}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 text-gray-900 dark:text-gray-100 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
              </div>

              <div>
                <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                  <span>Regions (comma-separated, blank = any)</span>
                  <HelpHint text="Geographic regions this model is hosted in. Lets LMRH ‘region’ hint route data-residency-sensitive workloads to the right place. Blank means ‘any region acceptable’ — the most common choice unless your callers have legal residency constraints." />
                </label>
                <input
                  type="text" value={form.regions} placeholder="us, eu, asia"
                  onChange={e => set('regions', e.target.value)}
                  className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>

              {/* v3.5.1 — model-identity fields. See docs/rfc/2026-05-model-identity.md */}
              <div className="border-t border-gray-200 dark:border-gray-700 pt-3">
                <p className="text-xs font-semibold text-gray-600 dark:text-gray-400 mb-2 flex items-center gap-1">
                  <span>Model identity (v3.5.0+)</span>
                  <HelpHint text="Optional metadata that lets LMRHv2 callers de-duplicate spelling variants of the same upstream model and pick between multi-route variants (e.g. grok-3 via the operator's grok.com web subscription vs via OpenRouter marketplace). See docs/rfc/2026-05-model-identity.md." />
                </p>
                <div>
                  <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                    <span>Aliases (comma-separated)</span>
                    <HelpHint text="Alternate spellings the proxy will accept and route to this same capability. Lets ‘grok-3’ and ‘x-ai/grok-3’ both resolve to the same Grok-3 model without listing it twice in /v1/models. Case-insensitive match. Leave blank if the canonical model_id is the only spelling clients send." />
                  </label>
                  <input
                    type="text" value={form.aliases} placeholder="grok-3, x-ai/grok-3-fast"
                    onChange={e => set('aliases', e.target.value)}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
                <div className="grid grid-cols-2 gap-4 mt-3">
                  <div>
                    <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                      <span>Family</span>
                      <HelpHint text="Upstream physical model identity, independent of provider. e.g. ‘grok-3’ for both the grok.com-web entry AND the OpenRouter entry. Two capability rows with the same family but different variants represent multi-route access to the SAME model. Leave blank to derive from canonical model_id (strip provider prefix)." />
                    </label>
                    <input
                      type="text" value={form.model_family} placeholder="grok-3"
                      onChange={e => set('model_family', e.target.value)}
                      className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                  </div>
                  <div>
                    <label className="text-xs font-medium text-gray-500 dark:text-gray-400 block mb-1 flex items-center gap-1">
                      <span>Variant</span>
                      <HelpHint text="Route flavour for multi-route models. Common values: ‘web’ (grok-bridge), ‘openrouter’ (marketplace), ‘direct’ (vendor API), ‘vertex’ (GCP), ‘azure’ (AOAI). Leave blank when there's only one route to this family." />
                    </label>
                    <input
                      type="text" value={form.model_variant} placeholder="web"
                      onChange={e => set('model_variant', e.target.value)}
                      className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                    />
                  </div>
                </div>
              </div>

              <div className="flex gap-6 flex-wrap pt-1">
                <Toggle
                  label="Native reasoning"
                  checked={form.native_reasoning} onChange={v => set('native_reasoning', v)}
                  tooltip="Model has native chain-of-thought / extended thinking (Claude opus/sonnet, gpt-4-turbo, o1). When false, the proxy can apply CoT-Emulation if the request asks for it; when true, the model's built-in thinking is used."
                />
                <Toggle
                  label="Native tool use"
                  checked={form.native_tools} onChange={v => set('native_tools', v)}
                  tooltip="Model accepts ‘tools: [...]’ requests natively (Claude, GPT-4, Gemini). When false, the proxy emulates tool-calling via prompt — slower and less reliable. Older models like haiku-3.5, mini, etc. are typically false."
                />
                <Toggle
                  label="Native vision"
                  checked={form.native_vision} onChange={v => set('native_vision', v)}
                  tooltip="Model accepts image inputs natively (gpt-4o, claude-sonnet-4-5, gemini-2.5). When false, requests with images get filtered out at the routing layer for this model."
                />
              </div>
            </div>
          </ModalBody>
          <ModalFooter>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            <Button onClick={() => saveMutation.mutate(form!)} loading={saveMutation.isPending}>Save</Button>
          </ModalFooter>
        </Modal>
      )}
    </>
  )
}
