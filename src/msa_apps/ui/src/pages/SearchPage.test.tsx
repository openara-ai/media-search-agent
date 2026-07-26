import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { SearchPage } from './SearchPage'

// ── WebSocket mock ─────────────────────────────────────────────────────────

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

const idleUpdate: WSMessage = {
  type: 'update',
  status: { status: 'idle', run_id: null, started_at: null, finished_at: null, elapsed_seconds: null, return_code: null },
  log: [],
}

const runningUpdate: WSMessage = {
  type: 'update',
  status: { status: 'running', run_id: 'abc', started_at: null, finished_at: null, elapsed_seconds: 10, return_code: null, summary: { phase: 'processing' } },
  log: [],
}

// M-8/S-2: the Qdrant lock window — the only slice of a run where search is
// briefly unavailable (sentinel-file handoff).
const exportingUpdate: WSMessage = {
  type: 'update',
  status: { status: 'running', run_id: 'abc', started_at: null, finished_at: null, elapsed_seconds: 10, return_code: null, summary: { phase: 'exporting' } },
  log: [],
}

// ── Helpers ────────────────────────────────────────────────────────────────

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <SearchPage />
      </MemoryRouter>
    </QueryClientProvider>
  )
}

beforeEach(() => {
  vi.stubGlobal('WebSocket', MockWebSocket)
  MockWebSocket.lastInstance = null
  MockWebSocket.pendingMessage = idleUpdate
  vi.stubGlobal('fetch', vi.fn(() =>
    Promise.resolve({ ok: true, json: () => Promise.resolve([]) })
  ))
})

afterEach(() => {
  vi.unstubAllGlobals()
})

// ── Tests ──────────────────────────────────────────────────────────────────

describe('SearchPage', () => {
  describe('indexer phase-aware banner (M-8/S-2)', () => {
    it('does not show a banner when indexer is idle', async () => {
      MockWebSocket.pendingMessage = idleUpdate
      renderPage()
      await waitFor(() => expect(screen.getByPlaceholderText(/describe what you're looking for/i)).toBeTruthy())
      expect(screen.queryByText(/indexing in progress/i)).toBeNull()
      expect(screen.queryByText(/finalizing index/i)).toBeNull()
    })

    it('pre-export phases: search works — banner says results reflect the pre-run library', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/indexing in progress — results reflect your library before this run/i)).toBeTruthy()
      )
      // The pre-S-2 "search is temporarily unavailable" claim is now false
      // during the long tail of a run and must never come back.
      expect(screen.queryByText(/temporarily unavailable/i)).toBeNull()
      expect(screen.queryByText(/database is locked/i)).toBeNull()
    })

    it('exporting phase: banner flips to the finalizing message', async () => {
      MockWebSocket.pendingMessage = exportingUpdate
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/finalizing index — search resumes shortly/i)).toBeTruthy()
      )
      expect(screen.queryByText(/indexing in progress/i)).toBeNull()
    })

    it('running without a summary yet defaults to the pre-export message', async () => {
      MockWebSocket.pendingMessage = {
        type: 'update',
        status: { status: 'running', run_id: 'abc', started_at: null, finished_at: null, elapsed_seconds: 1, return_code: null },
        log: [],
      }
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/indexing in progress/i)).toBeTruthy()
      )
    })
  })
})
