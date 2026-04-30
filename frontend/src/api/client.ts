// v3.0.16: detect the SPA's URL prefix at runtime (e.g. /llm-proxy2 or
// /llm-proxy2-smoke) so a single built bundle works at any mount point.
// See src/lib/basePath.ts for the detection logic.
import { getBasePath } from '@/lib/basePath'
const BASE = getBasePath()

async function req<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method,
    credentials: 'include',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 401) {
    // Any 401 means the server no longer accepts our session. Fire
    // auth:expired so the UI shows the login screen instead of a generic
    // "Unauthorized" toast on an otherwise broken page.
    //
    // 403 (forbidden-for-this-role) is different and stays as-is — that's
    // a per-action permission error, not a session-level issue.
    window.dispatchEvent(new CustomEvent('auth:expired'))
    throw new Error('Session expired — please sign in again')
  }

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText)
    let detail = text
    try { detail = JSON.parse(text).detail ?? text } catch { /* */ }
    throw new Error(detail)
  }

  const ct = res.headers.get('content-type') ?? ''
  return ct.includes('application/json') ? res.json() : (res.text() as unknown as T)
}

export const api = {
  get:    <T>(path: string)                => req<T>('GET',    path),
  post:   <T>(path: string, body?: unknown) => req<T>('POST',   path, body),
  put:    <T>(path: string, body: unknown)  => req<T>('PUT',    path, body),
  patch:  <T>(path: string, body?: unknown) => req<T>('PATCH',  path, body),
  delete: <T>(path: string)                => req<T>('DELETE',  path),
}
