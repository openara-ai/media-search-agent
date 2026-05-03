import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PeoplePage } from './PeoplePage'

// ── Mock data ─────────────────────────────────────────────────────────────────

const peopleResp = {
  people: [
    { person_id: 'p1', name: 'Alice', face_count: 3, thumbnail: '/face_thumbnails/f1.jpg' },
    { person_id: 'p2', name: 'Bob',   face_count: 2, thumbnail: '/face_thumbnails/f2.jpg' },
  ],
}

function ok(data: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(data) })
}

function mockFetch(): void {
  vi.stubGlobal('fetch', vi.fn((url: string, opts?: RequestInit) => {
    if (url === '/people') {
      if (opts?.method === 'PATCH') {
        const body = JSON.parse(opts.body as string)
        return ok({ person_id: 'p1', name: body.name, face_count: 3, thumbnail: '/face_thumbnails/f1.jpg' })
      }
      return ok(peopleResp)
    }
    if (url.startsWith('/faces?')) return ok({ faces: [], count: 7, offset: 0 })
    if (url.startsWith('/people/p1/merge')) return ok({ reassigned: 2, target_id: 'p1' })
    if (url === '/faces/search') return ok({
      matches: [
        {
          face_id: 'f-sim-1',
          score: 0.63,
          media_id: null,
          path: null,
          person_id: null,
          person_name: null,
          shot_index: null,
          kf_index: null,
        },
      ],
    })
    return ok({})
  }))
}

// ── WebSocket mock ─────────────────────────────────────────────────────────────

type WSMessage = { type: string; status: object; log: string[] }

class MockWebSocket {
  onmessage: ((e: { data: string }) => void) | null = null
  onclose: (() => void) | null = null
  static lastInstance: MockWebSocket | null = null
  static pendingMessage: WSMessage | null = null

  constructor(_url: string) {
    MockWebSocket.lastInstance = this
    if (MockWebSocket.pendingMessage) {
      const msg = MockWebSocket.pendingMessage
      setTimeout(() => this.onmessage?.({ data: JSON.stringify(msg) }), 0)
    }
  }
  close() {}
}

const idleWS: WSMessage = {
  type: 'update',
  status: { status: 'idle', run_id: null, started_at: null, finished_at: null, elapsed_seconds: null, return_code: null },
  log: [],
}

const runningWS: WSMessage = {
  type: 'update',
  status: { status: 'running', run_id: 'r1', started_at: null, finished_at: null, elapsed_seconds: 5, return_code: null },
  log: [],
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PeoplePage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('PeoplePage', () => {
  beforeEach(() => {
    vi.stubGlobal('WebSocket', MockWebSocket)
    MockWebSocket.lastInstance = null
    MockWebSocket.pendingMessage = idleWS
    mockFetch()
  })
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals() })

  it('shows page heading', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('People')).toBeTruthy())
  })

  it('renders person cards', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Alice')).toBeTruthy()
      expect(screen.getByText('Bob')).toBeTruthy()
    })
  })

  it('shows face counts', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('3 faces')).toBeTruthy()
      expect(screen.getByText('2 faces')).toBeTruthy()
    })
  })

  it('shows unknown-faces entry when no people', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (url === '/people') return ok({ people: [] })
      if (typeof url === 'string' && url.startsWith('/faces?')) return ok({ faces: [], count: 7, offset: 0 })
      return ok({})
    }))
    renderPage()
    // Overview always shows the "Unknown faces" entry card even when no people are labeled
    await waitFor(() => {
      expect(screen.getByText('Unknown faces')).toBeTruthy()
      expect(screen.getByText('7 faces')).toBeTruthy()
    })
  })

  it('shows person count in heading', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('(2)')).toBeTruthy()
    })
  })

  describe('indexer-running banner (similar-faces view)', () => {
    it('does not show warning banner in overview when indexer is idle', async () => {
      MockWebSocket.pendingMessage = idleWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      expect(screen.queryByText(/indexer is running/i)).toBeNull()
    })

    it('shows warning banner when entering similar-faces view while indexer is running', async () => {
      MockWebSocket.pendingMessage = runningWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() =>
        expect(screen.getByText(/indexer is running/i)).toBeTruthy()
      )
    })

    it('warning banner on similar-faces view mentions database locked', async () => {
      MockWebSocket.pendingMessage = runningWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() =>
        expect(screen.getByText(/database is locked/i)).toBeTruthy()
      )
    })

    it('shows person name as known-section title instead of "Known"', async () => {
      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (url.startsWith('/faces?')) return ok({ faces: [], count: 0, offset: 0 })
        if (url === '/faces/search') return ok({
          matches: [{
            face_id: 'f-known-1', score: 0.85, media_id: 'm1', path: '/photos/a.jpg',
            person_id: 'p1', person_name: 'Alice', shot_index: null, kf_index: null,
          }],
        })
        return ok({})
      }))
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() => expect(screen.getByText(/similar to alice/i)).toBeTruthy())
      // Section header must say "ALICE (N)", never "KNOWN"
      await waitFor(() => {
        expect(screen.queryByText(/\bknown\b/i)).toBeNull()
        // "Alice" and "(1)" are in sibling elements; check combined textContent via function matcher
        const found = screen.getAllByText((_, el) =>
          el?.tagName === 'SPAN' && /alice/i.test(el.textContent ?? '') && /\(\d+\)/.test(el.textContent ?? '')
        )
        expect(found.length).toBeGreaterThan(0)
      })
    })

    it('known section is expanded by default showing face thumbnails', async () => {
      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (url.startsWith('/faces?')) return ok({ faces: [], count: 0, offset: 0 })
        if (url === '/faces/search') return ok({
          matches: [{
            face_id: 'f-known-1', score: 0.85, media_id: 'm1', path: '/photos/a.jpg',
            person_id: 'p1', person_name: 'Alice', shot_index: null, kf_index: null,
          }],
        })
        return ok({})
      }))
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() => expect(screen.getByText(/similar to alice/i)).toBeTruthy())
      // Known section expanded: thumbnails visible without needing to click chevron
      await waitFor(() => expect(screen.getAllByRole('img').length).toBeGreaterThan(0))
    })

    it('supports precise 1-point threshold adjustments with buttons', async () => {
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() => expect(screen.getByText(/similar to alice/i)).toBeTruthy())

      expect(screen.getByText('40%')).toBeTruthy()
      await userEvent.click(screen.getByRole('button', { name: /increase similarity threshold/i }))
      expect(screen.getByText('41%')).toBeTruthy()
      await userEvent.click(screen.getByRole('button', { name: /decrease similarity threshold/i }))
      expect(screen.getByText('40%')).toBeTruthy()
    })
  })
})
