import { useState } from 'react'
import { Navigate } from 'react-router-dom'
import { Zap } from 'lucide-react'
import { useAuth } from '@/context/AuthContext'
import { authApi } from '@/api'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'

export function LoginPage() {
  const { user, login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  // v5.22.7 — "Forgot password?" panel. The request endpoint deliberately
  // returns the same response whether or not the account exists, so we show
  // one fixed confirmation and never reveal which case it was.
  const [mode, setMode] = useState<'signin' | 'forgot'>('signin')
  const [identifier, setIdentifier] = useState('')
  const [sent, setSent] = useState(false)

  const handleForgot = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await authApi.requestReset(identifier)
      setSent(true)
    } catch {
      // Even a transport failure shows the same message — see note above.
      setSent(true)
    } finally {
      setLoading(false)
    }
  }

  if (user) return <Navigate to="/" replace />

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        {/* Logo */}
        <div className="flex flex-col items-center mb-8">
          <div className="flex items-center justify-center h-14 w-14 bg-indigo-600 rounded-2xl mb-4 shadow-lg">
            <Zap className="h-7 w-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">llm-proxy</h1>
          <p className="text-sm text-gray-400 mt-1">LLM Routing Gateway</p>
        </div>

        {/* Form */}
        <div className="bg-gray-900 rounded-2xl border border-gray-800 shadow-2xl p-6">
          {mode === 'forgot' ? (
            <div>
              <h2 className="text-base font-semibold text-gray-100 mb-5">Reset your password</h2>
              {sent ? (
                <>
                  <p className="text-sm text-gray-300 bg-gray-800/60 border border-gray-700 rounded-lg px-3 py-3">
                    If that account exists and has an email address on file, a reset
                    link has been sent. The link expires in 30 minutes.
                  </p>
                  <button
                    type="button"
                    onClick={() => { setMode('signin'); setSent(false); setIdentifier('') }}
                    className="text-sm text-indigo-400 hover:text-indigo-300 mt-4"
                  >
                    Back to sign in
                  </button>
                </>
              ) : (
                <form onSubmit={handleForgot} className="flex flex-col gap-4">
                  <Input
                    label="Username or email"
                    value={identifier}
                    onChange={e => setIdentifier(e.target.value)}
                    autoFocus
                    autoComplete="username"
                    required
                  />
                  <Button type="submit" loading={loading} className="w-full mt-1">
                    Send reset link
                  </Button>
                  <button
                    type="button"
                    onClick={() => { setMode('signin'); setError('') }}
                    className="text-sm text-gray-400 hover:text-gray-300"
                  >
                    Back to sign in
                  </button>
                </form>
              )}
            </div>
          ) : (
          <div>
          <h2 className="text-base font-semibold text-gray-100 mb-5">Sign in</h2>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <Input
              label="Username"
              value={username}
              onChange={e => setUsername(e.target.value)}
              autoFocus
              autoComplete="username"
              required
            />
            <Input
              label="Password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              autoComplete="current-password"
              required
            />
            {error && (
              <p className="text-sm text-red-400 bg-red-900/20 border border-red-800 rounded-lg px-3 py-2">
                {error}
              </p>
            )}
            <Button type="submit" loading={loading} className="w-full mt-1">
              Sign in
            </Button>
            <button
              type="button"
              onClick={() => { setMode('forgot'); setError('') }}
              className="text-sm text-indigo-400 hover:text-indigo-300"
            >
              Forgot password?
            </button>
          </form>
          </div>
          )}
        </div>


      </div>
    </div>
  )
}
