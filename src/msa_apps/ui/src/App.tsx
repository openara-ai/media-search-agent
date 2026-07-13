import { useCallback, useEffect, useState } from 'react'
import { QueryClientProvider } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { queryClient } from '@/lib/queryClient'
import { Layout } from '@/components/layout/Layout'
import { LaunchBanner } from '@/components/layout/LaunchBanner'
import { SearchPage } from '@/pages/SearchPage'
import { BrowsePage } from '@/pages/BrowsePage'
import { PeoplePage } from '@/pages/PeoplePage'
import { IndexerPage } from '@/pages/IndexerPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { StartupGate } from '@/components/StartupGate'

function RootRedirect() {
  const { search } = useLocation()
  return <Navigate to={`/browse${search}`} replace />
}

function MainApp() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<Layout />}>
          <Route index element={<RootRedirect />} />
          <Route path="search"   element={<SearchPage />} />
          <Route path="browse"   element={<BrowsePage />} />
          <Route path="people"   element={<PeoplePage />} />
          <Route path="indexer"  element={<IndexerPage />} />
          <Route path="settings" element={<SettingsPage />} />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}

function AppInner() {
  const [launchSplashVisible, setLaunchSplashVisible] = useState(false)

  useEffect(() => {
    const url = new URL(window.location.href)
    if (url.searchParams.get('launch') !== '1') {
      return
    }

    setLaunchSplashVisible(true)
    url.searchParams.delete('launch')
    window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`)
  }, [])

  const dismissLaunchSplash = useCallback(() => setLaunchSplashVisible(false), [])

  // StartupGate guarantees the backend is up AND the AI models are ready before AppInner mounts,
  // so there is no setup/status gating here anymore — just the main app + the launch banner.
  return (
    <>
      <MainApp />
      <LaunchBanner visible={launchSplashVisible} onDismiss={dismissLaunchSplash} />
    </>
  )
}

export function App() {
  // StartupGate owns the entire first run in one splash: it polls /health for provisioning, then
  // gates on /api/setup/status + /ws/setup for the AI-model download, revealing AppInner only once
  // both are done (M-7 · spec §S-2). In browser/dev mode /health is same-origin and already ready
  // and the models are usually present, so the gate is invisible.
  return (
    <QueryClientProvider client={queryClient}>
      <StartupGate>
        <AppInner />
      </StartupGate>
    </QueryClientProvider>
  )
}
