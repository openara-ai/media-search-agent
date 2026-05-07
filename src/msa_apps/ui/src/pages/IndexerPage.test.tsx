import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { IndexerPage } from './IndexerPage'

// ── WebSocket mock ─────────────────────────────────────────────────────────

type WSMessage = { type: string; status: object; log: string[] }

class MockWebSocket {
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  static lastInstance: MockWebSocket | null = null
  static pendingMessage: WSMessage | null = null

  constructor(_url: string) {
    MockWebSocket.lastInstance = this
    // Deliver any pre-configured message after mount
    if (MockWebSocket.pendingMessage) {
      const msg = MockWebSocket.pendingMessage
      setTimeout(() => this.onmessage?.({ data: JSON.stringify(msg) }), 0)
    }
  }
  close() {}
}

const idleUpdate: WSMessage = {
  type: 'update',
  status: { status: 'idle', run_id: null, started_at: null, finished_at: null, elapsed_seconds: null, return_code: null },
  log: [],
}

const runningUpdate: WSMessage = {
  type: 'update',
  status: { status: 'running', run_id: 'abc123', started_at: null, finished_at: null, elapsed_seconds: 42, return_code: null },
  log: ['2026-03-24 12:00:00 | INFO | Indexing IMG_001.heic'],
}

// ── Fetch mock ─────────────────────────────────────────────────────────────

function mockFetch(overrides: Record<string, object> = {}) {
  vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
    if (url === '/config/sources' && opts?.method === 'POST')
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ status: 'ok' }) })
    if (url === '/config/sources')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(overrides[url] ?? { sources: [{ name: 'photos', path: '/mnt/d/Photos', display_path: '/mnt/d/Photos', enabled: true, read_only: true, description: 'Main library' }] }) })
    if (url === '/platform')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(overrides[url] ?? { platform: 'linux' }) })
    if (url === '/browse/pick')
      return Promise.resolve({ ok: true, json: () => Promise.resolve(overrides[url] ?? { path: '/Users/test/Pictures', cancelled: false }) })
    if (typeof url === 'string' && url.startsWith('/browse?path='))
      return Promise.resolve({
        ok: true,
        json: () => Promise.resolve({
          current: { display_path: '/', wsl_path: '' },
          parent: null,
          entries: [],
        }),
      })
    const body = overrides[url] ?? { images: 0, videos: 0, total_video_duration: 0, faces: 0, people: 0, last_indexed_at: null }
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body), text: () => Promise.resolve('') })
  }))
}

// ── Helpers ────────────────────────────────────────────────────────────────

function makeClient() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

