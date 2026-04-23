// Vite sets import.meta.env.BASE_URL to the configured base (e.g. '/llm-proxy2/').
// Strip the trailing slash so we can prefix '/api/...' cleanly.
const BASE = import.meta.env.BASE_URL.replace(/\/$/, '')

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
    // Only treat the /api/auth/me probe as a "session expired" signal — other
    // endpoints can return 401 for auth-scoped reasons (e.g. attempting a
    // reveal as a non-admin, or during a brief backend race) and shouldn't
    // log the user out of the whole UI.
    if (path === '/api/auth/me') {
      window.dispatchEvent(new CustomEvent('auth:expired'))
    }
    throw new Error('Unauthorized')
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
