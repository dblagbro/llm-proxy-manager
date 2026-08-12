import { useEffect, useState } from 'react'
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
  // v5.22.9 — SSO button, rendered only when the server says it's configured.
  const [sso, setSso] = useState<{ enabled: boolean; label: string } | null>(null)
  const params = new URLSearchParams(window.location.search)
  const ssoError = params.get('sso_error')

  useEffect(() => {
    authApi.ssoConfig().then(setSso).catch(() => setSso({ enabled: false, label: '' }))
  }, [])

  const SSO_ERRORS: Record<string, string> = {
    no_account: 'That Google account is not linked to a user here. Ask an administrator to add it.',
    domain: 'That email domain is not allowed to sign in.',
    unverified: 'That Google account has an unverified email address.',
    no_email: 'Google did not return an email address for that account.',
    denied: 'Sign-in was cancelled.',
    expired: 'That sign-in attempt expired. Please try again.',
    idp_unreachable: 'Could not reach Google. Please try again.',
    exchange_failed: 'Sign-in could not be completed. Please try again.',
    invalid: 'Sign-in could not be verified. Please try again.',
  }

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
            {(error || ssoError) && (
              <p className="text-sm text-red-400 bg-red-900/20 border border-red-800 rounded-lg px-3 py-2">
                {error || SSO_ERRORS[ssoError || ''] || 'Sign-in failed.'}
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
          {sso?.enabled && (
            <>
              <div className="flex items-center gap-3 my-4">
                <div className="h-px flex-1 bg-gray-800" />
                <span className="text-xs text-gray-500">or</span>
                <div className="h-px flex-1 bg-gray-800" />
              </div>
              <a
                href={`${import.meta.env.BASE_URL.replace(/\/$/, '')}/api/auth/sso/start`}
                className="flex items-center justify-center gap-2 w-full rounded-lg border border-gray-700 bg-gray-800 hover:bg-gray-750 px-4 py-2 text-sm font-medium text-gray-100 transition-colors"
              >
                <svg className="h-4 w-4" viewBox="0 0 48 48" aria-hidden="true">
                  <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9 3.6l6.7-6.7C35.6 2.6 30.2 0 24 0 14.6 0 6.5 5.4 2.6 13.2l7.8 6.1C12.3 13.2 17.7 9.5 24 9.5z"/>
                  <path fill="#4285F4" d="M46.1 24.6c0-1.6-.1-3.1-.4-4.6H24v9.1h12.4c-.5 2.9-2.1 5.4-4.6 7l7.2 5.6c4.2-3.9 6.6-9.6 6.6-17.1z"/>
                  <path fill="#FBBC05" d="M10.4 28.7a14.6 14.6 0 0 1 0-9.4l-7.8-6.1a24 24 0 0 0 0 21.6l7.8-6.1z"/>
                  <path fill="#34A853" d="M24 48c6.2 0 11.5-2 15.3-5.6l-7.2-5.6c-2 1.4-4.7 2.3-8.1 2.3-6.3 0-11.7-3.7-13.6-9.1l-7.8 6.1C6.5 42.6 14.6 48 24 48z"/>
                </svg>
                {sso.label || 'Sign in with Google'}
              </a>
            </>
          )}
          </div>
          )}
        </div>


      </div>
    </div>
  )
}
