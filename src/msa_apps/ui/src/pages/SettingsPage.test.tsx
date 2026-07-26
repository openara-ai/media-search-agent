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
    face_recognizer_backend: 'facenet_pytorch',
    face_model: 'vggface2',
    face_confidence_threshold: 0.95,
    face_min_size: 60,
    face_store_metadata: true,
  },
  defaults: {
    batch_size: 32,
    enable_object_detection: 'auto' as const,
    object_model: 'PekingU/rtdetr_r18vd',
    object_confidence_threshold: 0.35,
    enable_face_recognition: true,
    face_recognizer_backend: 'facenet_pytorch',
    face_model: 'vggface2',
    face_confidence_threshold: 0.95,
    face_min_size: 60,
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
    desktop: '/home/user/msa/logs/msa-desktop.log',
    launch:  '/home/user/msa/logs/launch-2026-03-24_170000.log',
  },
  qdrant_url: 'http://localhost:6333',
  api_url: 'http://localhost:52341',
  app_version: '0.3.2',
  cli: {
    msa_path: '/home/user/msa/.venv/bin/msa',
    launcher_path: '/home/user/.local/bin/msa',
    launcher_installed: false,
    on_path: false,
  },
}

function mockFetch(platform = 'macos', diag: Record<string, unknown> = diagData): void {
  vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
    if (url === '/diagnostics')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(diag) })
    if (url === '/platform')
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ platform }) })
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

    it('shows the backend url (port) row', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Diagnostics'))
      expect(screen.getByText('Backend')).toBeInTheDocument()
      expect(screen.getByText('http://localhost:52341')).toBeInTheDocument()
    })

    it('shows the desktop log location without an external link', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Diagnostics'))
      expect(screen.getByText('Desktop log')).toBeInTheDocument()
      expect(screen.getByText('/home/user/msa/logs/msa-desktop.log')).toBeInTheDocument()
      // msa-desktop.log has no GET /logs/{name} viewer, so no open-in-browser link
      const links = screen.getAllByTitle('Open log in browser')
      expect(links.some(l => (l.getAttribute('href') ?? '').includes('desktop'))).toBe(false)
    })
  })

  describe('about', () => {
    it('renders About heading and the backend-reported version', async () => {
      renderPage()
      // About/Version render immediately; the version resolves once diagnostics loads.
      expect(screen.getByText('About')).toBeInTheDocument()
      expect(screen.getByText('Version')).toBeInTheDocument()
      await waitFor(() => expect(screen.getByText('0.3.2')).toBeInTheDocument())
    })

    it('uses the backend version, NOT the shell-injected window.__APP_VERSION__', async () => {
      // Regression guard: window.__APP_VERSION__ is env!("CARGO_PKG_VERSION") from the never-
      // stamped src-tauri/Cargo.toml (stale "0.1.0"), so the backend app_version must win.
      // jsdom's window is the global object, so stubbing the global injects window.__APP_VERSION__.
      // afterEach → vi.unstubAllGlobals() restores it (and the fetch mock) for the next test.
      vi.stubGlobal('__APP_VERSION__', '0.1.0')
      renderPage()
      await waitFor(() => expect(screen.getByText('0.3.2')).toBeInTheDocument())
      expect(screen.queryByText('0.1.0')).toBeNull()
    })
  })

  describe('command-line tool (msa CLI opt-in)', () => {
    it('macOS not-installed: shows a non-clobbering symlink opt-in targeting the venv msa', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Command-line tool')).toBeInTheDocument())
      // Non-clobbering: `ln -s` (no -f) guarded by an existence check, targeting our venv msa.
      expect(
        screen.getByText(/ln -s "\/home\/user\/msa\/\.venv\/bin\/msa" ~\/\.local\/bin\/msa/),
      ).toBeInTheDocument()
      expect(screen.getByText(/already exists — remove it first/)).toBeInTheDocument()
      // Must NOT force-overwrite an existing launcher.
      expect(screen.queryByText(/ln -sf/)).toBeNull()
    })

    it('Windows not-installed: shows the executable path, no symlink command', async () => {
      mockFetch('windows', {
        ...diagData,
        cli: { msa_path: 'C:\\Users\\u\\msa\\.venv\\Scripts\\msa.exe', launcher_path: null, launcher_installed: false, on_path: false },
      })
      renderPage()
      await waitFor(() => expect(screen.getByText('Command-line tool')).toBeInTheDocument())
      expect(screen.getByText(/Scripts\\msa\.exe/)).toBeInTheDocument()
      expect(screen.queryByText(/ln -s /)).toBeNull()
    })

    it('already installed: states the tool is available, no install command', async () => {
      mockFetch('macos', {
        ...diagData,
        cli: { msa_path: '/home/user/msa/.venv/bin/msa', launcher_path: '/home/user/.local/bin/msa', launcher_installed: true, on_path: false },
      })
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/is available in your terminal/)).toBeInTheDocument(),
      )
      expect(screen.queryByText(/ln -s /)).toBeNull()
    })

    it('no msa_path: section is omitted entirely', async () => {
      mockFetch('macos', {
        ...diagData,
        cli: { msa_path: null, launcher_path: null, launcher_installed: false, on_path: false },
      })
      renderPage()
      await waitFor(() => expect(screen.getByText('Diagnostics')).toBeInTheDocument())
      expect(screen.queryByText('Command-line tool')).toBeNull()
    })
  })

  describe('uninstall', () => {
    it('renders the Uninstall section with instructions collapsed', () => {
      renderPage()
      expect(screen.getByText('Uninstall')).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /Uninstall Media Search Agent/ })).toBeInTheDocument()
      expect(screen.queryByText(/kept by default/)).toBeNull()
    })

    it('macOS instructions show the uninstall-desktop.sh one-liner and default-keep wording', async () => {
      renderPage()
      await userEvent.click(screen.getByRole('button', { name: /Uninstall Media Search Agent/ }))
      await waitFor(() =>
        expect(screen.getByText(/uninstall-desktop\.sh/)).toBeInTheDocument()
      )
      // ADR-005 Tier-2 posture must be stated: user data is kept by default.
      expect(screen.getByText(/index, config, logs and model cache are kept by default/)).toBeInTheDocument()
    })

    it('Windows instructions point at Apps and the app-data checkbox (default keep)', async () => {
      mockFetch('windows')
      renderPage()
      await userEvent.click(screen.getByRole('button', { name: /Uninstall Media Search Agent/ }))
      await waitFor(() => expect(screen.getByText(/Installed apps/)).toBeInTheDocument())
      expect(screen.getByText(/Delete the application data/)).toBeInTheDocument()
      expect(screen.getByText(/kept by default/)).toBeInTheDocument()
    })

    it('Linux/WSL2 points at the shell-bundle uninstall path', async () => {
      mockFetch('linux')
      renderPage()
      await userEvent.click(screen.getByRole('button', { name: /Uninstall Media Search Agent/ }))
      await waitFor(() => expect(screen.getByText(/msa uninstall/)).toBeInTheDocument())
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

    it('shows all buffalo options when backend is insightface', async () => {
      const insightfaceConfig = {
        ...modelConfigData,
        editable: {
          ...modelConfigData.editable,
          face_recognizer_backend: 'insightface',
          face_model: 'buffalo_l',
        },
      }
      vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/config/model') {
          if (opts?.method === 'PATCH')
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ updated: [] }) })
          return Promise.resolve({ ok: true, json: () => Promise.resolve(insightfaceConfig) })
        }
        if (url === '/diagnostics')
          return Promise.resolve({ ok: true, json: () => Promise.resolve(diagData) })
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })
      }))
      renderPage()
      await waitFor(() => screen.getByText('Model Configuration'))
      expect(screen.getByRole('option', { name: 'buffalo_s' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'buffalo_l' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'antelopev2' })).toBeInTheDocument()
    })

    it('renders unknown face_model as fallback option and keeps dropdown enabled in edit mode', async () => {
      const staleConfig = {
        ...modelConfigData,
        editable: { ...modelConfigData.editable, face_model: 'unknown_xyz' },
      }
      vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
        if (url === '/config/model') {
          if (opts?.method === 'PATCH')
            return Promise.resolve({ ok: true, json: () => Promise.resolve({ updated: [] }) })
          return Promise.resolve({ ok: true, json: () => Promise.resolve(staleConfig) })
        }
        if (url === '/diagnostics')
          return Promise.resolve({ ok: true, json: () => Promise.resolve(diagData) })
        return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })
      }))
      renderPage()
      await waitFor(() => screen.getByRole('button', { name: 'Edit' }))
      expect(screen.getByRole('option', { name: 'vggface2' })).toBeInTheDocument()
      expect(screen.getByRole('option', { name: 'unknown_xyz' })).toBeInTheDocument()
      await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
      const faceModelSelect = screen.getByDisplayValue('unknown_xyz')
      expect(faceModelSelect).not.toBeDisabled()
    })
  })
})
