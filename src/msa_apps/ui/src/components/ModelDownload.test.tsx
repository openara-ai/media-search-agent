import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { ModelDownload } from './ModelDownload'
import * as setupWs from '../hooks/useSetupWS'
import { retrySetup, type ModelDownloadState } from '../api/setup'

vi.mock('../hooks/useSetupWS')
// Keep fetchSetupStatus real (it's driven via the global fetch stub); mock only retrySetup so the
// Retry button's backend call can be asserted without a network round-trip.
vi.mock('../api/setup', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/setup')>()
  return { ...actual, retrySetup: vi.fn() }
})

const MODELS = [
  { id: 'clip', label: 'Semantic search model (CLIP ViT-L-14)', size_mb: 850, present: false, integrity_hint: 'sha256:9ce2e8a8ebff', source: 'huggingface.co/timm/vit_large_patch14_clip_224.openai' },
  { id: 'rtdetr', label: 'Object detection model (RT-DETR r18vd)', size_mb: 81, present: false, integrity_hint: 'rev:ac77a11ff017', source: 'huggingface.co/PekingU/rtdetr_r18vd' },
  { id: 'facenet_pytorch', label: 'Face recognition model (facenet-pytorch)', size_mb: 108, present: false, integrity_hint: '', source: 'github.com/timesler/facenet-pytorch' },
]

function stubWs(models: Record<string, ModelDownloadState> | null, complete: boolean) {
  vi.mocked(setupWs.useSetupWS).mockReturnValue({ models, complete })
}

function renderModelDownload(onReady = vi.fn()) {
  // A fresh QueryClient per render; retry off. initialModels seeds the list so no network is hit.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <ModelDownload onReady={onReady} startedAtMs={Date.now()} initialModels={MODELS} />
    </QueryClientProvider>,
  )
  return { onReady }
}

