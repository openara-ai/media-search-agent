import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { App } from './App'
import { queryClient } from './lib/queryClient'

vi.mock('./hooks/useSetupWS', () => ({
  useSetupWS: vi.fn(() => ({ models: null, complete: false })),
}))

const INITIAL_MODELS = [
  { id: 'clip',        label: 'CLIP ViT-L-14',        size_mb: 850, present: false, integrity_hint: 'sha256:9ce2e8a8ebff' },
  { id: 'rtdetr',     label: 'RT-DETR r18vd',         size_mb: 81,  present: false, integrity_hint: 'rev:ac77a11ff017' },
  { id: 'insightface', label: 'InsightFace buffalo_l', size_mb: 500, present: false, integrity_hint: 'sha256:5838f7fe0536' },
]

describe('App launch flow', () => {
  beforeEach(() => {
    queryClient.clear()
    window.history.replaceState({}, '', '/browse')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    queryClient.clear()
  })

  it('shows the launch splash immediately while setup status is still loading', async () => {
    window.history.replaceState({}, '', '/browse?launch=1')
    vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})))

    render(<App />)

    expect(await screen.findByLabelText(/media search agent launch splash/i)).toBeInTheDocument()
    // App strips ?launch on mount via history.replaceState before the API query resolves.
    // In jsdom, window.location reflects that same history mutation.
    await waitFor(() => expect(window.location.search).toBe(''))
  })

  it('keeps setup behind the splash and reveals it after dismissal when first-launch downloads are needed', async () => {
    window.history.replaceState({}, '', '/browse?launch=1')
    vi.stubGlobal('fetch', vi.fn(async () => ({
      ok: true,
      json: async () => ({ ready: false, models: INITIAL_MODELS }),
    } as Response)))

    render(<App />)

    expect(await screen.findByLabelText(/media search agent launch splash/i)).toBeInTheDocument()
    expect(await screen.findByText('First Launch Setup')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /dismiss launch banner/i }))

    await waitFor(() => {
      expect(screen.queryByLabelText(/media search agent launch splash/i)).not.toBeInTheDocument()
    })
    expect(screen.getByText('First Launch Setup')).toBeInTheDocument()
  })
})
