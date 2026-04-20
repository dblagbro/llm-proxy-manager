import { useQuery } from '@tanstack/react-query'
import { Sun, Moon, LogOut, User, RefreshCw } from 'lucide-react'
import { clsx } from 'clsx'
import { useAuth } from '@/context/AuthContext'
import { useTheme } from '@/context/ThemeContext'
import { clusterApi } from '@/api'
import { Badge } from '@/components/ui/Badge'

export function TopBar() {
  const { user, logout } = useAuth()
  const { theme, toggle } = useTheme()

  const { data: health, refetch } = useQuery({
    queryKey: ['health'],
    queryFn: clusterApi.health,
    refetchInterval: 15_000,
  })

  const statusVariant = !health
    ? 'muted'
    : health.healthyProviders === 0
    ? 'danger'
    : health.healthyProviders < health.totalProviders
    ? 'warning'
    : 'success'

  const statusLabel = !health
    ? 'Connecting…'
    : `${health.healthyProviders}/${health.totalProviders} providers`

  return (
    <header className="flex items-center justify-between px-6 py-3 bg-white dark:bg-gray-900 border-b border-gray-200 dark:border-gray-800 shrink-0 gap-4">
      {/* Health status */}
      <div className="flex items-center gap-3">
        <Badge variant={statusVariant} size="md">
          <span className={clsx(
            'inline-block h-1.5 w-1.5 rounded-full mr-1.5',
            statusVariant === 'success' ? 'bg-green-500' : statusVariant === 'warning' ? 'bg-amber-500' : statusVariant === 'danger' ? 'bg-red-500' : 'bg-gray-400'
          )} />
          {statusLabel}
        </Badge>

        {health && Object.values(health.circuitBreakers ?? {}).some(cb => cb.state === 'open') && (
          <Badge variant="danger" size="md">
            ⚡ Provider tripped
          </Badge>
        )}
      </div>

      {/* Right actions */}
      <div className="flex items-center gap-2">
        <button
          onClick={() => refetch()}
          className="p-2 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          title="Refresh health"
        >
          <RefreshCw className="h-4 w-4" />
        </button>

        <button
          onClick={toggle}
          className="p-2 rounded-lg text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
          title={theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
        </button>

        {/* User menu */}
        <div className="flex items-center gap-2 pl-2 border-l border-gray-200 dark:border-gray-700">
          <div className="flex items-center gap-2 px-2 py-1.5 rounded-lg text-sm text-gray-700 dark:text-gray-300">
            <User className="h-4 w-4" />
            <span className="hidden sm:block font-medium">{user?.username}</span>
            {user?.role && (
              <span className={`text-xs font-semibold px-2 py-0.5 rounded-full text-white ${user.role === 'admin' ? 'bg-indigo-600' : 'bg-gray-500'}`}>
                {user.role}
              </span>
            )}
          </div>
          <button
            onClick={logout}
            className="p-2 rounded-lg text-gray-400 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-900/20 transition-colors"
            title="Sign out"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </header>
  )
}
