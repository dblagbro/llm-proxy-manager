import { useState } from 'react'
import { Link, useSearchParams, useNavigate } from 'react-router-dom'
import { Zap } from 'lucide-react'
import { authApi } from '@/api'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'

/**
 * v5.22.7 — self-service password reset, step 2 (option B).
 *
 * Reached from the emailed link: /llm-proxy2/reset-password?token=...
 * The token is single-use and expires in 30 minutes; the server returns a
 * deliberately vague error for missing/used/expired so this page cannot be
 * used to probe which tokens ever existed.
 *
 * Unauthenticated route — it must sit OUTSIDE the ProtectedRoute wrapper in
 * App.tsx, alongside /login.
 */
export function ResetPasswordPage() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const token = params.get('token') || ''

  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [done, setDone] = useState(false)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setError('')
    if (password.length < 8) {
      setError('Password must be at least 8 characters')
      return
    }
    if (password !== confirm) {
      setError('Passwords do not match')
      return
    }
    setLoading(true)
    try {
      await authApi.confirmReset(token, password)
      setDone(true)
      setTimeout(() => navigate('/login'), 2500)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'This reset link is invalid or has expired')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-gray-950 p-4">
      <div className="w-full max-w-sm">
        <div className="flex flex-col items-center mb-8">
          <div className="flex items-center justify-center h-14 w-14 bg-indigo-600 rounded-2xl mb-4 shadow-lg">
            <Zap className="h-7 w-7 text-white" />
          </div>
          <h1 className="text-2xl font-bold text-white">llm-proxy</h1>
          <p className="text-sm text-gray-400 mt-1">LLM Routing Gateway</p>
        </div>

        <div className="bg-gray-900 rounded-2xl border border-gray-800 shadow-2xl p-6">
          <h2 className="text-base font-semibold text-gray-100 mb-5">Choose a new password</h2>

          {!token ? (
            <p className="text-sm text-red-400 bg-red-900/20 border border-red-800 rounded-lg px-3 py-2">
              This link is missing its token. Request a new reset link from the sign-in page.
            </p>
          ) : done ? (
            <p className="text-sm text-green-400 bg-green-900/20 border border-green-800 rounded-lg px-3 py-2">
              Password updated. Redirecting you to sign in…
            </p>
          ) : (
            <form onSubmit={handleSubmit} className="flex flex-col gap-4">
              <Input
                label="New password"
                type="password"
                value={password}
                onChange={e => setPassword(e.target.value)}
                autoFocus
                autoComplete="new-password"
                required
              />
              <Input
                label="Confirm new password"
                type="password"
                value={confirm}
                onChange={e => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
              {error && (
                <p className="text-sm text-red-400 bg-red-900/20 border border-red-800 rounded-lg px-3 py-2">
                  {error}
                </p>
              )}
              <Button type="submit" loading={loading} className="w-full mt-1">
                Set new password
              </Button>
            </form>
          )}

          <div className="mt-4">
            <Link to="/login" className="text-sm text-indigo-400 hover:text-indigo-300">
              Back to sign in
            </Link>
          </div>
        </div>
      </div>
    </div>
  )
}
