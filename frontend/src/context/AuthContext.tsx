import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'
import { authApi } from '@/api'
import type { AuthUser } from '@/types'

interface AuthContextValue {
  user: AuthUser | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)

  const checkSession = useCallback(async (isInitial = false) => {
    try {
      const me = await authApi.me()
      setUser(me)
    } catch (err: any) {
      // Only clear user on the initial session probe (no prior state) or on
      // an explicit auth-expired signal (handled by window event listener).
      // Network blips / 502s during a deploy must NOT log the user out.
      if (isInitial) {
        setUser(null)
      }
      // For non-initial checks: keep existing user, let auth:expired clear it
      // if and only if the backend truly invalidated the session.
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    checkSession(true)  // initial probe — clears user if unauthed
    const handler = () => setUser(null)
    window.addEventListener('auth:expired', handler)
    // Touch /me every 5 minutes so last_seen_at stays fresh.
    // Pass isInitial=false so transient failures (deploy windows, network
    // blips) don't log the user out — only real 401s via auth:expired.
    const interval = window.setInterval(() => { checkSession(false) }, 5 * 60 * 1000)
    return () => {
      window.removeEventListener('auth:expired', handler)
      window.clearInterval(interval)
    }
  }, [checkSession])

  const login = async (username: string, password: string) => {
    const me = await authApi.login(username, password)
    setUser(me)
  }

  const logout = async () => {
    await authApi.logout()
    setUser(null)
  }

  return (
    <AuthContext.Provider value={{ user, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
