/**
 * In-browser OAuth capture terminal (v2.6.0).
 *
 * Opens an xterm.js session against the llm-proxy2-capture sidecar via a
 * WebSocket relayed by the main proxy. The sidecar spawns the profile's
 * preset `login_cmd` (e.g. `claude login`) with env vars already set to
 * route traffic through the capture profile — so the admin doesn't have
 * to touch a shell on their workstation.
 *
 * States:
 *   idle      — not started
 *   starting  — POSTed /login, waiting for session_id
 *   running   — WebSocket connected, PTY live
 *   exited    — PTY exited or connection closed
 *   error     — failed to start or disconnected with error
 *
 * The component is hidden entirely when the profile's preset has no
 * login_cmd (every preset except claude-code in v2.6.0).
 */
import { useEffect, useRef, useState } from 'react'
import { Play, Square, AlertCircle, Loader2 } from 'lucide-react'
import { Terminal } from 'xterm'
import { FitAddon } from 'xterm-addon-fit'
import { WebLinksAddon } from 'xterm-addon-web-links'
import 'xterm/css/xterm.css'

import { Button } from '@/components/ui/Button'
import { useToast } from '@/components/ui/Toast'
import { oauthCaptureApi, type OAuthCaptureProfile, type OAuthCapturePreset } from '@/api'

type PaneState = 'idle' | 'starting' | 'running' | 'exited' | 'error'

export function TerminalPane({
  profile, preset,
}: {
  profile: OAuthCaptureProfile
  preset: OAuthCapturePreset | null
}) {
  const toast = useToast()
  const containerRef = useRef<HTMLDivElement>(null)
  const termRef = useRef<Terminal | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  const wsRef = useRef<WebSocket | null>(null)

  const [state, setState] = useState<PaneState>('idle')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [errorMsg, setErrorMsg] = useState<string>('')

  // Initialize xterm once the container mounts, tear down on unmount.
  useEffect(() => {
    if (!containerRef.current) return
    const term = new Terminal({
      convertEol: true,
      cursorBlink: true,
      fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
      fontSize: 13,
      theme: {
        background: '#0b1020',
        foreground: '#e5e7eb',
        cursor: '#60a5fa',
      },
    })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.loadAddon(new WebLinksAddon())
    term.open(containerRef.current)
    termRef.current = term
    fitRef.current = fit

    // Fit after layout settles
    requestAnimationFrame(() => {
      try { fit.fit() } catch { /* noop */ }
    })

    const onResize = () => {
      try {
        fit.fit()
        const ws = wsRef.current
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
        }
      } catch { /* noop */ }
    }
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      try { wsRef.current?.close() } catch { /* noop */ }
      try { term.dispose() } catch { /* noop */ }
      termRef.current = null
      fitRef.current = null
      wsRef.current = null
    }
  }, [])

  async function startSession(takeover = false) {
    const term = termRef.current
    if (!term) return
    setState('starting')
    setErrorMsg('')

    try {
      const resp = await oauthCaptureApi.startLogin(profile.name, takeover)
      if (resp.error === 'session_in_use') {
        const ok = confirm(
          `Another admin has a terminal session open (started ${resp.age_seconds}s ago). Take it over?`
        )
        if (ok) return startSession(true)
        setState('idle')
        return
      }
      if (!resp.session_id) {
        setState('error')
        setErrorMsg('No session_id returned')
        return
      }
      setSessionId(resp.session_id)

      // Open WebSocket
      const wsUrl = oauthCaptureApi.terminalWsUrl(resp.session_id)
      const ws = new WebSocket(wsUrl)
      wsRef.current = ws

      ws.onopen = () => {
        setState('running')
        // Send initial resize
        try {
          ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }))
        } catch { /* noop */ }
        term.focus()
      }

      ws.onmessage = (ev) => {
        const data = ev.data as string
        // Sidecar sends an __event__ JSON blob on PTY exit.
        if (data.startsWith('{') && data.includes('__event__')) {
          try {
            const obj = JSON.parse(data)
            if (obj.__event__ === 'exit') {
              term.write(`\r\n\x1b[90m[session exited, code=${obj.code}]\x1b[0m\r\n`)
              setState('exited')
              return
            }
          } catch { /* fall through */ }
        }
        term.write(data)
      }

      ws.onerror = () => {
        setState('error')
        setErrorMsg('WebSocket error')
      }

      ws.onclose = () => {
        if (state !== 'exited') setState('exited')
      }

      // Wire xterm data → WebSocket
      const disposer = term.onData((data) => {
        if (ws.readyState === WebSocket.OPEN) ws.send(data)
      })
      ws.addEventListener('close', () => disposer.dispose())

    } catch (e: any) {
      setState('error')
      setErrorMsg(e?.message || 'Failed to start session')
      toast.error(e?.message || 'Failed to start terminal session')
    }
  }

  async function stopSession() {
    try {
      if (sessionId) {
        await oauthCaptureApi.killSession(sessionId).catch(() => { /* noop */ })
      }
      wsRef.current?.close()
    } finally {
      setState('exited')
    }
  }

  // If the preset doesn't support in-browser capture, don't show the pane at all.
  if (!preset?.login_cmd) return null

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <div className="text-sm font-medium text-gray-800 dark:text-gray-200">
          In-browser terminal
          <span className="ml-2 text-xs font-normal text-gray-400">
            <code className="font-mono">{preset.login_cmd}</code>
          </span>
        </div>
        <div className="flex items-center gap-1.5">
          {state === 'idle' || state === 'exited' || state === 'error' ? (
            <Button onClick={() => startSession(false)} variant="primary" size="sm"
                    disabled={!profile.enabled}>
              <Play className="h-3.5 w-3.5 mr-1" />
              {state === 'exited' || state === 'error' ? 'Restart' : `Login to ${preset.label.split('—')[0].trim()}`}
            </Button>
          ) : state === 'starting' ? (
            <Button variant="primary" size="sm" disabled>
              <Loader2 className="h-3.5 w-3.5 mr-1 animate-spin" /> Starting…
            </Button>
          ) : (
            <Button onClick={stopSession} variant="danger" size="sm">
              <Square className="h-3.5 w-3.5 mr-1" /> Stop
            </Button>
          )}
        </div>
      </div>

      {!profile.enabled && (
        <div className="mb-2 text-xs text-amber-600 dark:text-amber-400 flex items-start gap-1.5">
          <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>Profile is paused. Click <strong>Start capture</strong> above before starting a login session.</span>
        </div>
      )}

      {state === 'error' && errorMsg && (
        <div className="mb-2 text-xs text-red-600 dark:text-red-400 flex items-start gap-1.5">
          <AlertCircle className="h-3.5 w-3.5 mt-0.5 shrink-0" />
          <span>{errorMsg}</span>
        </div>
      )}

      <div
        ref={containerRef}
        className="rounded border border-gray-700 bg-[#0b1020] p-2"
        style={{ height: '320px' }}
      />
      <p className="mt-1 text-xs text-gray-400">
        Tip: when the CLI prints an <code className="font-mono">https://…</code> approval URL,
        click it to open in a new tab. Approve in your browser; paste any resulting code back
        into the terminal.
      </p>
    </section>
  )
}
