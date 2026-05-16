/**
 * AIRI — AI Router Interface (v4.0 milestone 1).
 *
 * The conversational chat panel on the Routing / LMRH page. Milestone 1 is
 * read-only: AIRI inspects and explains routing, providers, and the AI
 * Provider Supervisor. It renders only when the `airi_enabled` feature flag
 * is on (probed via GET /api/airi/status). Mobile-responsive.
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'

type Msg = { role: 'user' | 'assistant'; content: string; error?: boolean }

const SUGGESTIONS = [
  'How does routing work?',
  'Show me provider health',
  'Is the AI Provider Supervisor enabled?',
]

/** POST the conversation and parse the SSE response, one frame at a time. */
async function streamChat(
  messages: { role: string; content: string }[],
  onEvent: (event: string, data: any) => void,
): Promise<void> {
  const res = await fetch(`${getBasePath()}/api/airi/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages }),
  })
  if (!res.ok || !res.body) {
    onEvent('error', { message: `AIRI request failed (HTTP ${res.status}).` })
    return
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    let sep: number
    while ((sep = buf.indexOf('\n\n')) >= 0) {
      const frame = buf.slice(0, sep)
      buf = buf.slice(sep + 2)
      let event = 'message'
      let data = '{}'
      for (const line of frame.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim()
        else if (line.startsWith('data:')) data = line.slice(5).trim()
      }
      try {
        onEvent(event, JSON.parse(data))
      } catch {
        /* ignore an unparseable frame */
      }
    }
  }
}

export function AiriChatPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(true)
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    fetch(`${getBasePath()}/api/airi/status`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => setEnabled(!!d.enabled))
      .catch(() => setEnabled(false))
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages, status])

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || busy) return
      const history: Msg[] = [...messages, { role: 'user', content: trimmed }]
      setMessages(history)
      setInput('')
      setBusy(true)
      setStatus('thinking…')
      const wire = history.map((m) => ({ role: m.role, content: m.content }))
      try {
        await streamChat(wire, (event, data) => {
          if (event === 'status') {
            setStatus(data.text || 'working…')
          } else if (event === 'message') {
            setMessages((m) => [...m, { role: 'assistant', content: data.text || '' }])
            setStatus('')
          } else if (event === 'error') {
            setMessages((m) => [
              ...m,
              { role: 'assistant', content: data.message || 'AIRI error', error: true },
            ])
            setStatus('')
          }
        })
      } catch {
        setMessages((m) => [
          ...m,
          { role: 'assistant', content: 'AIRI is unreachable right now.', error: true },
        ])
      } finally {
        setBusy(false)
        setStatus('')
      }
    },
    [busy, messages],
  )

  // Hidden entirely unless the feature flag is on.
  if (enabled !== true) return null

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      {/* Header */}
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            AIRI
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            AI Router Interface — ask about routing
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700">
          {/* Messages */}
          <div
            ref={scrollRef}
            className="max-h-[50vh] min-h-[8rem] overflow-y-auto px-4 py-3 space-y-3"
          >
            {messages.length === 0 && (
              <div className="space-y-2">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Ask AIRI about routing, providers, or the AI Provider Supervisor.
                  Read-only for now — it inspects and explains, it can't change anything yet.
                </p>
                <div className="flex flex-wrap gap-2">
                  {SUGGESTIONS.map((s) => (
                    <button
                      key={s}
                      onClick={() => send(s)}
                      disabled={busy}
                      className="text-xs rounded-full border border-gray-300 dark:border-gray-600
                                 px-3 py-1 text-gray-700 dark:text-gray-300
                                 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
                    >
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div
                key={i}
                className={m.role === 'user' ? 'flex justify-end' : 'flex justify-start'}
              >
                <div
                  className={
                    'max-w-[90%] sm:max-w-[80%] rounded-lg px-3 py-2 text-sm whitespace-pre-wrap break-words ' +
                    (m.role === 'user'
                      ? 'bg-blue-600 text-white'
                      : m.error
                        ? 'bg-red-50 dark:bg-red-900/30 text-red-700 dark:text-red-300'
                        : 'bg-gray-100 dark:bg-gray-700 text-gray-900 dark:text-gray-100')
                  }
                >
                  {m.content}
                </div>
              </div>
            ))}

            {status && (
              <div className="flex justify-start">
                <div className="rounded-lg px-3 py-2 text-sm text-gray-500 dark:text-gray-400 italic">
                  AIRI is {status}
                </div>
              </div>
            )}
          </div>

          {/* Input row */}
          <div className="border-t border-gray-200 dark:border-gray-700 p-3 flex gap-2">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  send(input)
                }
              }}
              disabled={busy}
              placeholder="Ask AIRI…"
              className="flex-1 min-w-0 rounded-md border border-gray-300 dark:border-gray-600
                         bg-white dark:bg-gray-900 px-3 py-2 text-sm
                         text-gray-900 dark:text-gray-100 disabled:opacity-60"
            />
            <button
              onClick={() => send(input)}
              disabled={busy || !input.trim()}
              className="shrink-0 rounded-md bg-blue-600 px-4 py-2 text-sm font-medium
                         text-white hover:bg-blue-700 disabled:opacity-50"
            >
              Send
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
