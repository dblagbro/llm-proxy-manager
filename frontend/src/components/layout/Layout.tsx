import { useState, useEffect, useRef } from 'react'
import { Outlet } from 'react-router-dom'
import { RefreshCw, X } from 'lucide-react'
import { Sidebar } from './Sidebar'
import { TopBar } from './TopBar'
import { useQuery } from '@tanstack/react-query'
import { clusterApi } from '@/api'

export function Layout() {
  const [collapsed, setCollapsed] = useState(() =>
    localStorage.getItem('sidebar-collapsed') === 'true'
  )

  const toggle = () => {
    const next = !collapsed
    setCollapsed(next)
    localStorage.setItem('sidebar-collapsed', String(next))
  }

  const { data: health } = useQuery({
    queryKey: ['health'],
    queryFn: clusterApi.health,
    refetchInterval: 15_000,
  })

  const openCircuitBreakers = Object.values(health?.circuitBreakers ?? {})
    .filter(cb => cb.state === 'open').length

  // v3.0.9: stale-bundle detection. We don't bake the build version into
  // the bundle — instead, on first successful /health response we record
  // the server version as the "loaded" baseline. Every subsequent poll
  // compares; if the server version differs, this bundle is stale (the
  // server has been redeployed) and we surface a refresh banner.
  const loadedVersionRef = useRef<string | null>(null)
  const [staleVersion, setStaleVersion] = useState<string | null>(null)
  const [bannerDismissed, setBannerDismissed] = useState(false)

  useEffect(() => {
    if (!health?.version) return
    if (loadedVersionRef.current === null) {
      loadedVersionRef.current = health.version
      return
    }
    if (health.version !== loadedVersionRef.current) {
      setStaleVersion(health.version)
    }
  }, [health?.version])

  return (
    <div className="flex h-screen overflow-hidden bg-gray-50 dark:bg-gray-950">
      <Sidebar
        collapsed={collapsed}
        onToggle={toggle}
        clusterEnabled={false}  // TODO: pull from settings
        openCircuitBreakers={openCircuitBreakers}
      />
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden">
        <TopBar />
        {staleVersion && !bannerDismissed && (
          <div className="bg-indigo-600 text-white px-4 py-2 flex items-center gap-3 text-sm shrink-0">
            <RefreshCw className="h-4 w-4 shrink-0" />
            <span className="flex-1 min-w-0">
              <span className="font-medium">New version available</span>
              <span className="text-indigo-200 ml-2 font-mono">
                {loadedVersionRef.current} → {staleVersion}
              </span>
              <span className="text-indigo-100 ml-2 hidden sm:inline">
                — your tab is running an older bundle. Reload to pick up the latest UI.
              </span>
            </span>
            <button
              onClick={() => window.location.reload()}
              className="bg-white text-indigo-700 hover:bg-indigo-50 px-3 py-1 rounded text-xs font-medium shrink-0"
            >
              Reload now
            </button>
            <button
              onClick={() => setBannerDismissed(true)}
              className="text-indigo-100 hover:text-white shrink-0"
              aria-label="Dismiss"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        )}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
