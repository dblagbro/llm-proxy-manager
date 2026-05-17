/**
 * AIRI voice input — hands-free wake word (v4.2 milestone 3).
 *
 * An opt-in toggle next to the push-to-talk mic. When on, Vosk (compiled to
 * WASM, running entirely IN THE BROWSER — no audio leaves the browser for
 * wake detection) listens continuously for the word "Airy". On hearing it,
 * the rest of the utterance is dropped into the chat input for the operator
 * to review and send. Voice NEVER auto-sends — consistent with the v4.2
 * push-to-talk button.
 *
 * The Vosk model (~40 MB) is fetched once from /api/airi/voice-model — the
 * proxy endpoint that streams it from the whisper-bridge sidecar — and is
 * cached by the browser thereafter. vosk-browser is dynamically imported so
 * its WASM payload only loads when the operator turns hands-free on. Renders
 * only when the airi_voice_enabled flag is on (the parent gates this).
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { Ear, EarOff, Loader2 } from 'lucide-react'
import { getBasePath } from '@/lib/basePath'

type HFState = 'off' | 'loading' | 'listening' | 'armed' | 'error'

// The wake word, plus the near-misses Vosk's small model tends to emit for it.
const WAKE_WORDS = ['airy', 'airey', 'arie', 'fairy', 'hairy', 'eyrie', 'airi']
// After a bare "Airy" with no command, wait this long for the command
// utterance before dropping back to plain listening.
const ARM_TIMEOUT_MS = 8_000

/** Split a final transcript at the wake word into { matched, command }. */
function matchWake(text: string): { matched: boolean; command: string } {
  const words = text.toLowerCase().trim().split(/\s+/).filter(Boolean)
  for (let i = 0; i < words.length; i++) {
    if (WAKE_WORDS.includes(words[i].replace(/[^a-z]/g, ''))) {
      return { matched: true, command: words.slice(i + 1).join(' ').trim() }
    }
  }
  return { matched: false, command: '' }
}

