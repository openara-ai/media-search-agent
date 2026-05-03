import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SetupPage } from './SetupPage'
import type { ModelInfo } from '../api/setup'

// ── Mock useSetupWS so each test controls the perceived WS state ───────────
//
// vi.mock is hoisted before imports by Vitest's transform plugin, so the
// mock is in place by the time SetupPage imports the hook.

vi.mock('../hooks/useSetupWS', () => ({
  useSetupWS: vi.fn(),
}))

import { useSetupWS } from '../hooks/useSetupWS'
const mockUseSetupWS = vi.mocked(useSetupWS)

// ── Fixtures ───────────────────────────────────────────────────────────────

const INITIAL_MODELS: ModelInfo[] = [
  { id: 'clip',        label: 'CLIP ViT-L-14',        size_mb: 850, present: false, integrity_hint: 'sha256:9ce2e8a8ebff' },
  { id: 'rtdetr',     label: 'RT-DETR r18vd',          size_mb: 81,  present: false, integrity_hint: 'rev:ac77a11ff017' },
  { id: 'insightface', label: 'InsightFace buffalo_l',  size_mb: 500, present: false, integrity_hint: 'sha256:5838f7fe0536' },
]

const ALL_DONE = {
  clip:        { status: 'done'  as const, error: null },
  rtdetr:      { status: 'done'  as const, error: null },
  insightface: { status: 'done'  as const, error: null },
}

const ONE_ERROR = {
  clip:        { status: 'error' as const, error: 'Download failed' },
  rtdetr:      { status: 'done'  as const, error: null },
  insightface: { status: 'done'  as const, error: null },
}

const IN_PROGRESS_WITH_ERROR = {
  clip:        { status: 'error'       as const, error: 'Download failed' },
  rtdetr:      { status: 'downloading' as const, error: null },
  insightface: { status: 'pending'     as const, error: null },
}

function renderSetup(onComplete = vi.fn()) {
  return render(<SetupPage initialModels={INITIAL_MODELS} onComplete={onComplete} />)
}

// ── Tests ──────────────────────────────────────────────────────────────────

describe('SetupPage', () => {
  describe('onComplete callback', () => {
    it('is called when WS reports complete with no errors', () => {
      const onComplete = vi.fn()
      mockUseSetupWS.mockReturnValue({ complete: true, models: ALL_DONE })
      renderSetup(onComplete)
      expect(onComplete).toHaveBeenCalledOnce()
    })

    it('is NOT called when WS reports complete but errors are present', () => {
      // This was the bug: complete=true always triggered onComplete(), so
      // users landed in a broken main app instead of seeing the retry button.
      const onComplete = vi.fn()
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup(onComplete)
      expect(onComplete).not.toHaveBeenCalled()
    })

    it('is NOT called while WS has not yet responded', () => {
      const onComplete = vi.fn()
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup(onComplete)
      expect(onComplete).not.toHaveBeenCalled()
    })

    it('is NOT called while downloads are still in progress', () => {
      const onComplete = vi.fn()
      mockUseSetupWS.mockReturnValue({ complete: false, models: IN_PROGRESS_WITH_ERROR })
      renderSetup(onComplete)
      expect(onComplete).not.toHaveBeenCalled()
    })
  })

  describe('Retry button', () => {
    it('is visible when complete and errors are present', () => {
      // The retry button is the only way out of a failed setup — it must be
      // reachable, which requires the setup screen to still be showing.
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      expect(screen.getByRole('button', { name: /retry failed downloads/i })).toBeTruthy()
    })

    it('is NOT visible when not yet complete even if a model has errored', () => {
      // Still in-flight: don't show the button until everything has settled.
      mockUseSetupWS.mockReturnValue({ complete: false, models: IN_PROGRESS_WITH_ERROR })
      renderSetup()
      expect(screen.queryByRole('button', { name: /retry failed downloads/i })).toBeNull()
    })

    it('is NOT visible when complete with no errors', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ALL_DONE })
      renderSetup()
      expect(screen.queryByRole('button', { name: /retry failed downloads/i })).toBeNull()
    })

    it('is NOT visible while WS has not yet connected', () => {
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup()
      expect(screen.queryByRole('button', { name: /retry failed downloads/i })).toBeNull()
    })
  })

  describe('model display', () => {
    it('renders all model labels from initialModels', () => {
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup()
      expect(screen.getByText('CLIP ViT-L-14')).toBeTruthy()
      expect(screen.getByText('RT-DETR r18vd')).toBeTruthy()
      expect(screen.getByText('InsightFace buffalo_l')).toBeTruthy()
    })

    it('shows sha256 prefix when a model transitions to done', () => {
      mockUseSetupWS.mockReturnValue({
        complete: false,
        models: {
          clip:        { status: 'done',    error: null },
          yolo:        { status: 'pending', error: null },
          insightface: { status: 'pending', error: null },
        },
      })
      renderSetup()
      expect(screen.getByText(/sha256:9ce2e8a8ebff/)).toBeTruthy() // integrity_hint for CLIP
    })

    it('shows error message text when a model fails', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      expect(screen.getByText('Download failed')).toBeTruthy()
    })

    it('shows "Some downloads failed" summary when any model has an error', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      expect(screen.getByText(/some downloads failed/i)).toBeTruthy()
    })

    it('does not show error summary when all models succeed', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ALL_DONE })
      renderSetup()
      expect(screen.queryByText(/some downloads failed/i)).toBeNull()
    })
  })
})
