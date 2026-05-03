import { useCallback, useEffect, useState } from 'react'
import { QueryClientProvider, useQuery } from '@tanstack/react-query'
import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { queryClient } from '@/lib/queryClient'
import { Layout } from '@/components/layout/Layout'
import { LaunchBanner } from '@/components/layout/LaunchBanner'
import { SearchPage } from '@/pages/SearchPage'
import { BrowsePage } from '@/pages/BrowsePage'
import { PeoplePage } from '@/pages/PeoplePage'
import { IndexerPage } from '@/pages/IndexerPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { SetupPage } from '@/pages/SetupPage'
import { fetchSetupStatus } from '@/api/setup'
import type { SetupStatus } from '@/api/setup'

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
  const [setupDone, setSetupDone] = useState(false)
  const [launchSplashVisible, setLaunchSplashVisible] = useState(false)

  const { data, isLoading, isError, refetch } = useQuery<SetupStatus>({
    queryKey: ['setup/status'],
    queryFn: fetchSetupStatus,
    // Retry while the API is still starting up
    retry: 10,
    retryDelay: 1000,
    // Never re-fetch automatically once we have an answer
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  })

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
  const launchSplash = (
    <LaunchBanner visible={launchSplashVisible} onDismiss={dismissLaunchSplash} />
  )

  if (isLoading) {
    return launchSplashVisible ? launchSplash : null
  }

  // Retries exhausted — API is unreachable. Show an explicit error rather than
  // a permanent blank screen so the user knows what happened and can retry.
  if (isError || !data) {
    return (
      <>
        <div className="fixed inset-0 flex flex-col items-center justify-center gap-4 bg-slate-950 text-slate-300">
          <p className="text-sm">Could not reach the Media Search Agent API.</p>
          <button
            type="button"
            onClick={() => refetch()}
            className="rounded-lg border border-slate-700 bg-slate-800 px-4 py-2 text-sm hover:bg-slate-700"
          >
            Retry
          </button>
        </div>
        {launchSplash}
      </>
    )
  }

  if (!data.ready && !setupDone) {
    return (
      <>
        <SetupPage
          initialModels={data.models}
          onComplete={() => setSetupDone(true)}
        />
        {launchSplash}
      </>
    )
  }

  return (
    <>
      <MainApp />
      {launchSplash}
    </>
  )
}

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppInner />
    </QueryClientProvider>
  )
}
