/**
 * AIRI voice input — hands-free wake word (v4.2 milestone 3).
 *
 * An opt-in toggle next to the push-to-talk mic. When on, Vosk (compiled to
 * WebAssembly, running entirely IN THE BROWSER) listens for the wake word
 * "Airy". On a wake, the command that follows is captured and dropped into
 * the chat input for the operator to review and send. Voice NEVER
 * auto-sends — consistent with the v4.2 push-to-talk button.
 *
 * Why two stages:
 *  - WAKE: a GRAMMAR-CONSTRAINED Vosk recognizer (`["airy","[unk]"]`). Vosk's
 *    small free recognizer mis-hears "Airy" as common words ("every", "very")
 *    — that both misses real wakes and, if those were treated as the wake
 *    word, would false-trigger on ordinary speech. Locking the recognizer to
 *    a wake-word grammar makes "airy" detection reliable. No audio leaves the
 *    browser for wake detection.
 *  - COMMAND: once awake, the utterance is recorded with MediaRecorder and
 *    transcribed by Whisper via /api/airi/transcribe — the same path
 *    push-to-talk uses. Whisper is far more accurate on open-ended speech
 *    than Vosk's small model (which a wake-word grammar cannot transcribe).
 *
 * The Vosk model (~40 MB) is fetched once from /api/airi/voice-model and
 * cached by the browser. vosk-browser is dynamically imported so its WASM
 * payload only loads when the operator turns hands-free on. Renders only
 * when the airi_voice_enabled flag is on (the parent gates this).
 */
import { useState, useRef, useCallback, useEffect } from 'react'
import { Ear, EarOff, Loader2 } from 'lucide-react'
import { getBasePath } from '@/lib/basePath'

type HFState =
  | 'off' | 'loading' | 'listening' | 'capturing' | 'transcribing' | 'error'

// Vosk recognises only these tokens — "airy" (the wake word) or "[unk]".
const WAKE_GRAMMAR = JSON.stringify(['airy', 'hey airy', '[unk]'])
// How long to record the command after a wake. The operator says "Airy"
// then their request; review-before-send means a generous window is safe.
const CAPTURE_MS = 6_000
// Whisper may catch the wake word at the head of the command clip — drop a
// leading word if it is the wake word or a common mis-hearing of it.
const LEADING_WAKEISH = new Set([
  'airy', 'airey', 'airi', 'hey', 'okay', 'ok', 'every', 'very', 'eerie',
])

/** Does this grammar-recognizer output contain the wake word? */
function isWake(text: string): boolean {
  return (text || '')
    .toLowerCase()
    .split(/\s+/)
    .some((w) => w.replace(/[^a-z]/g, '') === 'airy')
}

/** Drop a leading wake-ish word from a transcribed command. */
function stripLeadingWake(text: string): string {
  const words = (text || '').trim().split(/\s+/).filter(Boolean)
  while (words.length > 1 &&
         LEADING_WAKEISH.has(words[0].toLowerCase().replace(/[^a-z]/g, ''))) {
    words.shift()
  }
  return words.join(' ').trim()
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
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const capTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // The Vosk callback is long-lived; it reads the live state via this ref.
  const stateRef = useRef<HFState>('off')
  useEffect(() => {
    stateRef.current = state
  }, [state])

  const teardown = useCallback(() => {
    if (capTimerRef.current) clearTimeout(capTimerRef.current)
    capTimerRef.current = null
    try {
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    } catch { /* ignore */ }
    recorderRef.current = null
    chunksRef.current = []
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

  /** Send the recorded command clip to Whisper and fill the input. */
  const transcribe = useCallback(async () => {
    const chunks = chunksRef.current
    chunksRef.current = []
    const blob = new Blob(chunks, { type: chunks[0]?.type || 'audio/webm' })
    if (blob.size === 0) {
      setState('listening')
      return
    }
    setState('transcribing')
    try {
      const fd = new FormData()
      fd.append('file', blob, 'command.webm')
      const res = await fetch(`${getBasePath()}/api/airi/transcribe`, {
        method: 'POST',
        credentials: 'include',
        body: fd,
      })
      const body = await res.json().catch(() => ({}))
      const text = stripLeadingWake(body.text || '')
      if (res.ok && text) onTranscript(text)
      else if (res.ok) onError("Didn't catch a command after “Airy” — try again.")
      else onError(body.error || 'Could not transcribe the command.')
    } catch {
      onError('Could not transcribe the command — hands-free is still on.')
    } finally {
      // only return to listening if hands-free was not switched off meanwhile
      if (recognizerRef.current) setState('listening')
    }
  }, [onTranscript, onError])

  /** A wake word fired — record the command that follows. */
  const beginCapture = useCallback(() => {
    const stream = streamRef.current
    if (!stream || typeof MediaRecorder === 'undefined') return
    setState('capturing')
    let rec: MediaRecorder
    try {
      rec = new MediaRecorder(stream)
    } catch {
      setState('listening')
      return
    }
    chunksRef.current = []
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data)
    }
    rec.onstop = () => { void transcribe() }
    recorderRef.current = rec
    rec.start()
    capTimerRef.current = setTimeout(() => {
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    }, CAPTURE_MS)
  }, [transcribe])

  // One Vosk recognizer message (final `result` or streaming `partialresult`).
  const onVoskText = useCallback(
    (text: string) => {
      if (stateRef.current === 'listening' && isWake(text)) beginCapture()
    },
    [beginCapture],
  )

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      onError('Voice input is not supported in this browser.')
      return
    }
    setState('loading')

    // vosk-browser carries a sizeable WASM payload — load it on demand only.
    let createModel: (url: string, logLevel?: number) => Promise<any>
    try {
      const mod: any = await import('vosk-browser')
      createModel = mod.createModel || mod.default?.createModel
      if (!createModel) throw new Error('createModel missing')
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
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
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

    // 16 kHz — the Vosk model's native rate; no resampling guesswork.
    const ctx = new AudioContext({ sampleRate: 16_000 })
    ctxRef.current = ctx
    if (ctx.state === 'suspended') {
      try { await ctx.resume() } catch { /* best effort */ }
    }
    // Grammar-constrained recognizer — reliably detects "airy", nothing else.
    const recognizer = new model.KaldiRecognizer(ctx.sampleRate, WAKE_GRAMMAR)
    recognizerRef.current = recognizer
    recognizer.on('result', (m: any) => onVoskText(m?.result?.text || ''))
    recognizer.on('partialresult',
      (m: any) => onVoskText(m?.result?.partial || ''))

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
  }, [onVoskText, onError])

  const toggle = useCallback(() => {
    if (state === 'off' || state === 'error') void start()
    else {
      teardown()
      setState('off')
    }
  }, [state, start, teardown])

  const on = state !== 'off' && state !== 'error'
  const busyState = state === 'capturing' || state === 'transcribing'
  const label =
    state === 'loading'
      ? 'Loading hands-free…'
      : state === 'capturing'
        ? 'Listening for your command…'
        : state === 'transcribing'
          ? 'Transcribing your command…'
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
        (busyState
          ? 'bg-blue-600 text-white animate-pulse motion-reduce:animate-none'
          : on
            ? 'bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 ' +
              'border border-blue-300 dark:border-blue-700'
            : 'border border-gray-300 dark:border-gray-600 text-gray-600 ' +
              'dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700')
      }
    >
      {state === 'loading' || state === 'transcribing' ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : on ? (
        <Ear className="h-4 w-4" />
      ) : (
        <EarOff className="h-4 w-4" />
      )}
    </button>
  )
}
