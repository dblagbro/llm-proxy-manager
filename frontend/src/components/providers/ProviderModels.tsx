import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Pencil } from 'lucide-react'
import { providersApi } from '@/api'
import { Button } from '@/components/ui/Button'
import { Modal, ModalHeader, ModalBody, ModalFooter } from '@/components/ui/Modal'
import { useToast } from '@/components/ui/Toast'
import type { ModelCapability } from '@/types'

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
  }
}

function Toggle({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center gap-2 cursor-pointer select-none">
      <input
        type="checkbox"
        checked={checked}
        onChange={e => onChange(e.target.checked)}
        className="h-4 w-4 rounded border-gray-300 text-indigo-600 focus:ring-indigo-500"
      />
      <span className="text-sm text-gray-700 dark:text-gray-300">{label}</span>
    </label>
  )
}

function MultiCheck({ label, options, value, onChange }: {
  label: string; options: string[]; value: string[]; onChange: (v: string[]) => void
}) {
  function toggle(opt: string) {
    onChange(value.includes(opt) ? value.filter(x => x !== opt) : [...value, opt])
  }
  return (
    <div>
      <p className="text-xs font-medium text-gray-500 mb-1">{label}</p>
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

  const { data: caps, isLoading } = useQuery<ModelCapability[]>({
    queryKey: ['capabilities', providerId],
    queryFn: () => providersApi.capabilities(providerId),
  })

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
        <p className="text-xs text-gray-400 mb-2 font-medium">
          {caps.length} model{caps.length !== 1 ? 's' : ''} indexed
        </p>
        <div className="overflow-x-auto">
          <table className="w-full text-xs border-collapse">
            <thead>
              <tr className="text-left text-gray-400 border-b border-gray-200 dark:border-gray-700">
                <th className="pb-1 pr-4 font-medium">Model ID</th>
                <th className="pb-1 pr-4 font-medium">Tasks</th>
                <th className="pb-1 pr-4 font-medium">Cost</th>
                <th className="pb-1 pr-4 font-medium">Latency</th>
                <th className="pb-1 pr-4 font-medium">Context</th>
                <th className="pb-1 pr-4 font-medium">Features</th>
                <th className="pb-1 font-medium">Source</th>
                <th className="pb-1" />
              </tr>
            </thead>
            <tbody>
              {caps.map(c => (
                <tr key={c.id} className="border-b border-gray-100 dark:border-gray-800 last:border-0">
                  <td className="py-1 pr-4 font-mono text-gray-700 dark:text-gray-300 whitespace-nowrap">{c.model_id}</td>
                  <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.tasks.join(', ') || '—'}</td>
                  <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.cost_tier}</td>
                  <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">{c.latency}</td>
                  <td className="py-1 pr-4 text-gray-600 dark:text-gray-400">
                    {c.context_length >= 1000 ? `${Math.round(c.context_length / 1000)}k` : c.context_length}
                  </td>
                  <td className="py-1 pr-4 text-gray-500 dark:text-gray-500 whitespace-nowrap">
                    {c.native_reasoning && <span title="Native reasoning" className="mr-1">🧠</span>}
                    {c.native_tools && <span title="Native tool use" className="mr-1">🔧</span>}
                    {c.native_vision && <span title="Native vision" className="mr-1">👁</span>}
                  </td>
                  <td className="py-1 pr-4">
                    <span className={`px-1.5 py-0.5 rounded text-xs ${c.source === 'manual' ? 'bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-300' : 'bg-gray-100 text-gray-500 dark:bg-gray-800'}`}>
                      {c.source}
                    </span>
                  </td>
                  <td className="py-1">
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
            </tbody>
          </table>
        </div>
      </div>

      {editing && form && (
        <Modal open onClose={() => setEditing(null)} size="lg">
          <ModalHeader onClose={() => setEditing(null)}>
            Edit Capabilities — <span className="font-mono text-sm">{editing.model_id}</span>
          </ModalHeader>
          <ModalBody>
            <div className="space-y-4">
              <MultiCheck label="Tasks" options={TASKS} value={form.tasks} onChange={v => set('tasks', v)} />
              <MultiCheck label="Modalities" options={MODALITIES} value={form.modalities} onChange={v => set('modalities', v)} />

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Latency</label>
                  <select
                    value={form.latency}
                    onChange={e => set('latency', e.target.value)}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  >
                    {['low', 'medium', 'high'].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Cost tier</label>
                  <select
                    value={form.cost_tier}
                    onChange={e => set('cost_tier', e.target.value)}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  >
                    {['economy', 'standard', 'premium'].map(v => <option key={v} value={v}>{v}</option>)}
                  </select>
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Safety level (1–5)</label>
                  <input
                    type="number" min={1} max={5} value={form.safety}
                    onChange={e => set('safety', Number(e.target.value))}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
                <div>
                  <label className="text-xs font-medium text-gray-500 block mb-1">Context length (tokens)</label>
                  <input
                    type="number" min={1000} value={form.context_length}
                    onChange={e => set('context_length', Number(e.target.value))}
                    className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                  />
                </div>
              </div>

              <div>
                <label className="text-xs font-medium text-gray-500 block mb-1">Regions (comma-separated, blank = any)</label>
                <input
                  type="text" value={form.regions} placeholder="us, eu, asia"
                  onChange={e => set('regions', e.target.value)}
                  className="w-full px-2 py-1.5 text-sm bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-indigo-500"
                />
              </div>

              <div className="flex gap-6 flex-wrap pt-1">
                <Toggle label="Native reasoning" checked={form.native_reasoning} onChange={v => set('native_reasoning', v)} />
                <Toggle label="Native tool use" checked={form.native_tools} onChange={v => set('native_tools', v)} />
                <Toggle label="Native vision" checked={form.native_vision} onChange={v => set('native_vision', v)} />
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
