import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { renderHook, waitFor } from '@testing-library/react'
import {
  shouldConfirmClose,
  CLOSE_WHILE_INDEXING_MESSAGE,
  useCloseWhileIndexingGuard,
} from './useCloseWhileIndexing'

// ── the pure predicate ────────────────────────────────────────────────────────

describe('shouldConfirmClose', () => {
  it('confirms only for a running index', () => {
    expect(shouldConfirmClose('running')).toBe(true)
  })
  it.each(['idle', 'complete', 'error', 'stopped', undefined])(
    'closes silently for %s',
    (status) => {
      expect(shouldConfirmClose(status as string | undefined)).toBe(false)
    },
  )
  it('discloses that indexing continues in the background', () => {
    expect(CLOSE_WHILE_INDEXING_MESSAGE).toMatch(/keep running in the background/i)
  })
})

// ── the guard hook ────────────────────────────────────────────────────────────
//
// API_BASE is captured from window.__API_BASE__ at module load, so we mock the
// seam module to flip between browser (empty) and shell (non-empty) mode.

const onCloseRequested = vi.fn()
const destroy = vi.fn()
let capturedHandler: ((event: { preventDefault: () => void }) => void) | undefined

vi.mock('@tauri-apps/api/window', () => ({
  getCurrentWindow: () => ({
    onCloseRequested: (handler: (event: { preventDefault: () => void }) => void) => {
      capturedHandler = handler
      onCloseRequested(handler)
      return Promise.resolve(() => {})
    },
    destroy,
  }),
}))

vi.mock('../lib/apiBase', () => ({ API_BASE: 'http://127.0.0.1:52341' }))

beforeEach(() => {
  onCloseRequested.mockClear()
  destroy.mockClear()
  capturedHandler = undefined
})
afterEach(() => vi.clearAllMocks())

async function fireClose() {
  const preventDefault = vi.fn()
  capturedHandler!({ preventDefault })
  return { preventDefault }
}

describe('useCloseWhileIndexingGuard (shell mode)', () => {
  it('registers a close listener once the window API resolves', async () => {
    renderHook(() => useCloseWhileIndexingGuard(true, () => true))
    await waitFor(() => expect(onCloseRequested).toHaveBeenCalledTimes(1))
  })

  it('running + confirm → prevents default and destroys the window', async () => {
    const confirmFn = vi.fn(() => true)
    renderHook(() => useCloseWhileIndexingGuard(true, confirmFn))
    await waitFor(() => expect(capturedHandler).toBeDefined())
    const { preventDefault } = await fireClose()
    expect(preventDefault).toHaveBeenCalledOnce()
    expect(confirmFn).toHaveBeenCalledWith(CLOSE_WHILE_INDEXING_MESSAGE)
    expect(destroy).toHaveBeenCalledOnce()
  })

  it('running + cancel → prevents default but keeps the window open', async () => {
    const confirmFn = vi.fn(() => false)
    renderHook(() => useCloseWhileIndexingGuard(true, confirmFn))
    await waitFor(() => expect(capturedHandler).toBeDefined())
    const { preventDefault } = await fireClose()
    expect(preventDefault).toHaveBeenCalledOnce()
    expect(destroy).not.toHaveBeenCalled()
  })

  it('not running → lets the close proceed without a prompt', async () => {
    const confirmFn = vi.fn(() => true)
    renderHook(() => useCloseWhileIndexingGuard(false, confirmFn))
    await waitFor(() => expect(capturedHandler).toBeDefined())
    const { preventDefault } = await fireClose()
    expect(preventDefault).not.toHaveBeenCalled()
    expect(confirmFn).not.toHaveBeenCalled()
    expect(destroy).not.toHaveBeenCalled()
  })

  it('status ticks (re-renders) do NOT tear down and resubscribe the listener', async () => {
    // Regression for the #169-reopening bug: useIndexerRunning() re-renders this
    // component on every WS tick during a run, each time passing a fresh inline
    // confirmFn. The empty-deps effect + refs must keep exactly ONE subscription —
    // otherwise the sync-unlisten/async-reattach churn opens repeated unguarded
    // windows. (Before the fix, a [confirmFn] dep re-subscribed on every render.)
    const { rerender } = renderHook(
      ({ running }) => useCloseWhileIndexingGuard(running, () => true),
      { initialProps: { running: true } },
    )
    await waitFor(() => expect(onCloseRequested).toHaveBeenCalledTimes(1))
    rerender({ running: true })
    rerender({ running: true })
    rerender({ running: false })
    rerender({ running: true })
    expect(onCloseRequested).toHaveBeenCalledTimes(1)
  })
})
