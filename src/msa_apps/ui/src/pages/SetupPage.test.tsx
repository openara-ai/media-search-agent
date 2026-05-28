import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
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

// Labels mirror the backend's MODEL_META in setup_models.py (purpose-first,
// technical name in parens). Kept in sync intentionally so failures here
// surface label-format regressions in the user-facing string.
const INITIAL_MODELS: ModelInfo[] = [
  { id: 'clip',            label: 'Semantic search model (CLIP ViT-L-14)',    size_mb: 850, present: false, integrity_hint: 'sha256:9ce2e8a8ebff', source: 'huggingface.co/timm/vit_large_patch14_clip_224.openai' },
  { id: 'rtdetr',          label: 'Object detection model (RT-DETR r18vd)',   size_mb: 81,  present: false, integrity_hint: 'rev:ac77a11ff017',  source: 'huggingface.co/PekingU/rtdetr_r18vd' },
  { id: 'facenet_pytorch', label: 'Face detection model (facenet-pytorch)',   size_mb: 108, present: false, integrity_hint: 'torch-hub',         source: 'github.com/timesler/facenet-pytorch' },
]

const ALL_DONE = {
  clip:            { status: 'done'  as const, error: null },
  rtdetr:          { status: 'done'  as const, error: null },
  facenet_pytorch: { status: 'done'  as const, error: null },
}

const ONE_ERROR = {
  clip:            { status: 'error' as const, error: 'Download failed' },
  rtdetr:          { status: 'done'  as const, error: null },
  facenet_pytorch: { status: 'done'  as const, error: null },
}

