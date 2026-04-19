import { useQuery } from '@tanstack/react-query'
import { providersApi } from '@/api'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Spinner } from '@/components/ui/Spinner'
import { CopyButton } from '@/components/ui/CopyButton'

const LMRH_DIMS = [
  { key: 'task', label: 'task', values: 'chat, coding, reasoning, summarization, translation, classification, embedding', weight: 10 },
  { key: 'safety', label: 'safety', values: 'strict, balanced, permissive', weight: 8 },
  { key: 'modality', label: 'modality', values: 'text, image, audio, video', weight: 5 },
  { key: 'region', label: 'region', values: 'us, eu, ap, …', weight: 6 },
  { key: 'latency', label: 'latency', values: 'low, medium, high', weight: 4 },
  { key: 'cost', label: 'cost', values: 'low, medium, high', weight: 3 },
  { key: 'context-length', label: 'context-length', values: '4k, 8k, 16k, 32k, 128k, 200k', weight: 2 },
  { key: 'native-reasoning', label: 'native-reasoning', values: '?1, ?0', weight: 0 },
  { key: 'native-tools', label: 'native-tools', values: '?1, ?0', weight: 0 },
]

const EXAMPLES = [
  { label: 'Prefer reasoning model', header: 'LLM-Hint: task=reasoning;native-reasoning=?1' },
  { label: 'Low-cost EU region', header: 'LLM-Hint: cost=low;region=eu' },
  { label: 'Image analysis, strict safety', header: 'LLM-Hint: task=chat;modality=image;safety=strict' },
  { label: 'Long context (hard constraint)', header: 'LLM-Hint: context-length=128k;hard=context-length' },
  { label: 'Coding with tools required', header: 'LLM-Hint: task=coding;native-tools=?1;hard=native-tools' },
]

export function RoutingPage() {
  const { data: providers, isLoading } = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })

  return (
    <div className="p-6 space-y-6 max-w-5xl">
      <div>
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Routing / LMRH</h1>
        <p className="text-sm text-gray-500 mt-0.5">LLM Model Routing Hint protocol — draft-blagbrough-lmrh-00</p>
      </div>

      {/* Protocol overview */}
      <Card>
        <CardHeader><CardTitle>Protocol Overview</CardTitle></CardHeader>
        <CardContent className="space-y-4 text-sm text-gray-700 dark:text-gray-300">
          <p>
            LMRH uses RFC 8941 Structured Fields as an HTTP request header (<code className="font-mono text-indigo-400">LLM-Hint:</code>) to
            express routing preferences and hard constraints. The proxy scores each available provider against the hint
            and selects the best match.
          </p>
          <p>
            The response includes an <code className="font-mono text-indigo-400">LLM-Capability:</code> header describing the actual
            model that served the request and any unmet preferences.
          </p>
          <div className="bg-gray-50 dark:bg-gray-800 rounded-lg p-4 font-mono text-xs space-y-1">
            <p className="text-gray-400"># Request hint</p>
            <p>LLM-Hint: task=reasoning;safety=strict;hard=safety</p>
            <p className="text-gray-400 mt-2"># Response capability</p>
            <p>LLM-Capability: provider=anthropic;model=claude-opus-4-7;native-reasoning=?1;safety=strict</p>
          </div>
        </CardContent>
      </Card>

      {/* Dimensions */}
      <Card>
        <CardHeader><CardTitle>Routing Dimensions</CardTitle></CardHeader>
        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-100 dark:border-gray-700">
                  {['Dimension', 'Values', 'Score Weight'].map(h => (
                    <th key={h} className="text-left px-5 py-3 text-xs text-gray-400 font-medium">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
                {LMRH_DIMS.map(d => (
                  <tr key={d.key}>
                    <td className="px-5 py-3 font-mono text-indigo-400 text-xs">{d.label}</td>
                    <td className="px-5 py-3 text-gray-600 dark:text-gray-400 text-xs">{d.values}</td>
                    <td className="px-5 py-3">
                      {d.weight > 0 ? (
                        <Badge variant={d.weight >= 8 ? 'danger' : d.weight >= 5 ? 'warning' : 'default'}>{d.weight}</Badge>
                      ) : (
                        <Badge variant="muted">flag</Badge>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>

      {/* Examples */}
      <Card>
        <CardHeader><CardTitle>Example Headers</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          {EXAMPLES.map(ex => (
            <div key={ex.label}>
              <p className="text-xs text-gray-400 mb-1">{ex.label}</p>
              <div className="flex items-center gap-2 bg-gray-50 dark:bg-gray-800 rounded-lg px-4 py-2.5">
                <code className="flex-1 text-xs font-mono text-gray-800 dark:text-gray-200">{ex.header}</code>
                <CopyButton text={ex.header} />
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      {/* Provider capabilities */}
      <Card>
        <CardHeader><CardTitle>Provider Capability Profiles</CardTitle></CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : (providers?.length ?? 0) === 0 ? (
            <p className="text-center text-gray-500 py-8 text-sm">No providers configured</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-gray-100 dark:border-gray-700">
                    {['Provider', 'Type', 'Priority', 'Status'].map(h => (
                      <th key={h} className="text-left px-5 py-3 text-xs text-gray-400 font-medium">{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50 dark:divide-gray-800">
                  {(providers ?? []).map(p => (
                    <tr key={p.id}>
                      <td className="px-5 py-3 font-medium text-gray-900 dark:text-gray-100">{p.name}</td>
                      <td className="px-5 py-3 text-gray-600 dark:text-gray-400">{p.provider_type}</td>
                      <td className="px-5 py-3 text-gray-600 dark:text-gray-400">{p.priority}</td>
                      <td className="px-5 py-3">
                        <Badge variant={p.enabled ? 'success' : 'muted'}>{p.enabled ? 'Active' : 'Disabled'}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
