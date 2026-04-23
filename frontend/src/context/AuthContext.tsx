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

  const checkSession = useCallback(async () => {
    try {
      const me = await authApi.me()
      setUser(me)
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    checkSession()
    const handler = () => setUser(null)
    window.addEventListener('auth:expired', handler)
    // Touch /me every 5 minutes so last_seen_at stays fresh.
    // Backend idle TTL is 7 days; we just need to show the user the session
    // is still valid without waiting for them to navigate somewhere.
    const interval = window.setInterval(() => { checkSession() }, 5 * 60 * 1000)
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
