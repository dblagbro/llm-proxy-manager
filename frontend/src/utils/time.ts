/**
 * Per-user time formatting (v3.0 R1).
 *
 * Two prefs from `useAuth().user`:
 *   - timezone:    IANA name (e.g. 'America/New_York') or null = browser default
 *   - time_format: '12h' | '24h' | null = locale default
 *
 * Server time is always UTC. This module converts UTC → user-preferred display.
 *
 * Three modes:
 *   - 'datetime' — full date + time
 *   - 'time'     — time only (hh:mm:ss)
 *   - 'date'     — date only
 *
 * Inputs:
 *   - ISO 8601 string  ('2026-04-29T18:32:11Z')
 *   - Unix seconds     (number, < 1e12)
 *   - Unix milliseconds (number, ≥ 1e12)
 *   - Date object
 *   - null/undefined   → returns '—'
 */
import type { AuthUser } from '@/types'

export type TimeMode = 'datetime' | 'time' | 'date'

export interface TimePrefs {
  timezone?: string | null
  time_format?: '12h' | '24h' | null
}

function toDate(input: string | number | Date | null | undefined): Date | null {
  if (input == null) return null
  if (input instanceof Date) return Number.isNaN(input.getTime()) ? null : input
  if (typeof input === 'number') {
    // Unix seconds vs. ms: treat anything < 1e12 as seconds
    const ms = input < 1e12 ? input * 1000 : input
    const d = new Date(ms)
    return Number.isNaN(d.getTime()) ? null : d
  }
  const d = new Date(input)
  return Number.isNaN(d.getTime()) ? null : d
}

function intlOpts(prefs: TimePrefs, mode: TimeMode): Intl.DateTimeFormatOptions {
  const opts: Intl.DateTimeFormatOptions = {}
  if (prefs.timezone) opts.timeZone = prefs.timezone
  if (prefs.time_format === '24h') opts.hour12 = false
  else if (prefs.time_format === '12h') opts.hour12 = true
  // null → leave undefined → locale default
  if (mode === 'date' || mode === 'datetime') {
    opts.year = 'numeric'
    opts.month = 'short'
    opts.day = '2-digit'
  }
  if (mode === 'time' || mode === 'datetime') {
    opts.hour = '2-digit'
    opts.minute = '2-digit'
    opts.second = '2-digit'
  }
  return opts
}

export function formatTime(
  input: string | number | Date | null | undefined,
  prefs: TimePrefs = {},
  mode: TimeMode = 'datetime',
): string {
  const d = toDate(input)
  if (!d) return '—'
  return d.toLocaleString([], intlOpts(prefs, mode))
}

/** Convenience: pull prefs straight from an AuthUser without ceremony. */
export function formatTimeForUser(
  input: string | number | Date | null | undefined,
  user: AuthUser | null | undefined,
  mode: TimeMode = 'datetime',
): string {
  return formatTime(input, {
    timezone: user?.timezone ?? null,
    time_format: user?.time_format ?? null,
  }, mode)
}

/** A list of common IANA zones for the preferences UI. Not exhaustive —
 * advanced users can type any IANA zone name in the input. */
export const COMMON_TIMEZONES: { value: string; label: string }[] = [
  { value: '',                    label: 'Browser default' },
  { value: 'UTC',                 label: 'UTC' },
  { value: 'America/Los_Angeles', label: 'US Pacific (Los Angeles)' },
  { value: 'America/Denver',      label: 'US Mountain (Denver)' },
  { value: 'America/Chicago',     label: 'US Central (Chicago)' },
  { value: 'America/New_York',    label: 'US Eastern (New York)' },
  { value: 'Europe/London',       label: 'UK (London)' },
  { value: 'Europe/Berlin',       label: 'Central Europe (Berlin)' },
  { value: 'Europe/Athens',       label: 'Eastern Europe (Athens)' },
  { value: 'Asia/Kolkata',        label: 'India (Kolkata)' },
  { value: 'Asia/Singapore',      label: 'Singapore' },
  { value: 'Asia/Tokyo',          label: 'Japan (Tokyo)' },
  { value: 'Australia/Sydney',    label: 'Australia (Sydney)' },
]
