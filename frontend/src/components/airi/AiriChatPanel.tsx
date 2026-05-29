/**
 * AIRI — AI Router Interface chat panel (v4.0).
 *
 * The conversational panel on the Routing / LMRH page. AIRI inspects and
 * explains routing, and (milestone 3) can PROPOSE changes — rendered inline
 * as approve / reject cards. Milestone 5 adds persistent conversation
 * history with a cross-user search drawer. Renders only when the
 * `airi_enabled` flag is on. Mobile-responsive.
 */
import { useState, useEffect, useRef, useCallback } from 'react'
import { getBasePath } from '@/lib/basePath'
import { AiriHistory } from './AiriHistory'
import { AiriMicButton } from './AiriMicButton'
import { AiriHandsFree } from './AiriHandsFree'
import { AiriSpeaker, type AiriSpeakerHandle } from './AiriSpeaker'

type ProposalData = {
  proposal_id: string
  kind: string
  target: string
  change: { field?: string; from?: unknown; to?: unknown; capped?: boolean }
  dry_run: { summary?: string; warnings?: string[] }
  status: string
}
type Msg = {
  role: 'user' | 'assistant' | 'proposal'
  content?: string
  error?: boolean
  proposal?: ProposalData
}

const SUGGESTIONS = [
  'How does routing work?',
  'Show me provider health',
  'Is the AI Provider Supervisor enabled?',
]

/** POST the conversation and parse the SSE response, one frame at a time. */
async function streamChat(
  messages: { role: string; content: string }[],
  conversationId: string | null,
  onEvent: (event: string, data: any) => void,
): Promise<void> {
  const res = await fetch(`${getBasePath()}/api/airi/chat`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, conversation_id: conversationId }),
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

// The active thread id is remembered here so the conversation survives
// navigating away from the Routing page and back (the panel unmounts).
const CONV_STORAGE_KEY = 'airi.active-conversation-id'

