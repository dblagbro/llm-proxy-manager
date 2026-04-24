import { NavLink } from 'react-router-dom'
import { clsx } from 'clsx'
import { useQuery } from '@tanstack/react-query'
import {
  LayoutDashboard, Server, GitBranch, Key, Users,
  Network, BarChart2, Activity, Settings, ChevronLeft, ChevronRight,
  Zap, Fingerprint,
} from 'lucide-react'
import { clusterApi } from '@/api'

interface NavItem {
  to: string
  icon: React.ElementType
  label: string
  badge?: string
  hidden?: boolean
}

interface SidebarProps {
  collapsed: boolean
  onToggle: () => void
  clusterEnabled?: boolean
  openCircuitBreakers?: number
  liveActivity?: boolean
}

export function Sidebar({ collapsed, onToggle, openCircuitBreakers = 0, liveActivity = false }: Omit<SidebarProps, 'clusterEnabled'> & { clusterEnabled?: boolean }) {
  const { data: health } = useQuery({
    queryKey: ['sidebar-health'],
    queryFn: clusterApi.health,
    refetchInterval: 60_000,
    staleTime: 30_000,
  })
  const version = health?.version ? `v${health.version}` : 'v2.x'
  const navItems: (NavItem | 'divider')[] = [
    { to: '/',         icon: LayoutDashboard, label: 'Dashboard' },
    { to: '/providers', icon: Server,          label: 'Providers',
      badge: openCircuitBreakers > 0 ? String(openCircuitBreakers) : undefined },
    { to: '/routing',  icon: GitBranch,        label: 'Routing / LMRH' },
    'divider',
    { to: '/keys',     icon: Key,              label: 'API Keys' },
    { to: '/users',    icon: Users,            label: 'Users' },
    'divider',
    { to: '/cluster',  icon: Network,          label: 'Cluster' },
    { to: '/metrics',  icon: BarChart2,        label: 'Metrics' },
    { to: '/activity', icon: Activity,         label: 'Activity',
      badge: liveActivity ? '●' : undefined },
    'divider',
    { to: '/oauth-capture', icon: Fingerprint, label: 'OAuth Capture' },
    { to: '/settings', icon: Settings,         label: 'Settings' },
  ]

  return (
    <aside className={clsx(
      'flex flex-col bg-gray-900 border-r border-gray-800 transition-all duration-200 shrink-0',
      collapsed ? 'w-14' : 'w-56'
    )}>
      {/* Logo */}
      <div className={clsx('flex items-center gap-2 px-3 py-4 border-b border-gray-800', collapsed && 'justify-center')}>
        <div className="flex items-center justify-center h-8 w-8 bg-indigo-600 rounded-lg shrink-0">
          <Zap className="h-4 w-4 text-white" />
        </div>
        {!collapsed && (
          <div className="min-w-0">
            <p className="text-sm font-bold text-white truncate">llm-proxy</p>
            <p className="text-xs text-gray-400">{version}</p>
          </div>
        )}
      </div>

      {/* Nav */}
      <nav className="flex-1 overflow-y-auto py-3 space-y-0.5 px-2">
        {navItems.map((item, i) => {
          if (item === 'divider') {
            return <div key={i} className="h-px bg-gray-800 mx-1 my-2" />
          }
          if (item.hidden) return null
          const Icon = item.icon
          return (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === '/'}
              className={({ isActive }) => clsx(
                'flex items-center gap-2.5 px-2 py-2 rounded-lg text-sm transition-colors group',
                collapsed && 'justify-center',
                isActive
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-400 hover:text-white hover:bg-gray-800'
              )}
              title={collapsed ? item.label : undefined}
            >
              <Icon className="h-4 w-4 shrink-0" />
              {!collapsed && (
                <>
                  <span className="flex-1 truncate">{item.label}</span>
                  {item.badge && (
                    <span className={clsx(
                      'text-xs px-1.5 py-0.5 rounded-full font-medium shrink-0',
                      item.badge === '●' ? 'text-green-400 text-base leading-none' : 'bg-red-500 text-white'
                    )}>
                      {item.badge}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          )
        })}
      </nav>

      {/* Collapse toggle */}
      <button
        onClick={onToggle}
        className="flex items-center justify-center p-3 border-t border-gray-800 text-gray-500 hover:text-white hover:bg-gray-800 transition-colors"
      >
        {collapsed ? <ChevronRight className="h-4 w-4" /> : <ChevronLeft className="h-4 w-4" />}
      </button>
    </aside>
  )
}
