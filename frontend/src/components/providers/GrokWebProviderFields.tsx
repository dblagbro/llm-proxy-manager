import { useEffect, useState } from 'react'
import { Input } from '@/components/ui/Input'
import { Button } from '@/components/ui/Button'
import type { ProviderFormState } from './ProviderForm'

interface Props {
  form: ProviderFormState
  set: (patch: Partial<ProviderFormState>) => void
  editing: boolean
}

type Mode = 'bridge' | 'manual'

interface BridgeStatus {
  logged_in: boolean
  cookie_count: number
  cookies_present: Record<string, boolean>
  last_refresh_at: number
  last_refresh_status: string
  url: string | null
  current_conversation_id?: string | null
}

// Default bridge URL when llm-proxy2 + bridge live in the same docker-compose
// network. Operator can override for split-host setups.
const DEFAULT_BRIDGE_URL = 'http://llm-proxy2-grok-bridge:8443'
const DEFAULT_BRIDGE_PUBLIC = '/grok-bridge'
const DEFAULT_BRIDGE_TOKEN = 'bridge-internal-2026'

export function GrokWebProviderFields({ form, set, editing }: Props) {
  // Determine initial mode from existing extra_config: if bridge_url is
  // set, we're in bridge mode; otherwise default to bridge for new
  // providers (the easier path) or manual for existing legacy ones.
  const initialMode: Mode = (() => {
    if (form.extra_config?.bridge_url) return 'bridge'
    if (editing && form.extra_config?.cookie_header) return 'manual'
    return 'bridge'
  })()
  const [mode, setMode] = useState<Mode>(initialMode)
  const [status, setStatus] = useState<BridgeStatus | null>(null)
  const [statusErr, setStatusErr] = useState<string | null>(null)
  const [creatingConv, setCreatingConv] = useState(false)
  const [createConvErr, setCreateConvErr] = useState<string | null>(null)

  // Auto-populate bridge_url + bridge_token on initial mount when in bridge
  // mode. Without this, a new provider with bridge mode pre-selected (the
  // default) would submit with empty bridge_url and the backend would
  // reject it as "missing extra_config fields". Only fills if the operator
  // hasn't already typed a value.
  useEffect(() => {
    if (mode !== 'bridge') return
    const cur = form.extra_config || {}
    const next = { ...cur }
    let changed = false
    if (!cur.bridge_url) {
      next.bridge_url = DEFAULT_BRIDGE_URL
      changed = true
    }
    if (!cur.bridge_token) {
      next.bridge_token = DEFAULT_BRIDGE_TOKEN
      changed = true
    }
    if (changed) set({ extra_config: next })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode])

  // Live-poll bridge status when in bridge mode so operator sees login
  // state update immediately after they sign in.
  useEffect(() => {
    if (mode !== 'bridge') return
    let cancelled = false
    const poll = async () => {
      try {
        const r = await fetch(`${DEFAULT_BRIDGE_PUBLIC}/api/status`)
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const j = (await r.json()) as BridgeStatus
        if (!cancelled) {
          setStatus(j)
          setStatusErr(null)
        }
      } catch (e) {
        if (!cancelled) setStatusErr((e as Error).message)
      }
    }
    poll()
    const id = window.setInterval(poll, 5000)
    return () => {
      cancelled = true
      window.clearInterval(id)
    }
  }, [mode])

  // When operator switches to bridge mode for a new provider, prefill
  // the URL fields automatically — they shouldn't have to type these.
  function switchToBridge() {
    setMode('bridge')
    if (!form.extra_config?.bridge_url) {
      set({
        extra_config: {
          ...form.extra_config,
          bridge_url: DEFAULT_BRIDGE_URL,
        },
      })
    }
  }

  function switchToManual() {
    setMode('manual')
    // Strip bridge_url so the backend dispatcher knows to use cookie path.
    const next = { ...form.extra_config }
    delete next.bridge_url
    delete next.bridge_token
    set({ extra_config: next })
  }

  return (
    <div className="space-y-3 p-3 bg-purple-50 dark:bg-purple-950/20 border border-purple-200 dark:border-purple-900 rounded">
      <p className="text-xs text-purple-900 dark:text-purple-200 leading-snug">
        <strong>grok.com web subscription</strong> — proxies through your
        existing grok.com browser session. No xAI API key needed.
      </p>

      {/* Mode tabs */}
      <div className="flex gap-1 text-xs border-b border-purple-200 dark:border-purple-900">
        <button
          type="button"
          onClick={switchToBridge}
          className={
            'px-3 py-1.5 -mb-px border-b-2 font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-purple-500 rounded-sm ' +
            (mode === 'bridge'
              ? 'border-purple-600 text-purple-900 dark:text-purple-100'
              : 'border-transparent text-gray-500 hover:text-gray-700')
          }
        >
          Bridge (recommended)
        </button>
        <button
          type="button"
          onClick={switchToManual}
          className={
            'px-3 py-1.5 -mb-px border-b-2 font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-purple-500 rounded-sm ' +
            (mode === 'manual'
              ? 'border-purple-600 text-purple-900 dark:text-purple-100'
              : 'border-transparent text-gray-500 hover:text-gray-700')
          }
        >
          Manual paste
        </button>
      </div>

      {/* Common: conversation_id */}
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
          Conversation ID <span className="text-gray-500">(UUID after <code>grok.com/c/</code>)</span>
        </label>
        <div className="flex gap-2">
          <Input
            value={(form.extra_config?.conversation_id as string) || ''}
            onChange={e =>
              set({
                extra_config: {
                  ...form.extra_config,
                  conversation_id: e.target.value,
                },
              })
            }
            placeholder="e41fca28-3df3-44ae-ad27-1cb65d5fe2a5"
            required={!editing}
          />
          {mode === 'bridge' && status?.current_conversation_id &&
            (form.extra_config?.conversation_id as string) !== status.current_conversation_id && (
            <Button
              type="button"
              variant="secondary"
              onClick={() =>
                set({
                  extra_config: {
                    ...form.extra_config,
                    conversation_id: status.current_conversation_id || '',
                  },
                })
              }
              title={`Bridge is on ${status.current_conversation_id}`}
            >
              Use bridge's
            </Button>
          )}
          {mode === 'bridge' && (
            <Button
              type="button"
              variant="secondary"
              disabled={creatingConv}
              onClick={async () => {
                setCreatingConv(true)
                setCreateConvErr(null)
                try {
                  const r = await fetch(`${DEFAULT_BRIDGE_PUBLIC}/api/conversation/new`, {
                    method: 'POST',
                  })
                  const j = await r.json()
                  if (j.conversation_id) {
                    set({
                      extra_config: {
                        ...form.extra_config,
                        conversation_id: j.conversation_id,
                      },
                    })
                  } else {
                    setCreateConvErr(j.hint || j.error || 'No conversation_id returned')
                  }
                } catch (e) {
                  setCreateConvErr((e as Error).message)
                } finally {
                  setCreatingConv(false)
                }
              }}
              title="Have the bridge open a fresh grok.com chat and capture its UUID"
            >
              {creatingConv ? 'Creating…' : 'Create new'}
            </Button>
          )}
        </div>
        {createConvErr && (
          <p className="text-[11px] text-red-600 dark:text-red-400 mt-1">
            Create failed: {createConvErr}
          </p>
        )}
        <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
          {mode === 'bridge' && !status?.current_conversation_id ? (
            <>
              Click <strong>Create new</strong> to have the bridge open a
              fresh grok.com chat and auto-fill its UUID. Or paste one
              from your own browser's URL.
            </>
          ) : (
            <>
              Each proxy call uses <code>parentResponseId: ""</code> so callers
              don't share context inside this conversation. The conversation
              grows in your grok.com UI; pick a fresh one when it gets unwieldy.
            </>
          )}
        </p>
      </div>

      {mode === 'bridge' ? (
        <>
          <div className="rounded bg-white/60 dark:bg-gray-900/40 p-2.5 border border-purple-200 dark:border-purple-900">
            <div className="flex items-center gap-3 mb-2">
              <div className="text-xs flex-1">
                {statusErr ? (
                  <span className="text-amber-700 dark:text-amber-400">
                    Bridge unreachable: {statusErr}
                  </span>
                ) : status ? (
                  <span>
                    {status.logged_in ? (
                      <span className="text-green-700 dark:text-green-400">
                        ✓ Signed in to grok.com
                      </span>
                    ) : (
                      <span className="text-amber-700 dark:text-amber-400">
                        ⚠ Not signed in yet
                      </span>
                    )}
                    <span className="text-gray-500 ml-2">
                      · {status.cookie_count} cookies · last refresh{' '}
                      {status.last_refresh_at
                        ? new Date(status.last_refresh_at * 1000).toLocaleTimeString()
                        : 'never'}
                    </span>
                  </span>
                ) : (
                  <span className="text-gray-500">Checking bridge…</span>
                )}
              </div>
              <Button
                type="button"
                variant="primary"
                onClick={() => window.open(`${DEFAULT_BRIDGE_PUBLIC}/login`, '_blank', 'noopener,noreferrer')}
              >
                {status?.logged_in ? 'Re-sign in' : 'Connect Grok'}
              </Button>
            </div>
            <p className="text-[11px] text-gray-500 dark:text-gray-400">
              Click <strong>Connect Grok</strong> to open the bridge's noVNC
              login tab. Sign in to grok.com via Google OAuth in the embedded
              browser. The bridge keeps Cloudflare cookies fresh automatically
              after that.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Bridge URL <span className="text-gray-500">(internal docker network)</span>
            </label>
            <Input
              value={(form.extra_config?.bridge_url as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, bridge_url: e.target.value },
                })
              }
              placeholder={DEFAULT_BRIDGE_URL}
            />
            <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
              Default works when llm-proxy2 and the bridge run in the same
              docker-compose stack. Override for split-host or custom-port setups.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Bridge token <span className="text-gray-500">(must match BRIDGE_TOKEN env on the bridge container)</span>
            </label>
            <Input
              type="password"
              value={(form.extra_config?.bridge_token as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, bridge_token: e.target.value },
                })
              }
              placeholder="bridge-internal-2026"
            />
          </div>
        </>
      ) : (
        <>
          <p className="text-xs text-purple-900 dark:text-purple-200 leading-snug">
            From a logged-in browser tab on grok.com: DevTools Network → find a{' '}
            <code className="px-1 bg-white/40 rounded">/responses</code> request →
            Copy as cURL → paste below.
          </p>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Cookie header (raw, from cURL)
            </label>
            <textarea
              value={(form.extra_config?.cookie_header as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, cookie_header: e.target.value },
                })
              }
              rows={4}
              placeholder="cf_clearance=…; __cf_bm=…; sso=…; sso-rw=…; x-userid=…"
              className="w-full px-3 py-2 text-xs font-mono bg-white dark:bg-gray-900 text-gray-900 dark:text-gray-100 border border-gray-300 dark:border-gray-700 rounded focus:outline-none focus:ring-2 focus:ring-purple-500"
              required={!editing}
            />
            <p className="text-[11px] text-gray-500 dark:text-gray-400 mt-1">
              <code>cf_clearance</code> rotates every few hours and <code>__cf_bm</code> every 30 min —
              expect to re-paste periodically. (Bridge mode handles this automatically.)
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              x-statsig-id header
            </label>
            <Input
              value={(form.extra_config?.x_statsig_id as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, x_statsig_id: e.target.value },
                })
              }
              placeholder="d91JSvHnOYpC8kO+mqgfzEoKkCG…"
              required={!editing}
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              x-userid header <span className="text-gray-500">(optional — also pulled from cookies)</span>
            </label>
            <Input
              value={(form.extra_config?.x_userid as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, x_userid: e.target.value },
                })
              }
              placeholder="d5f586ee-8593-47a8-ba5f-8ca6f8d383aa"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              user-agent <span className="text-gray-500">(optional — defaults to Chrome 147)</span>
            </label>
            <Input
              value={(form.extra_config?.user_agent as string) || ''}
              onChange={e =>
                set({
                  extra_config: { ...form.extra_config, user_agent: e.target.value },
                })
              }
              placeholder="Mozilla/5.0 (Windows NT 10.0; Win64; x64) …"
            />
          </div>
        </>
      )}
    </div>
  )
}
