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
  status: { status: 'running', run_id: 'abc', started_at: null, finished_at: null, elapsed_seconds: 10, return_code: null },
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
  describe('indexer-running banner', () => {
    it('does not show warning banner when indexer is idle', async () => {
      MockWebSocket.pendingMessage = idleUpdate
      renderPage()
      await waitFor(() => expect(screen.getByPlaceholderText(/describe what you're looking for/i)).toBeTruthy())
      expect(screen.queryByText(/indexer is running/i)).toBeNull()
    })

    it('shows warning banner when indexer is running', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/indexer is running/i)).toBeTruthy()
      )
    })

    it('warning banner mentions database locked', async () => {
      MockWebSocket.pendingMessage = runningUpdate
      renderPage()
      await waitFor(() =>
        expect(screen.getByText(/database is locked/i)).toBeTruthy()
      )
    })
  })
})
