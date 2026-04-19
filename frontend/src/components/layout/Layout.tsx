import { useState } from 'react'
import { Outlet } from 'react-router-dom'
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
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
