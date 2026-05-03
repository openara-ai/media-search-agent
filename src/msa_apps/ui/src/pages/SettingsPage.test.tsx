import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SettingsPage } from './SettingsPage'

// ── API mock ───────────────────────────────────────────────────────────────

const modelConfigData = {
  readonly: { device: 'cuda', model_name: 'ViT-L-14', pretrained: 'openai' },
  editable: {
    batch_size: 32,
    enable_object_detection: 'auto' as const,
    object_model: 'PekingU/rtdetr_r18vd',
    object_confidence_threshold: 0.35,
    enable_face_recognition: true,
    face_model: 'buffalo_l',
    face_confidence_threshold: 0.7,
    face_min_size: 20,
    face_store_metadata: true,
  },
  defaults: {
    batch_size: 32,
    enable_object_detection: 'auto' as const,
    object_model: 'PekingU/rtdetr_r18vd',
    object_confidence_threshold: 0.35,
    enable_face_recognition: true,
    face_model: 'buffalo_l',
    face_confidence_threshold: 0.7,
    face_min_size: 20,
    face_store_metadata: true,
  },
}
const diagData = {
  msa_root: '/home/user/msa',
  config_file: '/home/user/msa/config.yaml',
  sqlite_path: '/home/user/msa/index/media.sqlite',
  log_dir: '/home/user/msa/logs',
  logs: {
    app:     '/home/user/msa/logs/msa.log',
    uvicorn: '/home/user/msa/logs/uvicorn.log',
    qdrant:  '/home/user/msa/logs/qdrant.log',
    launch:  '/home/user/msa/logs/launch-2026-03-24_170000.log',
  },
  qdrant_url: 'http://localhost:6333',
  api_url: 'http://localhost:8000',
}

function mockFetch(): void {
  vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
    if (url === '/diagnostics')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(diagData) })
    if (url === '/config/model') {
      if (opts?.method === 'PATCH')
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ updated: ['batch_size'] }) })
      return Promise.resolve({ ok: true, json: () => Promise.resolve(modelConfigData) })
    }
    // DELETE / POST
    return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })
  }))
}

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderPage() {
  return render(
    <QueryClientProvider client={makeClient()}>
      <SettingsPage />
    </QueryClientProvider>
  )
}

beforeEach(() => mockFetch())
afterEach(() => vi.unstubAllGlobals())

// ── Tests ──────────────────────────────────────────────────────────────────

describe('SettingsPage', () => {
  describe('diagnostics', () => {
    it('renders diagnostics section heading', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Diagnostics')).toBeInTheDocument())
    })

    it('log entries have an external link (open in browser)', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Diagnostics'))
      // Log entries render an <a> with the /logs/{key} href
      const links = screen.getAllByTitle('Open log in browser')
      expect(links.length).toBeGreaterThanOrEqual(3) // app, uvicorn, qdrant, launch
      links.forEach(link => {
        expect(link).toHaveAttribute('href', expect.stringMatching(/^\/logs\//))
      })
    })

    it('Config and SQLite entries have no external link', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Diagnostics'))
      const links = screen.getAllByTitle('Open log in browser')
      const hrefs = links.map(l => l.getAttribute('href') ?? '')
      // /logs/app etc. — none should be /config or /sqlite
      expect(hrefs.every(h => h.startsWith('/logs/'))).toBe(true)
    })
  })

  describe('model configuration', () => {
    it('renders Model Configuration heading', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Model Configuration')).toBeInTheDocument())
    })

    it('shows read-only CLIP model fields', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Model Configuration'))
      expect(screen.getByText('ViT-L-14')).toBeInTheDocument()
      expect(screen.getByText('cuda')).toBeInTheDocument()
      expect(screen.getByText('openai')).toBeInTheDocument()
    })

    it('shows batch size input with current value', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Model Configuration'))
      const batchInput = screen.getByDisplayValue('32')
      expect(batchInput).toBeInTheDocument()
      expect(batchInput).toBeDisabled()
    })

    it('reset button absent when value equals default', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Model Configuration'))
      // All values are at defaults in modelConfigData — no reset buttons visible
      expect(screen.queryByTitle('Reset to default')).toBeNull()
    })

    it('reset button appears when batch_size differs from default', async () => {
      const modified = {
        ...modelConfigData,
        editable: { ...modelConfigData.editable, batch_size: 16 },
      }
      vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/config/model') {
          if (opts?.method === 'PATCH')
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ updated: ['batch_size'] }) })
          return Promise.resolve({ ok: true, json: () => Promise.resolve(modified) })
        }
        if (url === '/diagnostics')
          return Promise.resolve({ ok: true, json: () => Promise.resolve(diagData) })
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })
      }))
      renderPage()
      await waitFor(() => screen.getByRole('button', { name: 'Edit' }))
      await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
      await waitFor(() => screen.getByTitle('Reset to default'))
      expect(screen.getByTitle('Reset to default')).toBeInTheDocument()
    })

    it('requires edit mode before model controls can be changed', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Model Configuration'))
      const toggle = screen.getAllByRole('switch')[0]
      expect(toggle).toBeDisabled()
      await userEvent.click(toggle)
      expect(vi.mocked(fetch)).not.toHaveBeenCalledWith(
        '/config/model',
        expect.objectContaining({ method: 'PATCH' }),
      )

      await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
      expect(toggle).not.toBeDisabled()
      await userEvent.click(toggle)
      expect(vi.mocked(fetch)).toHaveBeenCalledWith(
        '/config/model',
        expect.objectContaining({ method: 'PATCH' }),
      )
    })
  })
})
