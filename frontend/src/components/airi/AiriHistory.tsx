/**
 * AIRI conversation history drawer (v4.0 milestone 5).
 *
 * Two views in one drawer:
 *  - the calling operator's own past conversations (most-recent first);
 *  - a cross-user search box — searching spans EVERY operator's AIRI history
 *    (decision #5: the shared history is the change-coordination surface).
 *
 * Selecting a conversation (or a search hit) hands its id up to the chat
 * panel, which loads the transcript. Mobile-first: full-width tappable rows.
 */
import { useState, useEffect, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type ConversationSummary = {
  id: string
  user_id: string
  title: string
  message_count: number
  updated_at: string | null
}
type SearchResult = {
  conversation_id: string
  conversation_title: string
  user_id: string
  role: string
  snippet: string
  at: string | null
}

const when = (s: string | null) => (s ? s.slice(0, 16) : '')

export function AiriHistory({
  open,
  onSelect,
}: {
  open: boolean
  onSelect: (conversationId: string) => void
}) {
  const [convs, setConvs] = useState<ConversationSummary[]>([])
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<SearchResult[] | null>(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState('')

  useEffect(() => {
    if (!open) return
    setLoading(true)
    setErr('')
    fetch(`${getBasePath()}/api/airi/conversations`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : { conversations: [] }))
      .then((d) => setConvs(d.conversations || []))
      .catch(() => setErr('Could not load history'))
      .finally(() => setLoading(false))
  }, [open])

  const runSearch = useCallback(async () => {
    const q = query.trim()
    if (!q) {
      setResults(null)
      return
    }
    setLoading(true)
    setErr('')
    try {
      const r = await fetch(
        `${getBasePath()}/api/airi/search?q=${encodeURIComponent(q)}`,
        { credentials: 'include' },
      )
      const d = await r.json()
      setResults(d.results || [])
    } catch {
      setErr('Search failed')
    } finally {
      setLoading(false)
    }
  }, [query])

  if (!open) return null

  return (
    <div className="border-t border-gray-200 dark:border-gray-700 p-3 space-y-3
                    bg-gray-50 dark:bg-gray-900/40">
      {/* cross-user search */}
      <div className="flex gap-2">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault()
              runSearch()
            }
          }}
          placeholder="Search all operators' AIRI history…"
          className="flex-1 min-w-0 rounded-md border border-gray-300 dark:border-gray-600
                     bg-white dark:bg-gray-900 px-3 py-2 text-sm
                     text-gray-900 dark:text-gray-100"
        />
        <button
          onClick={runSearch}
          className="shrink-0 rounded-md bg-gray-600 px-3 py-2 text-sm font-medium
                     text-white hover:bg-gray-700"
        >
          Search
        </button>
        {results !== null && (
          <button
            onClick={() => {
              setQuery('')
              setResults(null)
            }}
            className="shrink-0 rounded-md border border-gray-300 dark:border-gray-600
                       px-3 py-2 text-sm text-gray-700 dark:text-gray-300
                       hover:bg-gray-100 dark:hover:bg-gray-700"
          >
            Clear
          </button>
        )}
      </div>

      {loading && (
        <p className="text-xs text-gray-500 dark:text-gray-400">Loading…</p>
      )}
      {err && <p className="text-xs text-red-600 dark:text-red-400">{err}</p>}

      {/* search results — cross-user */}
      {!loading && results !== null && (
        <div className="space-y-1.5 max-h-[40vh] overflow-y-auto">
          {results.length === 0 ? (
            <p className="text-xs text-gray-500 dark:text-gray-400">No matches.</p>
          ) : (
            results.map((r, i) => (
              <button
                key={i}
                onClick={() => onSelect(r.conversation_id)}
                className="w-full text-left rounded-md border border-gray-200 dark:border-gray-700
                           bg-white dark:bg-gray-800 px-3 py-2 hover:bg-gray-50
                           dark:hover:bg-gray-700"
              >
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                    {r.conversation_title}
                  </span>
                  <span className="text-xs text-gray-400 shrink-0">
                    {r.user_id} · {when(r.at)}
                  </span>
                </div>
                <div className="text-xs text-gray-600 dark:text-gray-300 mt-0.5">
                  <span className="font-mono text-gray-400">{r.role}:</span> {r.snippet}
                </div>
              </button>
            ))
          )}
        </div>
      )}

      {/* the operator's own conversations */}
      {!loading && results === null && (
        <div className="space-y-1.5 max-h-[40vh] overflow-y-auto">
          {convs.length === 0 ? (
            <p className="text-xs text-gray-500 dark:text-gray-400">
              No saved conversations yet.
            </p>
          ) : (
            convs.map((c) => (
              <button
                key={c.id}
                onClick={() => onSelect(c.id)}
                className="w-full text-left rounded-md border border-gray-200 dark:border-gray-700
                           bg-white dark:bg-gray-800 px-3 py-2 hover:bg-gray-50
                           dark:hover:bg-gray-700"
              >
                <div className="flex items-center justify-between gap-2 flex-wrap">
                  <span className="text-sm font-medium text-gray-900 dark:text-gray-100 truncate">
                    {c.title}
                  </span>
                  <span className="text-xs text-gray-400 shrink-0">
                    {c.message_count} msg · {when(c.updated_at)}
                  </span>
                </div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
