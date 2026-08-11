import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { AuthProvider, useAuth } from '@/context/AuthContext'
import { getBasePath } from '@/lib/basePath'
import { ThemeProvider } from '@/context/ThemeContext'
import { Toaster } from '@/components/ui/Toast'
import { Layout } from '@/components/layout/Layout'
import { LoginPage } from '@/pages/LoginPage'
import { ResetPasswordPage } from '@/pages/ResetPasswordPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { ProvidersPage } from '@/pages/ProvidersPage'
import { RoutingPage } from '@/pages/RoutingPage'
import { APIKeysPage } from '@/pages/APIKeysPage'
import { UsersPage } from '@/pages/UsersPage'
import { ClusterPage } from '@/pages/ClusterPage'
import { MetricsPage } from '@/pages/MetricsPage'
import { ActivityPage } from '@/pages/ActivityPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { MyCompliancePage } from '@/pages/MyCompliancePage'
import { CompliancePage } from '@/pages/CompliancePage'
import { McpPage } from '@/pages/McpPage'
import { IntegrationPage } from '@/pages/IntegrationPage'

const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, staleTime: 10_000 } },
})

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth()
  if (loading) return null
  if (!user) return <Navigate to="/login" replace />
  return <>{children}</>
}

// v5.0.0 — admin-only route gate. The admin compliance page reads
// /api/admin/* endpoints which the backend already enforces, but
// hiding the route entirely keeps non-admins out of a 403'd page.
function AdminGate({ children }: { children: React.ReactNode }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" replace />
  if (user.role !== 'admin') return <Navigate to="/" replace />
  return <>{children}</>
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      {/* v5.22.7 — unauthenticated: reached from the emailed reset link */}
      <Route path="/reset-password" element={<ResetPasswordPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="providers" element={<ProvidersPage />} />
        <Route path="routing" element={<RoutingPage />} />
        <Route path="keys" element={<APIKeysPage />} />
        <Route path="users" element={<UsersPage />} />
        <Route path="cluster" element={<ClusterPage />} />
        <Route path="metrics" element={<MetricsPage />} />
        <Route path="activity" element={<ActivityPage />} />
        <Route path="settings" element={<SettingsPage />} />
        <Route path="compliance" element={<MyCompliancePage />} />
        <Route path="admin/compliance" element={<AdminGate><CompliancePage /></AdminGate>} />
        <Route path="admin/mcp" element={<AdminGate><McpPage /></AdminGate>} />
        <Route path="admin/integration" element={<AdminGate><IntegrationPage /></AdminGate>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <BrowserRouter basename={getBasePath()}>
          <AuthProvider>
            <AppRoutes />
            <Toaster />
          </AuthProvider>
        </BrowserRouter>
      </ThemeProvider>
    </QueryClientProvider>
  )
}
