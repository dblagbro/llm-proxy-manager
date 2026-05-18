/**
 * AIRI voice output — speaker toggle (v4.3).
 *
 * An opt-in toggle beside the mic. When on, each completed AIRI answer is
 * synthesized to speech by Piper — via POST /api/airi/speak, self-hosted on
 * the whisper-bridge sidecar (see docs/4.3-tts-design.md) — and played back.
 *
 * The parent calls speak() through the ref when an assistant message
 * finishes streaming; this component owns the toggle state, the <audio>
 * element, and the synthesize-and-play flow. A new utterance supersedes
 * whatever is playing, so AIRI never talks over itself. Renders only when
 * airi_tts_enabled is on (the parent gates this). A synthesis or playback
 * failure is surfaced as a quiet note and never disrupts the chat.
 */
import {
  useState, useRef, useCallback, useEffect, useImperativeHandle, forwardRef,
} from 'react'
import { Volume2, VolumeX, Loader2, Square } from 'lucide-react'
import { getBasePath } from '@/lib/basePath'

export type AiriSpeakerHandle = {
  /** Synthesize and play one AIRI answer. No-op while the toggle is off. */
  speak: (text: string) => void
  /** Halt any in-progress synthesis/playback. */
  stop: () => void
}

type SpeakerState = 'off' | 'idle' | 'synthesizing' | 'speaking'

export const AiriSpeaker = forwardRef<AiriSpeakerHandle, {
  onError: (message: string) => void
}>(function AiriSpeaker({ onError }, ref) {
  const [state, setState] = useState<SpeakerState>('off')
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const urlRef = useRef<string | null>(null)
  // speak()/handlers are long-lived — they read the live toggle via this ref.
  const stateRef = useRef<SpeakerState>('off')
  useEffect(() => {
    stateRef.current = state
  }, [state])

  // A function call, not an inline `stateRef.current === 'off'` — the latter
  // lets TypeScript narrow the ref across an await, which it must not (the
  // toggle can change while we synthesize).
  const isOff = useCallback(() => stateRef.current === 'off', [])

  const releaseUrl = useCallback(() => {
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current)
      urlRef.current = null
    }
  }, [])

  const stop = useCallback(() => {
    const a = audioRef.current
    if (a) {
      try { a.pause(); a.currentTime = 0 } catch { /* ignore */ }
    }
    releaseUrl()
    if (!isOff()) setState('idle')
  }, [releaseUrl, isOff])

  const speak = useCallback(
    async (text: string) => {
      if (isOff()) return // toggle is off — stay silent
      const clean = (text || '').trim()
      if (!clean) return
      // a new utterance supersedes whatever is playing
      try { audioRef.current?.pause() } catch { /* ignore */ }
      releaseUrl()
      setState('synthesizing')
      try {
        const res = await fetch(`${getBasePath()}/api/airi/speak`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: clean }),
        })
        if (!res.ok) throw new Error(`HTTP ${res.status}`)
        const blob = await res.blob()
        if (isOff()) return // toggled off mid-synthesis
        const url = URL.createObjectURL(blob)
        urlRef.current = url
        const audio = (audioRef.current ||= new Audio())
        audio.onended = () => {
          releaseUrl()
          if (!isOff()) setState('idle')
        }
        audio.onerror = () => {
          releaseUrl()
          if (!isOff()) setState('idle')
        }
        audio.src = url
        setState('speaking')
        await audio.play()
      } catch {
        releaseUrl()
        if (!isOff()) setState('idle')
        onError('AIRI could not read that answer aloud.')
      }
    },
    [releaseUrl, onError, isOff],
  )

  useImperativeHandle(ref, () => ({ speak, stop }), [speak, stop])

  // Release the audio resources when the panel unmounts.
  useEffect(
    () => () => {
      try { audioRef.current?.pause() } catch { /* ignore */ }
      releaseUrl()
    },
    [releaseUrl],
  )

  const onClick = () => {
    if (state === 'off') {
      setState('idle') // turn spoken replies on
    } else if (state === 'speaking' || state === 'synthesizing') {
      stop() // halt the current utterance, stay on
    } else {
      stop() // idle + on -> turn off
      setState('off')
    }
  }

  const on = state !== 'off'
  const busyState = state === 'synthesizing' || state === 'speaking'
  const label =
    state === 'off'
      ? 'Enable spoken replies'
      : state === 'synthesizing'
        ? 'Preparing speech…'
        : state === 'speaking'
          ? 'Stop speaking'
          : 'Spoken replies on — AIRI reads answers aloud'

  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      aria-pressed={on}
      className={
        'shrink-0 rounded-md px-3 py-2 flex items-center justify-center ' +
        'transition-colors ' +
        (busyState
          ? 'bg-blue-600 text-white animate-pulse'
          : on
            ? 'bg-blue-50 dark:bg-blue-900/40 text-blue-700 dark:text-blue-300 ' +
              'border border-blue-300 dark:border-blue-700'
            : 'border border-gray-300 dark:border-gray-600 text-gray-600 ' +
              'dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700')
      }
    >
      {state === 'synthesizing' ? (
        <Loader2 className="h-4 w-4 animate-spin" />
      ) : state === 'speaking' ? (
        <Square className="h-4 w-4" />
      ) : on ? (
        <Volume2 className="h-4 w-4" />
      ) : (
        <VolumeX className="h-4 w-4" />
      )}
    </button>
  )
})