const IN_PROGRESS_WITH_ERROR = {
  clip:            { status: 'error'       as const, error: 'Download failed' },
  rtdetr:          { status: 'downloading' as const, error: null },
  facenet_pytorch: { status: 'pending'     as const, error: null },
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

  describe('Continue anyway button', () => {
    // Escape hatch: if one model host is unreachable (e.g. GitHub release assets
    // blocked by a corporate proxy), users must be able to reach the main app
    // rather than be trapped on the setup screen forever.
    it('is visible when complete and errors are present', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      expect(screen.getByRole('button', { name: /continue anyway/i })).toBeTruthy()
    })

    it('is NOT visible when not yet complete even if a model has errored', () => {
      mockUseSetupWS.mockReturnValue({ complete: false, models: IN_PROGRESS_WITH_ERROR })
      renderSetup()
      expect(screen.queryByRole('button', { name: /continue anyway/i })).toBeNull()
    })

    it('is NOT visible when complete with no errors', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ALL_DONE })
      renderSetup()
      expect(screen.queryByRole('button', { name: /continue anyway/i })).toBeNull()
    })

    it('calls onComplete when clicked', () => {
      const onComplete = vi.fn()
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup(onComplete)
      // The useEffect path does NOT auto-fire onComplete when errors are present,
      // so the only way it gets called is via this button.
      expect(onComplete).not.toHaveBeenCalled()
      fireEvent.click(screen.getByRole('button', { name: /continue anyway/i }))
      expect(onComplete).toHaveBeenCalledOnce()
    })

    // PR #132 review feedback (Copilot): the previous "retry from Settings"
    // helper text was misinformation — Settings has no setup-retry control.
    // The interim wording must be truthful while #131's full persistence
    // fix is open. Reloading the page genuinely does re-show the SetupPage
    // (data.ready is still false), so it's an honest, actionable instruction.
    it('helper text below Continue anyway is honest about how to retry', () => {
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      expect(screen.queryByText(/from Settings/i)).toBeNull()
      expect(screen.getByText(/reload this page/i)).toBeTruthy()
    })
  })

  describe('integrity hint rendering', () => {
    // PR #132 review feedback (Copilot): `_integrity_hint()` returned an
    // empty string for models without sha256/revision (facenet-pytorch
    // historically), and SetupPage rendered "{integrity_hint}…" for every
    // done model — producing a lone "…" next to "Verified" for facenet.
    // Fixed two ways: (a) backend now returns 'torch-hub' for github-sourced
    // models, (b) UI renders the span only when the hint is non-empty.
    // This test guards both fixes.

    function modelInfoWithHint(hint: string): ModelInfo {
      // A real fixture sourced from the live INITIAL_MODELS array would do,
      // but a minimal record keeps the test focused on the render rule.
      return {
        id: 'clip',
        label: 'Semantic search model (CLIP ViT-L-14)',
        size_mb: 850,
        present: false,
        integrity_hint: hint,
        source: 'huggingface.co/timm/vit_large_patch14_clip_224.openai',
      }
    }

    it('renders the integrity-hint span only when hint is non-empty', () => {
      // Model with empty hint + status done should NOT produce the "…" suffix.
      mockUseSetupWS.mockReturnValue({
        complete: true,
        models: {
          clip: { status: 'done', error: null },
        },
      })
      const { rerender } = render(
        <SetupPage
          initialModels={[modelInfoWithHint('')]}
          onComplete={vi.fn()}
        />,
      )
      // The ellipsis suffix in the span is "…" (Unicode HORIZONTAL ELLIPSIS).
      // No mono-font span containing it should be in the document.
      expect(screen.queryByText(/…/)).toBeNull()

      // Same model with a populated hint should produce the span.
      rerender(
        <SetupPage
          initialModels={[modelInfoWithHint('torch-hub')]}
          onComplete={vi.fn()}
        />,
      )
      expect(screen.getByText(/torch-hub…/)).toBeTruthy()
    })
  })

  describe('model display', () => {
    it('renders all model labels from initialModels', () => {
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup()
      expect(screen.getByText('Semantic search model (CLIP ViT-L-14)')).toBeTruthy()
      expect(screen.getByText('Object detection model (RT-DETR r18vd)')).toBeTruthy()
      expect(screen.getByText('Face detection model (facenet-pytorch)')).toBeTruthy()
    })

    it('leads with the user-facing purpose, keeping the technical name in parens', () => {
      // Locks the label format. Bare technical names ("CLIP ViT-L-14",
      // "RT-DETR r18vd") were opaque to users seeing the first-launch
      // download page; the purpose-first form makes it obvious what each
      // download is for.
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup()
      for (const label of INITIAL_MODELS.map((m) => m.label)) {
        expect(label).toMatch(/^[A-Z][^()]+\([^)]+\)$/)
      }
    })

    it('shows sha256 prefix when a model transitions to done', () => {
      mockUseSetupWS.mockReturnValue({
        complete: false,
        models: {
          clip:        { status: 'done',    error: null },
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

  describe('source display', () => {
    // Trust signal: users should see where the bytes are coming from
    // (huggingface.co, github.com) before/during download.
    it('shows the source host + path for pending models', () => {
      mockUseSetupWS.mockReturnValue({ complete: false, models: null })
      renderSetup()
      expect(screen.getByText(/from huggingface\.co\/PekingU\/rtdetr_r18vd/)).toBeTruthy()
      expect(screen.getByText(/from github\.com\/timesler\/facenet-pytorch/)).toBeTruthy()
    })

    it('hides the source line once a model is done', () => {
      // Once verified, the integrity hint replaces source as the relevant
      // artifact — showing both would clutter the row.
      mockUseSetupWS.mockReturnValue({ complete: true, models: ALL_DONE })
      renderSetup()
      expect(screen.queryByText(/from huggingface\.co/)).toBeNull()
      expect(screen.queryByText(/from github\.com/)).toBeNull()
    })

    it('hides the source line when a model has errored', () => {
      // Error message takes priority over source.
      mockUseSetupWS.mockReturnValue({ complete: true, models: ONE_ERROR })
      renderSetup()
      // clip errored — its source should be hidden in favor of the error msg
      expect(screen.queryByText(/from huggingface\.co\/timm/)).toBeNull()
    })
  })
})
