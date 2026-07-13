import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import { App } from './App'
import { queryClient } from './lib/queryClient'

// These tests exercise the GATE → reveal → launch-banner flow, not the app's pages (which have
// their own tests). Stub the default route so revealing MainApp doesn't drag in BrowsePage's data
// fetching, and stub the WS-backed hooks (jsdom has no WebSocket).
vi.mock('@/pages/BrowsePage', () => ({ BrowsePage: () => <div>Browse</div> }))
vi.mock('./hooks/useIndexerStatus', () => ({
  useIndexerStatus: vi.fn(() => ({ status: null, connected: false })),
}))
vi.mock('./hooks/useSetupWS', () => ({
  useSetupWS: vi.fn(() => ({ models: null, complete: false })),
}))

// URL-aware fetch: /health drives the gate → ready; /api/setup/status decides first-run vs warm
// launch (models present → straight to the app). Any other API call the mounted app makes returns
// a benign empty payload so MainApp renders without throwing.
function routedFetch(setup: () => unknown) {
  return vi.fn((input: RequestInfo | URL) => {
    const url = String(input)
    if (url.includes('/api/setup/status')) {
      return Promise.resolve({ ok: true, json: async () => setup() } as Response)
    }
    if (url.includes('/health')) {
      return Promise.resolve({ ok: true, json: async () => ({ status: 'ready' }) } as Response)
    }
    return Promise.resolve({ ok: true, json: async () => ({}) } as Response)
  })
}

// The gate→reveal chain is a fixed multi-hop async chain (/health → /api/setup/status → reveal);
// fake timers let us own the single clock and drain each hop deterministically rather than betting
// the tail settles inside a wall-clock budget on a loaded runner (the historical ~1/168 flake).
async function settleLaunchChain() {
  for (let i = 0; i < 8; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100)
    })
  }
}

// jsdom has no IntersectionObserver; BrowsePage (the default /browse route the revealed app mounts)
// uses it for infinite scroll — stub it the same way the page tests do.
class MockIntersectionObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return []
  }
}

describe('App launch flow', () => {
  beforeEach(() => {
    vi.stubGlobal('IntersectionObserver', MockIntersectionObserver)
    vi.useFakeTimers()
    queryClient.clear()
    window.history.replaceState({}, '', '/browse')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  it('reveals the app and shows the launch banner once the backend and models are ready', async () => {
    window.history.replaceState({}, '', '/browse?launch=1')
    // Warm launch: /health ready and the models are already on disk, so the gate reveals AppInner.
    vi.stubGlobal('fetch', routedFetch(() => ({ ready: true, models: [] })))

    render(<App />)
    await settleLaunchChain()

    expect(screen.getByLabelText(/media search agent launch splash/i)).toBeInTheDocument()
    // App strips ?launch on mount via history.replaceState.
    expect(window.location.search).toBe('')
  })

  it('dismisses the launch banner', async () => {
    window.history.replaceState({}, '', '/browse?launch=1')
    vi.stubGlobal('fetch', routedFetch(() => ({ ready: true, models: [] })))

    render(<App />)
    await settleLaunchChain()

    expect(screen.getByLabelText(/media search agent launch splash/i)).toBeInTheDocument()
    // fireEvent (not userEvent) so the click dispatches synchronously and never stalls on the
    // frozen fake clock; the onClick state update (splash → hidden) is act-flushed.
    fireEvent.click(screen.getByRole('button', { name: /dismiss launch banner/i }))
    expect(screen.queryByLabelText(/media search agent launch splash/i)).not.toBeInTheDocument()
  })

  it('stays on the splash (app gated) while first-run model downloads are still needed', async () => {
    // First run: /health ready but models missing → the gate holds on the in-splash model download;
    // the main app (its nav) never mounts until the models are ready.
    vi.stubGlobal(
      'fetch',
      routedFetch(() => ({
        ready: false,
        models: [
          { id: 'clip', label: 'Semantic search model (CLIP ViT-L-14)', size_mb: 850, present: false, integrity_hint: '', source: 'huggingface.co/timm/x' },
        ],
      })),
    )

    render(<App />)
    await settleLaunchChain()

    expect(screen.getByText('Downloading AI models')).toBeInTheDocument()
    // The app shell (main navigation) must not have mounted behind the gate.
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })
})
