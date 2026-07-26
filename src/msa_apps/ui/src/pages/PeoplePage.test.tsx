import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
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
  status: { status: 'running', run_id: 'r1', started_at: null, finished_at: null, elapsed_seconds: 5, return_code: null, summary: { phase: 'processing' } },
  log: [],
}

// M-8/S-2: the Qdrant lock window (export step) — the only slice of a run
// where face search is briefly unavailable.
const exportingWS: WSMessage = {
  type: 'update',
  status: { status: 'running', run_id: 'r1', started_at: null, finished_at: null, elapsed_seconds: 5, return_code: null, summary: { phase: 'exporting' } },
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

  describe('indexer phase-aware banner (similar-faces view, M-8/S-2)', () => {
    it('does not show a banner in overview when indexer is idle', async () => {
      MockWebSocket.pendingMessage = idleWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      expect(screen.queryByText(/indexing in progress/i)).toBeNull()
      expect(screen.queryByText(/finalizing index/i)).toBeNull()
    })

    it('pre-export phases: banner says face results reflect the pre-run library', async () => {
      MockWebSocket.pendingMessage = runningWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() =>
        expect(screen.getByText(/indexing in progress — face results reflect your library before this run/i)).toBeTruthy()
      )
      // Face search now works during the long tail of a run — the pre-S-2
      // "temporarily unavailable while the database is locked" text is false.
      expect(screen.queryByText(/temporarily unavailable/i)).toBeNull()
      expect(screen.queryByText(/database is locked/i)).toBeNull()
    })

    it('exporting phase: banner flips to the finalizing message', async () => {
      MockWebSocket.pendingMessage = exportingWS
      renderPage()
      await waitFor(() => expect(screen.getByText('Alice')).toBeTruthy())
      await userEvent.click(screen.getAllByRole('button', { name: 'Alice' })[0])
      await waitFor(() =>
        expect(screen.getByText(/finalizing index — face search resumes shortly/i)).toBeTruthy()
      )
      expect(screen.queryByText(/indexing in progress/i)).toBeNull()
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

      expect(screen.getByText('65%')).toBeTruthy()
      await userEvent.click(screen.getByRole('button', { name: /increase similarity threshold/i }))
      expect(screen.getByText('66%')).toBeTruthy()
      await userEvent.click(screen.getByRole('button', { name: /decrease similarity threshold/i }))
      expect(screen.getByText('65%')).toBeTruthy()
    })
  })

  // ── FaceCard expand-to-drawer affordance ────────────────────────────────────
  // Regression coverage for: thumbnail click opens drawer, bottom-bar buttons
  // do NOT bubble to the drawer, and orphaned faces have no expand affordance.
  describe('FaceCard expand affordance (Browse mode)', () => {
    class MockIO {
      observe() {}
      unobserve() {}
      disconnect() {}
    }

    function browseFetch(face: { face_id: string; media_id: string | null }) {
      return vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (typeof url === 'string' && url.startsWith('/faces?')) return ok({
          faces: [{
            face_id: face.face_id, media_id: face.media_id, path: '/photos/x.jpg',
            gender: null, age: null, person_id: null, person_name: null, thumbnail: null,
          }],
          count: 1, offset: 0,
        })
        if (face.media_id && url === `/media/${face.media_id}/info`) return ok({
          id: face.media_id, path: '/photos/x.jpg', type: 'image',
          date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
        })
        if (face.media_id && url === `/media/${face.media_id}/faces`) return ok({ faces: [] })
        if (url === '/faces/search') return ok({ matches: [] })
        return ok({})
      })
    }

    async function enterBrowseView() {
      renderPage()
      await waitFor(() => expect(screen.getByText('Unknown faces')).toBeTruthy())
      const unknownLabel = screen.getByText('Unknown faces')
      const unknownBtn = unknownLabel.closest('div')!.querySelector('button')!
      await userEvent.click(unknownBtn)
    }

    beforeEach(() => { vi.stubGlobal('IntersectionObserver', MockIO) })

    it('clicking a face thumbnail opens the side drawer with the source media', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-A', media_id: 'm-7' }))
      await enterBrowseView()

      const expandTarget = await screen.findByRole('button', { name: /expand face/i })
      await userEvent.click(expandTarget)

      await waitFor(() => {
        expect(document.querySelector('img[src="/images/m-7"]')).toBeTruthy()
      })
    })

    it('clicking the Label button does NOT also open the drawer (stopPropagation)', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-A', media_id: 'm-7' }))
      await enterBrowseView()

      const labelBtn = await screen.findByRole('button', { name: 'Label' })
      await userEvent.click(labelBtn)

      // Label popover opened; drawer's source-media image must not be in DOM
      expect(document.querySelector('img[src="/images/m-7"]')).toBeNull()
    })

    it('clicking Find similar does NOT also open the drawer (stopPropagation)', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-A', media_id: 'm-7' }))
      await enterBrowseView()

      const similarBtn = await screen.findByRole('button', { name: 'Find similar' })
      await userEvent.click(similarBtn)

      // Navigates to Similar mode; drawer's source-media image must not be in DOM
      expect(document.querySelector('img[src="/images/m-7"]')).toBeNull()
    })

    it('a face with no media_id exposes no expand affordance', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-orphan', media_id: null }))
      await enterBrowseView()

      // Bottom-bar buttons still render so we know the card mounted
      await screen.findByRole('button', { name: 'Label' })
      expect(screen.queryByRole('button', { name: /expand face/i })).toBeNull()
    })

    it('expand affordance is keyboard-activatable via Enter', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-A', media_id: 'm-7' }))
      await enterBrowseView()

      const expandTarget = await screen.findByRole('button', { name: /expand face/i })
      expect(expandTarget.getAttribute('tabindex')).toBe('0')

      expandTarget.focus()
      expect(document.activeElement).toBe(expandTarget)
      await userEvent.keyboard('{Enter}')

      await waitFor(() => {
        expect(document.querySelector('img[src="/images/m-7"]')).toBeTruthy()
      })
    })

    it('expand affordance is keyboard-activatable via Space', async () => {
      vi.stubGlobal('fetch', browseFetch({ face_id: 'face-A', media_id: 'm-7' }))
      await enterBrowseView()

      const expandTarget = await screen.findByRole('button', { name: /expand face/i })
      expandTarget.focus()
      await userEvent.keyboard(' ')

      await waitFor(() => {
        expect(document.querySelector('img[src="/images/m-7"]')).toBeTruthy()
      })
    })

    it('a slower earlier response cannot overwrite a faster later click', async () => {
      // Out-of-order resolution: click A, click B, B's /info resolves first,
      // then A's /info resolves last. Drawer must still show B.
      let resolveA: ((v: unknown) => void) | null = null
      let resolveB: ((v: unknown) => void) | null = null

      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (typeof url === 'string' && url.startsWith('/faces?')) return ok({
          faces: [
            { face_id: 'face-A', media_id: 'm-A', path: '/photos/a.jpg',
              gender: null, age: null, person_id: null, person_name: null, thumbnail: null },
            { face_id: 'face-B', media_id: 'm-B', path: '/photos/b.jpg',
              gender: null, age: null, person_id: null, person_name: null, thumbnail: null },
          ],
          count: 2, offset: 0,
        })
        if (url === '/media/m-A/info') return new Promise(r => {
          resolveA = () => r({ ok: true, json: () => Promise.resolve({
            id: 'm-A', path: '/photos/a.jpg', type: 'image',
            date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
          }) })
        })
        if (url === '/media/m-B/info') return new Promise(r => {
          resolveB = () => r({ ok: true, json: () => Promise.resolve({
            id: 'm-B', path: '/photos/b.jpg', type: 'image',
            date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
          }) })
        })
        if (url === '/media/m-A/faces' || url === '/media/m-B/faces') return ok({ faces: [] })
        return ok({})
      }))

      await enterBrowseView()

      const expandTargets = await screen.findAllByRole('button', { name: /expand face/i })
      expect(expandTargets.length).toBe(2)
      await userEvent.click(expandTargets[0])  // click A
      await userEvent.click(expandTargets[1])  // click B

      await waitFor(() => expect(resolveA).not.toBeNull())
      await waitFor(() => expect(resolveB).not.toBeNull())

      // Resolve in reversed order: B first (newer), then A (older/stale)
      await act(async () => { resolveB!(null) })
      await waitFor(() => {
        expect(document.querySelector('img[src="/images/m-B"]')).toBeTruthy()
      })
      await act(async () => { resolveA!(null) })
      // A is stale — drawer must remain on B, never swap to A
      expect(document.querySelector('img[src="/images/m-B"]')).toBeTruthy()
      expect(document.querySelector('img[src="/images/m-A"]')).toBeNull()
    })

    it('closing the drawer cancels an in-flight expand request', async () => {
      // Codex P2: user clicks A (drawer opens), clicks B (request pending),
      // closes the drawer, B's response resolves — must NOT reopen.
      let resolveB: ((v: unknown) => void) | null = null

      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (typeof url === 'string' && url.startsWith('/faces?')) return ok({
          faces: [
            { face_id: 'face-A', media_id: 'm-A', path: '/photos/a.jpg',
              gender: null, age: null, person_id: null, person_name: null, thumbnail: null },
            { face_id: 'face-B', media_id: 'm-B', path: '/photos/b.jpg',
              gender: null, age: null, person_id: null, person_name: null, thumbnail: null },
          ],
          count: 2, offset: 0,
        })
        if (url === '/media/m-A/info') return ok({
          id: 'm-A', path: '/photos/a.jpg', type: 'image',
          date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
        })
        if (url === '/media/m-B/info') return new Promise(r => {
          resolveB = () => r({ ok: true, json: () => Promise.resolve({
            id: 'm-B', path: '/photos/b.jpg', type: 'image',
            date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
          }) })
        })
        if (url === '/media/m-A/faces' || url === '/media/m-B/faces') return ok({ faces: [] })
        return ok({})
      }))

      await enterBrowseView()
      const targets = await screen.findAllByRole('button', { name: /expand face/i })

      // Open A's drawer
      await userEvent.click(targets[0])
      await waitFor(() => expect(document.querySelector('img[src="/images/m-A"]')).toBeTruthy())

      // Click B — request pending
      await userEvent.click(targets[1])
      await waitFor(() => expect(resolveB).not.toBeNull())

      // Close the drawer (drawer's keydown listener is on window)
      await act(async () => {
        window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
      })
      await waitFor(() => expect(document.querySelector('img[src="/images/m-A"]')).toBeNull())

      // B's stale response must NOT reopen the drawer
      await act(async () => { resolveB!(null) })
      expect(document.querySelector('img[src="/images/m-A"]')).toBeNull()
      expect(document.querySelector('img[src="/images/m-B"]')).toBeNull()
    })

    it('spinner stays visible while a second click on the same face is in flight', async () => {
      // Copilot: when loading was a Set<media_id>, the first request's finally
      // would clear the spinner even though a second request for the same mid
      // was still pending. Now tracked as a single latest mid.
      const resolves: Array<() => void> = []

      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (typeof url === 'string' && url.startsWith('/faces?')) return ok({
          faces: [{
            face_id: 'face-A', media_id: 'm-A', path: '/photos/a.jpg',
            gender: null, age: null, person_id: null, person_name: null, thumbnail: null,
          }],
          count: 1, offset: 0,
        })
        if (url === '/media/m-A/info') return new Promise(r => {
          resolves.push(() => r({ ok: true, json: () => Promise.resolve({
            id: 'm-A', path: '/photos/a.jpg', type: 'image',
            date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
          }) }))
        })
        if (url === '/media/m-A/faces') return ok({ faces: [] })
        return ok({})
      }))

      await enterBrowseView()
      const target = await screen.findByRole('button', { name: /expand face/i })

      await userEvent.click(target)
      await userEvent.click(target)
      await waitFor(() => expect(resolves.length).toBe(2))

      // Spinner is visible — Loader2 carries class "animate-spin"
      expect(target.querySelector('.animate-spin')).toBeTruthy()

      // Resolve the first request only — spinner must NOT clear because the
      // second request is still pending.
      await act(async () => { resolves[0]!() })
      expect(target.querySelector('.animate-spin')).toBeTruthy()

      // Resolving the second request clears the spinner
      await act(async () => { resolves[1]!() })
      await waitFor(() => expect(target.querySelector('.animate-spin')).toBeNull())
    })

    it('held keys (auto-repeat) do not fire multiple expand requests', async () => {
      // Copilot: keydown with repeat=true used to trigger one expand per
      // repeat tick. Guard now ignores repeats.
      let infoCalls = 0
      vi.stubGlobal('fetch', vi.fn((url: string) => {
        if (url === '/people') return ok(peopleResp)
        if (typeof url === 'string' && url.startsWith('/faces?')) return ok({
          faces: [{
            face_id: 'face-A', media_id: 'm-A', path: '/photos/a.jpg',
            gender: null, age: null, person_id: null, person_name: null, thumbnail: null,
          }],
          count: 1, offset: 0,
        })
        if (url === '/media/m-A/info') {
          infoCalls++
          return ok({
            id: 'm-A', path: '/photos/a.jpg', type: 'image',
            date: null, gps_lat: null, gps_lon: null, place: null, duration: null,
          })
        }
        if (url === '/media/m-A/faces') return ok({ faces: [] })
        return ok({})
      }))

      await enterBrowseView()
      const target = await screen.findByRole('button', { name: /expand face/i })
      target.focus()

      // Simulate auto-repeat: keydown fires repeatedly with repeat=true
      fireEvent.keyDown(target, { key: 'Enter', repeat: true })
      fireEvent.keyDown(target, { key: 'Enter', repeat: true })
      fireEvent.keyDown(target, { key: ' ', repeat: true })

      // Yield to any pending microtasks that would have fired requests
      await act(async () => { await Promise.resolve() })

      expect(infoCalls).toBe(0)
      expect(document.querySelector('img[src="/images/m-A"]')).toBeNull()
    })
  })
})