function renderPage() {
  return render(
    <QueryClientProvider client={makeClient()}>
      <IndexerPage />
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', MockWebSocket)
  MockWebSocket.lastInstance = null
  MockWebSocket.pendingMessage = idleUpdate
  mockFetch()
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── Tests ──────────────────────────────────────────────────────────────────

describe('IndexerPage', () => {
  describe('stats bar', () => {
    it('renders four stat labels', () => {
      renderPage()
      expect(screen.getByText('Photos')).toBeTruthy()
      expect(screen.getByText('Videos')).toBeTruthy()
      expect(screen.getByText('Faces')).toBeTruthy()
      expect(screen.getByText('People')).toBeTruthy()
    })

    it('shows dashes before stats load', () => {
      renderPage()
      // Four stat boxes with — until fetch resolves
      expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(4)
    })

    it('shows loaded stats from API', async () => {
      mockFetch({ '/indexer/stats': { images: 1234, videos: 56, total_video_duration: 0, faces: 789, people: 12, last_indexed_at: null } })
      renderPage()
      await waitFor(() => expect(screen.getByText('1,234')).toBeTruthy())
      expect(screen.getByText('56')).toBeTruthy()
    })

    it('shows Not yet indexed when last_indexed_at is null', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Not yet indexed')).toBeTruthy())
    })

    it('shows last indexed timestamp when available', async () => {
      mockFetch({ '/indexer/stats': { images: 1, videos: 0, total_video_duration: 0, faces: 0, people: 0, last_indexed_at: '2026-03-24 12:00:00' } })
      renderPage()
      await waitFor(() => expect(screen.getByText(/Last indexed:/)).toBeTruthy())
    })

    it('shows total video duration when non-zero', async () => {
      mockFetch({ '/indexer/stats': { images: 10, videos: 5, total_video_duration: 217, faces: 0, people: 0, last_indexed_at: null } })
      renderPage()
      await waitFor(() => expect(screen.getByText('3m 37s')).toBeTruthy())
    })

    it('does not show video duration when total_video_duration is 0', async () => {
      mockFetch({ '/indexer/stats': { images: 10, videos: 5, total_video_duration: 0, faces: 0, people: 0, last_indexed_at: null } })
      renderPage()
      await waitFor(() => expect(screen.getByText('5')).toBeTruthy())
      expect(screen.queryByText(/^\d+m \d+s$/)).toBeNull()
    })

    it('does not show video duration for other stat tiles', async () => {
      mockFetch({ '/indexer/stats': { images: 10, videos: 5, total_video_duration: 217, faces: 20, people: 3, last_indexed_at: null } })
      renderPage()
      await waitFor(() => expect(screen.getByText('3m 37s')).toBeTruthy())
      // Duration only appears once — in the Videos tile, not duplicated in Photos/Faces/People
      expect(screen.getAllByText('3m 37s').length).toBe(1)
    })
  })

  describe('status and controls', () => {
    it('shows Ready and Run Indexer when idle', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Ready')).toBeTruthy())
      expect(screen.getByRole('button', { name: /run indexer/i })).toBeTruthy()
    })

    it('shows Running and Stop when WS reports running', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() => expect(screen.getByText('Running…')).toBeTruthy())
      expect(screen.getByRole('button', { name: /stop/i })).toBeTruthy()
    })

    it('shows elapsed time when running', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() => expect(screen.getByText(/42s elapsed/i)).toBeTruthy())
    })

    it('shows last log line as current activity when running', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/Indexing IMG_001\.heic/)).toBeTruthy()
      )
    })

    it('calls POST /indexer/start when Run is clicked', async () => {
      renderPage()
      await waitFor(() => screen.getByRole('button', { name: /run indexer/i }))
      const fetchMock = vi.mocked(fetch)
      await userEvent.click(screen.getByRole('button', { name: /run indexer/i }))
      expect(fetchMock).toHaveBeenCalledWith('/indexer/start', expect.objectContaining({ method: 'POST' }))
    })

    it('disables Run Indexer until a source is configured', async () => {
      mockFetch({ '/config/sources': { sources: [] } })
      renderPage()
      await waitFor(() => expect(screen.getByText(/setup required/i)).toBeTruthy())
      expect(screen.getByRole('button', { name: /run indexer/i })).toBeDisabled()
    })
  })

  describe('log viewer', () => {
    it('log section is absent when log is empty', async () => {
      renderPage()
      await waitFor(() => screen.getByText('Ready'))
      expect(screen.queryByText(/log \(/i)).toBeNull()
    })

    it('log toggle appears when log has lines', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() => screen.getByText(/log \(1 lines\)/i))
    })
  })

  describe('media sources', () => {
    it('shows the configured source in the sources card', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('photos')).toBeTruthy())
      expect(screen.getByText('/mnt/d/Photos')).toBeTruthy()
      expect(screen.getByText('Read-only')).toBeTruthy()
    })

    it('expands the source form by default when no sources exist', async () => {
      mockFetch({ '/config/sources': { sources: [] } })
      renderPage()
      await waitFor(() => expect(screen.getByText(/add your first source/i)).toBeTruthy())
      await waitFor(() => expect(screen.getByPlaceholderText(/name/i)).toBeTruthy())
    })

    it('keeps the add source form collapsed by default when sources already exist', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('photos')).toBeTruthy())
      expect(screen.queryByPlaceholderText(/name/i)).toBeNull()
    })

    it('uses the native picker on macOS', async () => {
      mockFetch({ '/platform': { platform: 'macos' }, '/browse/pick': { path: '/Users/test/Pictures', cancelled: false } })
      renderPage()
      await waitFor(() => expect(screen.getByText('photos')).toBeTruthy())

      await userEvent.click(screen.getByText(/^add source$/i))
      await waitFor(() => expect(screen.getByPlaceholderText(/e\.g\. \/home\/user\/photos/i)).toBeTruthy())
      await userEvent.click(screen.getByTitle('Browse folders'))

      await waitFor(() => {
        expect(vi.mocked(fetch)).toHaveBeenCalledWith('/browse/pick')
      })
      expect(screen.getByDisplayValue('/Users/test/Pictures')).toBeTruthy()
      expect(screen.queryByText('Select Folder')).toBeNull()
    })
  })
})