export function AiriChatPanel() {
  const [enabled, setEnabled] = useState<boolean | null>(null)
  const [expanded, setExpanded] = useState(true)
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [status, setStatus] = useState('')
  // proposal_id -> latest status (overrides the value the event carried)
  const [propStatus, setPropStatus] = useState<Record<string, string>>({})
  // M5 — the persisted thread this panel is attached to, and the history drawer
  const [conversationId, setConversationId] = useState<string | null>(null)
  const [historyOpen, setHistoryOpen] = useState(false)
  // v4.2 — voice input; v4.3 — voice output (text-to-speech)
  const [voiceEnabled, setVoiceEnabled] = useState(false)
  const [ttsEnabled, setTtsEnabled] = useState(false)
  const [micError, setMicError] = useState('')
  const speakerRef = useRef<AiriSpeakerHandle>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const restoredRef = useRef(false)

  useEffect(() => {
    fetch(`${getBasePath()}/api/airi/status`, { credentials: 'include' })
      .then((r) => (r.ok ? r.json() : { enabled: false }))
      .then((d) => {
        setEnabled(!!d.enabled)
        setVoiceEnabled(!!d.voice_enabled)
        setTtsEnabled(!!d.tts_enabled)
      })
      .catch(() => setEnabled(false))
  }, [])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages, status])

  // Remember the active thread so it can be restored after navigation.
  useEffect(() => {
    if (conversationId) localStorage.setItem(CONV_STORAGE_KEY, conversationId)
  }, [conversationId])

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim()
      if (!trimmed || busy) return
      const history: Msg[] = [...messages, { role: 'user', content: trimmed }]
      setMessages(history)
      setInput('')
      setBusy(true)
      setStatus('thinking…')
      // wire conversation = only user/assistant text turns
      const wire = history
        .filter((m) => m.role === 'user' || m.role === 'assistant')
        .map((m) => ({ role: m.role, content: m.content || '' }))
      try {
        await streamChat(wire, conversationId, (event, data) => {
          if (event === 'conversation') {
            // server tells us which persisted thread this turn landed in
            if (data.conversation_id) setConversationId(data.conversation_id)
          } else if (event === 'status') {
            setStatus(data.text || 'working…')
          } else if (event === 'proposal') {
            setMessages((m) => [...m, { role: 'proposal', proposal: data }])
          } else if (event === 'message') {
            setMessages((m) => [...m, { role: 'assistant', content: data.text || '' }])
            setStatus('')
            // v4.3 — read the completed answer aloud (no-op if TTS is off)
            speakerRef.current?.speak(data.text || '')
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
    [busy, messages, conversationId],
  )

  /** Start a fresh thread — the next turn creates a new persisted conversation. */
  const newChat = useCallback(() => {
    if (busy) return
    setMessages([])
    setConversationId(null)
    setPropStatus({})
    setStatus('')
    setHistoryOpen(false)
    localStorage.removeItem(CONV_STORAGE_KEY)
  }, [busy])

  /** Load a persisted conversation (the operator's own, or anyone's via search). */
  const loadConversation = useCallback(
    async (id: string) => {
      if (busy) return
      setHistoryOpen(false)
      setBusy(true)
      try {
        const res = await fetch(
          `${getBasePath()}/api/airi/conversations/${id}`,
          { credentials: 'include' },
        )
        if (!res.ok) {
          // stale / deleted thread — drop the remembered id so it isn't retried
          localStorage.removeItem(CONV_STORAGE_KEY)
          setStatus('Could not load that conversation.')
          return
        }
        const body = await res.json()
        const loaded: Msg[] = (body.messages || [])
          .filter((m: any) => m.role === 'user' || m.role === 'assistant')
          .map((m: any) => ({ role: m.role, content: m.content || '' }))
        setMessages(loaded)
        setConversationId(id)
        setPropStatus({})
        setStatus('')
      } catch {
        setStatus('Could not load that conversation — AIRI is unreachable.')
      } finally {
        setBusy(false)
      }
    },
    [busy],
  )

  // On mount, restore the conversation that was active before navigation.
  useEffect(() => {
    if (enabled !== true || restoredRef.current) return
    restoredRef.current = true
    const saved = localStorage.getItem(CONV_STORAGE_KEY)
    if (saved) loadConversation(saved)
    // run once when the feature flag resolves; loadConversation is stable enough
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled])

  const decide = useCallback(
    async (proposalId: string, action: 'apply' | 'reject' | 'revert') => {
      setBusy(true)
      try {
        const res = await fetch(
          `${getBasePath()}/api/airi/proposals/${proposalId}/${action}`,
          { method: 'POST', credentials: 'include' },
        )
        const body = await res.json().catch(() => ({}))
        if (!res.ok) {
          setStatus(body.error || `Could not ${action} the proposal`)
        } else {
          setPropStatus((m) => ({ ...m, [proposalId]: body.status || action }))
          setStatus('')
        }
      } catch {
        setStatus(`Could not ${action} the proposal — AIRI is unreachable`)
      } finally {
        setBusy(false)
      }
    },
    [],
  )

  // Voice (push-to-talk and hands-free) drops its transcript into the input —
  // it is never auto-sent; the operator reviews and presses Send.
  const onVoiceTranscript = useCallback((t: string) => {
    setMicError('')
    setInput((cur) => (cur.trim() ? cur.trim() + ' ' + t : t))
  }, [])

  if (enabled !== true) return null

  return (
    <div className="rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-3 text-left"
      >
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-base font-semibold text-gray-900 dark:text-gray-100">
            AIRI
          </span>
          <span className="text-xs text-gray-500 dark:text-gray-400">
            AI Router Interface — ask about routing, or propose a change
          </span>
        </div>
        <span className="text-gray-400 text-sm">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="border-t border-gray-200 dark:border-gray-700">
          {/* M5 toolbar — new thread + history drawer */}
          <div className="flex items-center gap-2 px-3 py-2 flex-wrap
                          border-b border-gray-100 dark:border-gray-700/60">
            <button
              onClick={newChat}
              disabled={busy}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1.5
                         text-xs font-medium text-gray-700 dark:text-gray-300
                         hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              + New chat
            </button>
            <button
              onClick={() => setHistoryOpen((h) => !h)}
              className={
                'rounded-md border px-3 py-1.5 text-xs font-medium disabled:opacity-50 ' +
                (historyOpen
                  ? 'border-blue-400 bg-blue-50 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300'
                  : 'border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700')
              }
            >
              History &amp; search {historyOpen ? '▾' : '▸'}
            </button>
            {conversationId && (
              <span className="text-xs text-gray-400 dark:text-gray-400 truncate">
                thread saved
              </span>
            )}
          </div>

          <AiriHistory open={historyOpen} onSelect={loadConversation} />

          <div
            ref={scrollRef}
            className="max-h-[55vh] min-h-[8rem] overflow-y-auto px-4 py-3 space-y-3"
          >
            {messages.length === 0 && (
              <div className="space-y-2">
                <p className="text-sm text-gray-500 dark:text-gray-400">
                  Ask AIRI about routing, or ask it to propose a change. Proposed
                  changes appear as cards you approve — nothing is applied until you do
                  (unless you ask AIRI to auto-apply).
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

            {messages.map((m, i) => {
              if (m.role === 'proposal' && m.proposal) {
                return (
                  <ProposalCard
                    key={i}
                    p={m.proposal}
                    status={propStatus[m.proposal.proposal_id] ?? m.proposal.status}
                    busy={busy}
                    decide={decide}
                  />
                )
              }
              return (
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
              )
            })}

            {status && (
              <div className="flex justify-start">
                <div className="rounded-lg px-3 py-2 text-sm text-gray-500 dark:text-gray-400 italic">
                  AIRI is {status}
                </div>
              </div>
            )}
          </div>

          {micError && (
            <div className="px-3 pt-2 text-xs text-amber-700 dark:text-amber-400">
              {micError}
            </div>
          )}
          <div className="border-t border-gray-200 dark:border-gray-700 p-3 flex gap-2">
            {voiceEnabled && (
              <>
                <AiriMicButton
                  disabled={busy}
                  onTranscript={onVoiceTranscript}
                  onError={setMicError}
                />
                <AiriHandsFree
                  disabled={busy}
                  onTranscript={onVoiceTranscript}
                  onError={setMicError}
                />
              </>
            )}
            {ttsEnabled && (
              <AiriSpeaker ref={speakerRef} onError={setMicError} />
            )}
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
              placeholder={voiceEnabled ? 'Ask AIRI — or tap the mic…' : 'Ask AIRI…'}
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

function ProposalCard({
  p,
  status,
  busy,
  decide,
}: {
  p: ProposalData
  status: string
  busy: boolean
  decide: (id: string, action: 'apply' | 'reject' | 'revert') => void
}) {
  const c = p.change || {}
  const dry = p.dry_run || {}
  const warnings = dry.warnings || []
  return (
    <div className="rounded-lg border border-blue-300 dark:border-blue-700 bg-blue-50 dark:bg-blue-900/20 p-3 space-y-2">
      <div className="text-sm font-semibold text-gray-900 dark:text-gray-100">
        AIRI proposes — {p.kind?.replace(/_/g, ' ')} · {p.target}
      </div>
      <div className="text-sm font-mono text-gray-800 dark:text-gray-200">
        {c.field}: {String(c.from)} → {String(c.to)}
        {c.capped ? ' (capped by the active rule-set)' : ''}
      </div>
      {dry.summary && (
        <div className="text-xs text-gray-600 dark:text-gray-300">{dry.summary}</div>
      )}
      {warnings.map((w, i) => (
        <div
          key={i}
          className="text-xs text-amber-700 dark:text-amber-400 bg-amber-50 dark:bg-amber-900/30
                     rounded px-2 py-1"
        >
          ⚠ {w}
        </div>
      ))}
      <div className="flex items-center gap-2 pt-1">
        {status === 'pending' && (
          <>
            <button
              onClick={() => decide(p.proposal_id, 'apply')}
              disabled={busy}
              className="rounded-md bg-blue-600 px-3 py-1 text-xs font-medium text-white
                         hover:bg-blue-700 disabled:opacity-50"
            >
              Approve &amp; apply
            </button>
            <button
              onClick={() => decide(p.proposal_id, 'reject')}
              disabled={busy}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1
                         text-xs text-gray-700 dark:text-gray-300
                         hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              Reject
            </button>
          </>
        )}
        {status === 'applied' && (
          <>
            <span className="text-xs font-medium text-green-700 dark:text-green-400">
              ✓ Applied
            </span>
            <button
              onClick={() => decide(p.proposal_id, 'revert')}
              disabled={busy}
              className="rounded-md border border-gray-300 dark:border-gray-600 px-3 py-1
                         text-xs text-gray-700 dark:text-gray-300
                         hover:bg-gray-100 dark:hover:bg-gray-700 disabled:opacity-50"
            >
              Revert
            </button>
          </>
        )}
        {status === 'rejected' && (
          <span className="text-xs text-gray-500 dark:text-gray-400">Rejected</span>
        )}
        {status === 'reverted' && (
          <span className="text-xs text-gray-500 dark:text-gray-400">Reverted</span>
        )}
      </div>
    </div>
  )
}