describe('ModelDownload', () => {
  beforeEach(() => {
    // Benign default so the 3 s backstop poll (refetchInterval) is harmless in the synchronous
    // tests; tests that care about the status response override this with their own stub.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => ({ ready: false, models: MODELS }) }) as unknown as Response),
    )
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it('renders the model checklist with sizes and sources inside one splash', () => {
    stubWs({ clip: { status: 'downloading', error: null } }, false)
    renderModelDownload()
    expect(screen.getByText('Downloading AI models')).toBeInTheDocument()
    // the active (downloading) CLIP model appears BOTH as the detail line and its checklist row
    expect(screen.getAllByText(/Semantic search model \(CLIP ViT-L-14\)/).length).toBeGreaterThan(1)
    expect(screen.getByText('~850 MB')).toBeInTheDocument()
    expect(screen.getByText(/from huggingface\.co\/timm/)).toBeInTheDocument()
  })

  it('weights the unified bar by download size and stays in the 60–100 band', () => {
    // rtdetr + facenet done (81+108 of 1039 MB) → ~67% on the 60→100 model span.
    stubWs(
      { clip: { status: 'downloading', error: null }, rtdetr: { status: 'done', error: null }, facenet_pytorch: { status: 'done', error: null } },
      false,
    )
    renderModelDownload()
    const width = parseInt((screen.getByTestId('provision-bar') as HTMLElement).style.width, 10)
    expect(width).toBeGreaterThanOrEqual(60)
    expect(width).toBeLessThan(100)
  })

  it('reveals the app (onReady) once every model is done with no errors', async () => {
    stubWs(
      { clip: { status: 'done', error: null }, rtdetr: { status: 'done', error: null }, facenet_pytorch: { status: 'done', error: null } },
      true,
    )
    const { onReady } = renderModelDownload()
    await waitFor(() => expect(onReady).toHaveBeenCalled())
  })

  it('does NOT auto-reveal on a failed model; offers Retry + Continue anyway', async () => {
    stubWs(
      { clip: { status: 'error', error: 'Couldn’t reach huggingface.co' }, rtdetr: { status: 'done', error: null }, facenet_pytorch: { status: 'done', error: null } },
      true,
    )
    const onReady = vi.fn()
    renderModelDownload(onReady)
    expect(onReady).not.toHaveBeenCalled()
    expect(screen.getByText(/Couldn’t reach huggingface\.co/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /retry failed downloads/i })).toBeInTheDocument()
    // Continue anyway is the escape hatch — it reveals the app despite the failure.
    await userEvent.click(screen.getByRole('button', { name: /continue anyway/i }))
    await waitFor(() => expect(onReady).toHaveBeenCalled())
  })

  it('Retry failed downloads restarts the backend download (start_if_needed) then reloads', async () => {
    // Codex P2 (188): the button must hit the backend retry endpoint, not merely reload the webview
    // (a reload only re-subscribes to the manager's held error state). Assert retrySetup() runs,
    // then the reload.
    vi.mocked(retrySetup).mockResolvedValue(undefined)
    const reload = vi.fn()
    const originalLocation = window.location
    Object.defineProperty(window, 'location', { configurable: true, value: { ...originalLocation, reload } })
    try {
      stubWs(
        { clip: { status: 'error', error: 'boom' }, rtdetr: { status: 'done', error: null }, facenet_pytorch: { status: 'done', error: null } },
        true,
      )
      renderModelDownload()
      await userEvent.click(screen.getByRole('button', { name: /retry failed downloads/i }))
      await waitFor(() => expect(retrySetup).toHaveBeenCalledTimes(1))
      await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    } finally {
      Object.defineProperty(window, 'location', { configurable: true, value: originalLocation })
    }
  })

  it('does NOT auto-reveal in the fallback path (empty metadata) when the WS completes with errors', async () => {
    // Codex P2: StartupGate's /api/setup/status check failed → initialModels=[] and the component's
    // own refetch also fails, so the checklist `rows` are empty. A `complete` carrying a model error
    // must still be caught from the raw wsModels states and surface retry/continue — never silently
    // reveal the app past the first-run failure UI.
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('status unreachable')
    }))
    stubWs({ clip: { status: 'error', error: 'Network unreachable' } }, true)
    const onReady = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ModelDownload onReady={onReady} startedAtMs={Date.now()} initialModels={[]} />
      </QueryClientProvider>,
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /retry failed downloads/i })).toBeInTheDocument(),
    )
    expect(onReady).not.toHaveBeenCalled() // app stays gated behind the failure UI
  })

  it('surfaces a Try-again path when setup status keeps failing and the WS never completes (no infinite spinner)', async () => {
    // Codex P2 (69): fallback path where /api/setup/status persistently errors (empty metadata) AND
    // /ws/setup never sends `complete`. Once the query's retries exhaust, the splash must offer a
    // way out (Try again / Continue) instead of hanging on "Downloading AI models" forever.
    vi.useFakeTimers()
    try {
      vi.stubGlobal('fetch', vi.fn(async () => {
        throw new Error('status unreachable')
      }))
      stubWs(null, false) // WS connected but never completes and reports no model states
      const onReady = vi.fn()
      const qc = new QueryClient({ defaultOptions: { queries: { retry: 10, retryDelay: 1000 } } })
      render(
        <QueryClientProvider client={qc}>
          <ModelDownload onReady={onReady} startedAtMs={Date.now()} initialModels={[]} />
        </QueryClientProvider>,
      )
      // step past the ~10 × 1 s query retries so the query reaches its error state
      await act(async () => {
        await vi.advanceTimersByTimeAsync(12_000)
      })
      expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
      expect(onReady).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('reveals the app when a status refetch returns ready:true even if the WS never completes', async () => {
    // Codex P2 (99): fallback path where StartupGate's first status check failed (initialModels=[]),
    // but the models are actually on disk — a successful refetch (ready:true) must reveal the app
    // even though /ws/setup closed and never sent `complete`.
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ ready: true, models: MODELS.map((m) => ({ ...m, present: true })) }),
    }) as unknown as Response))
    stubWs(null, false) // WS never completes
    const onReady = vi.fn()
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <ModelDownload onReady={onReady} startedAtMs={Date.now()} initialModels={[]} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(onReady).toHaveBeenCalled())
  })

  it('polls setup status as a backstop and reveals when it flips to ready even if the WS never completes (normal path)', async () => {
    // Codex P2 (76): NORMAL path — StartupGate seeded initialModels (ready:false), so the query is
    // "fresh" (staleTime:Infinity) and never refetches on its own. If /ws/setup disconnects and
    // never sends `complete` while the downloader finishes on disk, only a status POLL can observe
    // ready:true. Assert the backstop poll reveals the app.
    vi.useFakeTimers()
    try {
      vi.stubGlobal('fetch', vi.fn(async () => ({
        ok: true,
        json: async () => ({ ready: true, models: MODELS.map((m) => ({ ...m, present: true })) }),
      }) as unknown as Response))
      stubWs(null, false) // WS connected but never completes
      const onReady = vi.fn()
      const qc = new QueryClient()
      render(
        <QueryClientProvider client={qc}>
          <ModelDownload onReady={onReady} startedAtMs={Date.now()} initialModels={MODELS} />
        </QueryClientProvider>,
      )
      expect(onReady).not.toHaveBeenCalled() // seeded ready:false → gated at mount
      await act(async () => {
        await vi.advanceTimersByTimeAsync(4000) // past the 3 s backstop poll → status → ready:true
      })
      expect(onReady).toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})