export function AiriHandsFree({
  disabled,
  onTranscript,
  onError,
}: {
  disabled: boolean
  onTranscript: (text: string) => void
  onError: (message: string) => void
}) {
  const [state, setState] = useState<HFState>('off')

  // Long-lived audio + Vosk handles, torn down on stop / unmount.
  const modelRef = useRef<any>(null)
  const recognizerRef = useRef<any>(null)
  const ctxRef = useRef<AudioContext | null>(null)
  const nodeRef = useRef<ScriptProcessorNode | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const armTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The result handler is long-lived; it reads the live state via this ref.
  const stateRef = useRef<HFState>('off')
  useEffect(() => {
    stateRef.current = state
  }, [state])

  const teardown = useCallback(() => {
    if (armTimerRef.current) clearTimeout(armTimerRef.current)
    armTimerRef.current = null
    try { nodeRef.current?.disconnect() } catch { /* already disconnected */ }
    nodeRef.current = null
    try { void ctxRef.current?.close() } catch { /* already closed */ }
    ctxRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
    try { recognizerRef.current?.remove?.() } catch { /* ignore */ }
    recognizerRef.current = null
    try { modelRef.current?.terminate?.() } catch { /* ignore */ }
    modelRef.current = null
  }, [])

  // Always release the mic + WASM worker when the panel unmounts.
  useEffect(() => teardown, [teardown])

  const arm = useCallback(() => {
    setState('armed')
    if (armTimerRef.current) clearTimeout(armTimerRef.current)
    armTimerRef.current = setTimeout(() => setState('listening'), ARM_TIMEOUT_MS)
  }, [])

  // One completed Vosk utterance. Vosk emits this on end-of-speech (silence).
  const handleFinal = useCallback(
    (rawText: string) => {
      const text = (rawText || '').trim()
      if (!text) return
      if (stateRef.current === 'armed') {
        // The wake word was already heard — this whole utterance is the command.
        if (armTimerRef.current) clearTimeout(armTimerRef.current)
        const { matched, command } = matchWake(text)
        if (matched && !command) { arm(); return } // a second "Airy" re-arms
        onTranscript(matched ? command : text)
        setState('listening')
        return
      }
      // Plain listening — react only if the wake word is in this utterance.
      const { matched, command } = matchWake(text)
      if (!matched) return
      if (command) onTranscript(command)  // "Airy, show me provider health"
      else arm()                          // bare "Airy" — await the command
    },
    [arm, onTranscript],
  )

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      onError('Voice input is not supported in this browser.')
      return
    }
    setState('loading')

    // vosk-browser carries a sizeable WASM payload — load it on demand only.
    let createModel: (url: string) => Promise<any>
    try {
      ;({ createModel } = await import('vosk-browser'))
    } catch {
      onError('Hands-free voice could not load in this browser.')
      setState('off')
      return
    }

    // Fetch the Vosk model through the proxy ourselves (admin cookie) so the
    // request is authenticated regardless of how vosk-browser fetches URLs.
    let modelUrl: string
    try {
      const res = await fetch(`${getBasePath()}/api/airi/voice-model`, {
        credentials: 'include',
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      modelUrl = URL.createObjectURL(await res.blob())
    } catch {
      onError('Could not load the voice model — hands-free is unavailable.')
      setState('off')
      return
    }

    let model: any
    let stream: MediaStream
    try {
      model = await createModel(modelUrl)
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      })
    } catch (e: any) {
      URL.revokeObjectURL(modelUrl)
      if (e?.name === 'NotAllowedError') onError('Microphone access was denied.')
      else onError('Hands-free voice could not start.')
      try { model?.terminate?.() } catch { /* ignore */ }
      setState('off')
      return
    }
    URL.revokeObjectURL(modelUrl)
    modelRef.current = model
    streamRef.current = stream

    const ctx = new AudioContext()
    ctxRef.current = ctx
    if (ctx.state === 'suspended') {
      try { await ctx.resume() } catch { /* best effort */ }
    }
    const recognizer = new model.KaldiRecognizer(ctx.sampleRate)
    recognizerRef.current = recognizer
    recognizer.on('result', (m: any) => handleFinal(m?.result?.text || ''))

    const source = ctx.createMediaStreamSource(stream)
    // ScriptProcessor is deprecated but universally supported and is what
    // vosk-browser's own examples use; an AudioWorklet would need a separate
    // served module. The node writes no output, so routing it to destination
    // is silent — it is only there so onaudioprocess keeps firing.
    const node = ctx.createScriptProcessor(4096, 1, 1)
    nodeRef.current = node
    node.onaudioprocess = (ev) => {
      try { recognizer.acceptWaveform(ev.inputBuffer) } catch { /* drop a frame */ }
    }
    source.connect(node)
    node.connect(ctx.destination)
    setState('listening')
  }, [handleFinal, onError])

  const toggle = useCallback(() => {
    if (state === 'off' || state === 'error') void start()
    else {
      teardown()
      setState('off')
    }
  }, [state, start, teardown])

  const on = state !== 'off' && state !== 'error'
  const label =
    state === 'loading'
      ? 'Loading hands-free…'
      : state === 'armed'
        ? 'Listening for your command…'
        : on
          ? 'Hands-free on — say “Airy”'
          : 'Enable hands-free (say “Airy”)'

  return (
    <button
      type="button"
      onClick={toggle}
      disabled={disabled || state === 'loading'}
      title={label}
      aria-label={label}
      aria-pressed={on}
      className={
        'shrink-0 rounded-md px-3 py-2 flex items-center justify-center ' +
        'disabled:opacity-50 transition-colors ' +
        (state === 'armed'
          ? 'bg-blue-600 text-white animate-pulse'
          : on
            ? 'bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 ' +
              'border border-blue-300 dark:border-blue-700'
            : 'border border-gray-300 dark:border-gray-600 text-gray-600 ' +
              'dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700')
      }
    >
      {state === 'loading' ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : on ? (
        <Ear className="h-4 w-4" />
      ) : (
        <EarOff className="h-4 w-4" />
      )}
    </button>
  )
}
