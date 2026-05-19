/**
 * AIRI voice input — push-to-talk mic button (v4.2 milestone 2).
 *
 * Tap to record, tap to stop. The clip is posted to /api/airi/transcribe
 * (the whisper-bridge sidecar) and the transcript is handed back via
 * onTranscript — the operator reviews it in the input before sending.
 * Voice never auto-sends. Renders only when the airi_voice_enabled flag
 * is on (the parent gates this). Mobile-friendly.
 */
import { useState, useRef, useCallback } from 'react'
import { Mic, Square, Loader2 } from 'lucide-react'
import { getBasePath } from '@/lib/basePath'

type RecState = 'idle' | 'recording' | 'transcribing'

// Safety cap — a forgotten recording auto-stops rather than running forever.
const MAX_RECORD_MS = 60_000

export function AiriMicButton({
  disabled,
  onTranscript,
  onError,
}: {
  disabled: boolean
  onTranscript: (text: string) => void
  onError: (message: string) => void
}) {
  const [state, setState] = useState<RecState>('idle')
  const recorderRef = useRef<MediaRecorder | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const streamRef = useRef<MediaStream | null>(null)
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const cleanup = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current)
    timerRef.current = null
    streamRef.current?.getTracks().forEach((t) => t.stop())
    streamRef.current = null
  }, [])

  const transcribe = useCallback(async () => {
    const chunks = chunksRef.current
    const blob = new Blob(chunks, { type: chunks[0]?.type || 'audio/webm' })
    if (blob.size === 0) {
      setState('idle')
      return
    }
    setState('transcribing')
    try {
      const fd = new FormData()
      fd.append('file', blob, 'speech.webm')
      const res = await fetch(`${getBasePath()}/api/airi/transcribe`, {
        method: 'POST',
        credentials: 'include',
        body: fd,
      })
      const body = await res.json().catch(() => ({}))
      if (res.ok && body.text) {
        onTranscript(body.text)
      } else if (res.ok) {
        onError("Didn't catch that — please try again or type.")
      } else {
        onError(body.error || 'Could not transcribe — please type instead.')
      }
    } catch {
      onError('Could not transcribe — please type instead.')
    } finally {
      setState('idle')
    }
  }, [onTranscript, onError])

  const start = useCallback(async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      onError('Voice input is not supported in this browser.')
      return
    }
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch {
      onError('Microphone access was denied.')
      return
    }
    streamRef.current = stream
    const rec = new MediaRecorder(stream)
    chunksRef.current = []
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data)
    }
    rec.onstop = () => {
      cleanup()
      void transcribe()
    }
    recorderRef.current = rec
    rec.start()
    setState('recording')
    timerRef.current = setTimeout(() => {
      if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    }, MAX_RECORD_MS)
  }, [cleanup, transcribe, onError])

  const stop = useCallback(() => {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
  }, [])

  const onClick = () => {
    if (state === 'recording') stop()
    else if (state === 'idle') start()
    // 'transcribing' — ignore
  }

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled || state === 'transcribing'}
      title={state === 'recording' ? 'Stop recording' : 'Speak to AIRI'}
      aria-label={state === 'recording' ? 'Stop recording' : 'Record voice input'}
      className={
        'shrink-0 rounded-md px-3 py-2 flex items-center justify-center ' +
        'disabled:opacity-50 transition-colors ' +
        (state === 'recording'
          ? 'bg-red-600 text-white hover:bg-red-700 animate-pulse motion-reduce:animate-none'
          : 'border border-gray-300 dark:border-gray-600 text-gray-600 ' +
            'dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700')
      }
    >
      {state === 'transcribing' ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : state === 'recording' ? (
        <Square className="h-4 w-4" />
      ) : (
        <Mic className="h-4 w-4" />
      )}
    </button>
  )
}
